"""Proof-of-test on a target repo under each ``core.autocrlf`` setting.

The proof stashes and restores the change, and git converts line endings on the way, so the change comes
back as git would check it out: byte for byte when its endings already match the setting (CRLF under
``true``, anything under ``false``), and with the same text but converted endings when they do not. Either
way the proof must still prove a real change.
"""

import subprocess

import pytest

from kickass_loop_engineer.gates import DEFAULT_GATE_CMD
from kickass_loop_engineer.verifier import prove


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.mark.parametrize(("autocrlf", "eol", "byte_exact"), [
    ("true", b"\r\n", True), ("false", b"\n", True), ("false", b"\r\n", True),
    ("true", b"\n", False), ("input", b"\r\n", False),
])
def test_proof_proves_a_real_change_and_restores_its_text(tmp_path, autocrlf, eol, byte_exact):
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "core.autocrlf", autocrlf)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_bytes(b"__pycache__/" + eol + b"*.pyc" + eol + b".pytest_cache/" + eol)
    (repo / "test_answer.py").write_bytes(
        b"from answer import answer" + eol + eol + b"def test_answer():" + eol + b"    assert answer() == 42" + eol)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "red baseline")
    change = b"def answer():" + eol + b"    return 42" + eol
    (repo / "answer.py").write_bytes(change)

    record = prove(DEFAULT_GATE_CMD, str(repo))

    assert record.proven, record.error
    restored = (repo / "answer.py").read_bytes()
    assert restored.replace(b"\r\n", b"\n") == change.replace(b"\r\n", b"\n")
    if byte_exact:
        assert restored == change
