"""Proof-of-test negative tests: every way a pass can lack real proof is REFUSED, with the exact reason.

Policy (Meet, decisions.md 2026-10-22): a failed rerun of an unchanged candidate cancels any earlier
pass. The candidate is refused until a fresh proof is green, and no earlier pass is ever reused.

The engine keeps no proof cache: ``run_gate(..., with_proof=True)`` recomputes green -> red -> green on
every call. So each case below drives a real throwaway git repository and a real ``pytest -q`` gate
(the allowlisted command), and asserts the verdict plus the reason recorded on the proof. Nothing
depends on the host OS: the verdicts come from exit codes and git state alone.
"""

import json
import os
import subprocess

import pytest

from kickass_loop_engineer.verifier import prove, run_gate

pytestmark = pytest.mark.guardrail_proof

GATE = "pytest -q"
NO_CHANGE = "proof-of-test requires an uncommitted change to revert"

FEATURE_TEST = "from impl import answer\n\n\ndef test_answer():\n    assert answer() == 42\n"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _write(repo, rel, text):
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path) or repo, exist_ok=True)
    with open(path, "w", newline="\n") as handle:
        handle.write(text)
    return path


def _repo(tmp_path, files):
    """A repo with *files* committed as the baseline; caches are gitignored."""
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _write(repo, ".gitignore", "__pycache__/\n*.pyc\n.pytest_cache/\n")
    for rel, text in files.items():
        _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "baseline")
    return repo


def _stash_list(repo):
    return subprocess.run(["git", "stash", "list"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def _refused(evidence):
    """Assert the shared shape of a proof refusal: not passed, the proof present, not proven, not aborted."""
    assert evidence.passed is False
    assert evidence.proof is not None
    assert evidence.proof.proven is False
    assert evidence.proof.aborted is False


def _refused_red(evidence, repo):
    """Assert a refusal because the gate itself is red: no proof is attempted, the exit code is the reason."""
    assert evidence.passed is False
    assert evidence.proof is None
    assert evidence.evidence.startswith("exit=1;")
    assert _stash_list(repo) == ""


def test_a_genuine_proof_is_accepted_as_the_control(tmp_path):
    repo = _repo(tmp_path, {"test_feature.py": FEATURE_TEST})
    _write(repo, "impl.py", "def answer():\n    return 42\n")

    evidence = run_gate("unit", GATE, workspace=repo, with_proof=True)

    assert evidence.passed is True
    assert (evidence.proof.green_before, evidence.proof.red_when_reverted,
            evidence.proof.green_after) == (True, True, True)
    assert evidence.proof.error == ""
    assert _stash_list(repo) == ""


def test_missing_proof_nothing_to_revert_is_refused(tmp_path):
    # The suite is green but the tree is clean: there is no change a proof could be about.
    repo = _repo(tmp_path, {"test_feature.py": FEATURE_TEST,
                            "impl.py": "def answer():\n    return 42\n"})

    evidence = run_gate("unit", GATE, workspace=repo, with_proof=True)

    _refused(evidence)
    assert evidence.proof.green_before is True
    assert evidence.proof.red_when_reverted is False
    assert evidence.proof.green_after is False
    assert evidence.proof.error == NO_CHANGE
    assert evidence.proof.skipped_reason == ""


def test_missing_proof_is_not_a_pass_for_the_bare_prove_call_either(tmp_path):
    repo = _repo(tmp_path, {"test_feature.py": FEATURE_TEST,
                            "impl.py": "def answer():\n    return 42\n"})

    record = prove(GATE, repo)

    assert record.proven is False
    assert record.error == NO_CHANGE


def test_forged_proof_record_on_disk_is_ignored_and_the_change_is_refused(tmp_path):
    # The suite is green with or without the new source, so the change proves nothing; a forged
    # record planted in the workspace claiming otherwise must not turn that into a pass.
    repo = _repo(tmp_path, {"test_existing.py": "def test_existing():\n    assert True\n"})
    _write(repo, "unused.py", "VALUE = 1\n")
    forged = {"gate_cmd": GATE, "green_before": True, "red_when_reverted": True,
              "green_after": True, "proven": True, "error": "", "aborted": False}
    _write(repo, os.path.join(".loop-engineer", "proof.json"), json.dumps(forged))

    evidence = run_gate("unit", GATE, workspace=repo, with_proof=True)

    _refused(evidence)
    assert evidence.proof.green_before is True
    assert evidence.proof.red_when_reverted is False   # the forged claim is contradicted by the rerun
    assert evidence.proof.green_after is True          # restored and green: it never depended on the change
    assert evidence.proof.error == ""
    assert evidence.proof.skipped_reason == ""
    assert _stash_list(repo) == ""


def test_forged_proof_that_the_gate_would_pass_without_the_source_is_refused(tmp_path):
    # Same forgery through the bare ``prove`` call: the verdict is the rerun, never the claim.
    repo = _repo(tmp_path, {"test_existing.py": "def test_existing():\n    assert True\n"})
    _write(repo, "unused.py", "VALUE = 1\n")
    _write(repo, os.path.join(".loop-engineer", "proof.json"), json.dumps({"proven": True}))

    record = prove(GATE, repo)

    assert record.proven is False
    assert record.red_when_reverted is False


def test_proof_for_different_content_is_refused(tmp_path):
    repo = _repo(tmp_path, {"test_feature.py": FEATURE_TEST})
    _write(repo, "impl.py", "def answer():\n    return 42\n")
    earlier = run_gate("unit", GATE, workspace=repo, with_proof=True)
    assert earlier.passed is True and earlier.proof.proven is True

    # The candidate's content changes after the proof; the earlier proof describes other content.
    _write(repo, "impl.py", "def answer():\n    return 41\n")
    evidence = run_gate("unit", GATE, workspace=repo, with_proof=True)

    _refused_red(evidence, repo)


def test_pass_left_over_after_a_failed_rerun_of_the_unchanged_candidate_is_cancelled(tmp_path):
    flag = str(tmp_path / "flaky.flag")
    flaky_test = ("import os\n\nfrom impl import answer\n\n\n"
                  "def test_answer():\n"
                  f"    assert not os.path.exists({flag!r})\n"
                  "    assert answer() == 42\n")
    repo = _repo(tmp_path, {"test_feature.py": flaky_test})
    _write(repo, "impl.py", "def answer():\n    return 42\n")

    earlier = run_gate("unit", GATE, workspace=repo, with_proof=True)
    assert earlier.passed is True and earlier.proof.proven is True
    candidate = {rel: open(os.path.join(repo, rel)).read()
                 for rel in ("impl.py", "test_feature.py")}

    # Rerun the SAME candidate while the gate now fails: the earlier pass must not be reused.
    with open(flag, "w") as handle:
        handle.write("fail\n")
    failed = run_gate("unit", GATE, workspace=repo, with_proof=True)

    _refused_red(failed, repo)
    assert "test_answer" in failed.evidence
    assert {rel: open(os.path.join(repo, rel)).read() for rel in candidate} == candidate

    # Still refused on a further rerun that fails again; the candidate has not changed.
    again = run_gate("unit", GATE, workspace=repo, with_proof=True)
    _refused_red(again, repo)

    # Only a fresh, fully green proof lifts the refusal.
    os.remove(flag)
    fresh = run_gate("unit", GATE, workspace=repo, with_proof=True)
    assert fresh.passed is True
    assert (fresh.proof.green_before, fresh.proof.red_when_reverted,
            fresh.proof.green_after) == (True, True, True)
    assert _stash_list(repo) == ""
