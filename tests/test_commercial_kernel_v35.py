"""Tests for v3.5.0 commercial kernel: POSIX confinement parity, scoped fast-path verifier, and SLSA attestation."""

from __future__ import annotations

import copy
import os
import subprocess
from dataclasses import dataclass, field

import pytest

from kickass_loop_engineer import confine
from kickass_loop_engineer.attestation import (
    HIVEMIND_PREDICATE_TYPE,
    IN_TOTO_STATEMENT_TYPE,
    AttestationReceipt,
    create_attestation,
    verify_attestation,
)
from kickass_loop_engineer.guardrails import VerificationPolicy, VerificationResult
from kickass_loop_engineer.verifier import ProofOfTestVerifier, scoped_gate_command


# ---------------------------------------------------------------------------
# 1. Cross-Platform Confinement Parity (POSIX Seatbelt & Bubblewrap)
# ---------------------------------------------------------------------------


def test_seatbelt_profile_generates_expected_sbpl_rules():
    profile = confine._seatbelt_profile("/workspace/app", "/tmp/le-confine-123/tmp")
    assert "(version 1)" in profile
    assert "(allow default)" in profile
    assert "(deny network*)" in profile
    assert '(deny file-write* (subpath "/"))' in profile
    assert '(allow file-write* (subpath "/workspace/app"))' in profile
    assert '(allow file-write* (subpath "/tmp/le-confine-123/tmp"))' in profile
    assert '(deny file-write* (subpath "/workspace/app/.loop-engineer"))' in profile


def test_bwrap_argv_with_and_without_run_state_dir(tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    argv_no_state = confine._bwrap_argv(["pytest", "-q"], str(cwd), str(scratch))
    assert argv_no_state[:6] == ["bwrap", "--ro-bind", "/", "/", "--bind", str(cwd)]
    assert "--unshare-net" in argv_no_state
    assert "--die-with-parent" in argv_no_state
    assert ["--bind", str(scratch), "/tmp"] == argv_no_state[
        argv_no_state.index("/tmp") - 2 : argv_no_state.index("/tmp") + 1
    ]
    assert argv_no_state[-2:] == ["pytest", "-q"]
    state_path = os.path.join(str(cwd), confine.RUN_STATE_DIR)
    assert state_path not in argv_no_state

    # Now create .loop-engineer and verify it is masked read-only
    (cwd / confine.RUN_STATE_DIR).mkdir()
    argv_with_state = confine._bwrap_argv(["pytest", "-q"], str(cwd), str(scratch))
    assert state_path in argv_with_state
    idx = argv_with_state.index(state_path)
    assert argv_with_state[idx - 1 : idx + 2] == ["--ro-bind", state_path, state_path]


def test_detect_posix_backend_darwin_linux_and_fallback(monkeypatch):
    monkeypatch.setattr(confine.sys, "platform", "darwin")
    monkeypatch.setattr(confine.shutil, "which", lambda name: "/usr/bin/sandbox-exec" if name == "sandbox-exec" else None)
    assert confine._detect_posix_backend() == "seatbelt"

    monkeypatch.setattr(confine.sys, "platform", "linux")
    monkeypatch.setattr(confine.shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
    assert confine._detect_posix_backend() == "bwrap"

    monkeypatch.setattr(confine.shutil, "which", lambda _name: None)
    assert confine._detect_posix_backend() is None


def test_run_confined_posix_seatbelt_and_bwrap_dispatch(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    state = work / ".loop-engineer"
    state.mkdir()

    captured_calls = []

    def fake_run(cmd, **kwargs):
        captured_calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

    monkeypatch.setattr(confine, "CONFINEMENT_AVAILABLE", True)
    monkeypatch.setattr(confine.subprocess, "run", fake_run)

    # Seatbelt dispatch
    monkeypatch.setattr(confine, "_detect_posix_backend", lambda: "seatbelt")
    res_seatbelt = confine.run_confined(["pytest", "-q"], cwd=str(work), env={}, timeout=30.0)
    completed, confined_flag = res_seatbelt
    assert confined_flag is True
    assert res_seatbelt.confined is True
    assert completed.returncode == 0
    assert captured_calls[-1][0][0] == "sandbox-exec"
    assert captured_calls[-1][0][1] == "-p"
    assert "(deny network*)" in captured_calls[-1][0][2]
    assert captured_calls[-1][0][3:] == ["pytest", "-q"]

    # Bwrap dispatch
    monkeypatch.setattr(confine, "_detect_posix_backend", lambda: "bwrap")
    res_bwrap = confine.run_confined(["pytest", "-q"], cwd=str(work), env={}, timeout=30.0)
    assert res_bwrap.confined is True
    assert res_bwrap.returncode == 0
    assert captured_calls[-1][0][0] == "bwrap"
    assert "--unshare-net" in captured_calls[-1][0]
    assert captured_calls[-1][0][-2:] == ["pytest", "-q"]


# ---------------------------------------------------------------------------
# 2. Scoped Fast-Path Pytest Helper & ProofOfTestVerifier(fast_red_scope=True)
# ---------------------------------------------------------------------------


def test_scoped_gate_command_appends_safe_test_files_and_rejects_unsafe():
    assert scoped_gate_command("pytest -q", ["tests/test_alpha.py", "test_beta.py"]) == (
        "pytest -q tests/test_alpha.py test_beta.py"
    )
    assert scoped_gate_command("python -m pytest -q", ["tests/unit/test_x.py"]) == (
        "python -m pytest -q tests/unit/test_x.py"
    )
    assert scoped_gate_command("python3 -m pytest", ["tests/test_y.py"]) == (
        "python3 -m pytest tests/test_y.py"
    )
    # Non-pytest commands stay unchanged
    assert scoped_gate_command("ruff check .", ["tests/test_alpha.py"]) == "ruff check ."
    # Empty test_files stays unchanged
    assert scoped_gate_command("pytest -q", []) == "pytest -q"
    # Non-test or support files alone stay unchanged
    assert scoped_gate_command("pytest -q", ["src/module.py", "tests/conftest.py"]) == "pytest -q"
    # Unsafe paths (metacharacters, traversal, absolute, flag injection) stay unchanged
    assert scoped_gate_command("pytest -q", ["tests/test_a.py; echo bad"]) == "pytest -q"
    assert scoped_gate_command("pytest -q", ["../test_escape.py"]) == "pytest -q"
    assert scoped_gate_command("pytest -q", ["/abs/test_a.py"]) == "pytest -q"
    assert scoped_gate_command("pytest -q", ["-o=evil.py"]) == "pytest -q"


@dataclass
class _RecordingPolicy(VerificationPolicy):
    commands: list[str] = field(default_factory=list)

    def run(self, command: str, cwd: str, **kwargs) -> VerificationResult:
        self.commands.append(command)
        return super().run(command, cwd, **kwargs)


def _init_git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n", encoding="utf-8")
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_baseline.py").write_text("def test_base():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def test_proof_of_test_verifier_fast_red_scope_scopes_only_reverted_run(tmp_path):
    repo = _init_git_repo(tmp_path)
    (repo / "feature.py").write_text("def compute():\n    return 99\n", encoding="utf-8")
    (repo / "tests" / "test_feature.py").write_text(
        "from feature import compute\n\ndef test_compute():\n    assert compute() == 99\n",
        encoding="utf-8",
    )

    # With fast_red_scope=True, the middle (reverted) run scopes to tests/test_feature.py
    policy_fast = _RecordingPolicy()
    verifier_fast = ProofOfTestVerifier(policy=policy_fast, fast_red_scope=True)
    record_fast = verifier_fast.prove("pytest -q", str(repo))
    assert record_fast.proven is True
    assert policy_fast.commands == [
        "pytest -q",
        "pytest -q tests/test_feature.py",
        "pytest -q",
    ]

    # Default fast_red_scope=False runs unscoped gate command on all 3 phases
    policy_default = _RecordingPolicy()
    verifier_default = ProofOfTestVerifier(policy=policy_default)
    assert verifier_default.fast_red_scope is False
    record_default = verifier_default.prove("pytest -q", str(repo))
    assert record_default.proven is True
    assert policy_default.commands == [
        "pytest -q",
        "pytest -q",
        "pytest -q",
    ]


# ---------------------------------------------------------------------------
# 3. SLSA Level 3 / In-Toto Build Attestation Module
# ---------------------------------------------------------------------------


def test_create_and_verify_attestation_unsigned_and_signed():
    stmt_unsigned = create_attestation(
        subject_name="kickass-loop-engineer",
        candidate_digest="sha256:abcdef0123456789",
        builder_id="claude-opus-4-6",
        reviewer_id="gpt-5.4",
        proof_passed=True,
        confined=True,
        signing_key=None,
    )
    assert stmt_unsigned["_type"] == IN_TOTO_STATEMENT_TYPE
    assert stmt_unsigned["predicateType"] == HIVEMIND_PREDICATE_TYPE
    assert stmt_unsigned["subject"][0]["name"] == "kickass-loop-engineer"
    assert stmt_unsigned["subject"][0]["digest"]["sha256"] == "abcdef0123456789"
    assert stmt_unsigned["predicate"]["proof_passed"] is True
    assert stmt_unsigned["predicate"]["confined"] is True
    assert stmt_unsigned["statement_digest"]
    assert stmt_unsigned["signature"] is None
    assert verify_attestation(stmt_unsigned) is True

    receipt = AttestationReceipt.from_statement(stmt_unsigned)
    assert receipt.subject_name == "kickass-loop-engineer"
    assert receipt.builder_id == "claude-opus-4-6"
    assert receipt.reviewer_id == "gpt-5.4"
    assert receipt.proof_passed is True
    assert receipt.confined is True
    assert receipt.to_dict()["statement_digest"] == stmt_unsigned["statement_digest"]

    # Signed attestation with HMAC-SHA256
    key = "super-secret-signing-key"
    stmt_signed = create_attestation(
        subject_name="kickass-loop-engineer",
        candidate_digest="abcdef0123456789",
        builder_id="claude-opus-4-6",
        reviewer_id="gpt-5.4",
        proof_passed=True,
        confined=True,
        signing_key=key,
    )
    assert isinstance(stmt_signed["signature"], str) and len(stmt_signed["signature"]) == 64
    assert verify_attestation(stmt_signed, signing_key=key) is True
    assert verify_attestation(stmt_signed, signing_key="wrong-key") is False
    assert verify_attestation(stmt_signed, signing_key=None) is False


def test_verify_attestation_detects_tampering():
    key = " signing-key-v35 "
    stmt = create_attestation(
        subject_name="artifact.whl",
        candidate_digest="1234567890abcdef",
        builder_id="builder-1",
        reviewer_id="reviewer-1",
        proof_passed=True,
        confined=True,
        signing_key=key,
    )
    assert verify_attestation(stmt, signing_key=key) is True

    # Tamper with predicate
    tampered_pred = copy.deepcopy(stmt)
    tampered_pred["predicate"]["confined"] = False
    assert verify_attestation(tampered_pred, signing_key=key) is False

    # Tamper with subject digest
    tampered_subj = copy.deepcopy(stmt)
    tampered_subj["subject"][0]["digest"]["sha256"] = "0000000000000000"
    assert verify_attestation(tampered_subj, signing_key=key) is False

    # Tamper with signature directly
    tampered_sig = copy.deepcopy(stmt)
    tampered_sig["signature"] = "0" * 64
    assert verify_attestation(tampered_sig, signing_key=key) is False

    # Tamper with statement_digest
    tampered_digest = copy.deepcopy(stmt)
    tampered_digest["statement_digest"] = "f" * 64
    assert verify_attestation(tampered_digest, signing_key=key) is False
