"""Proof-of-test on a target repo checked out with ``core.autocrlf`` true (CRLF working tree, as on a
Windows user's machine) and false. The proof stashes and restores the change; it must still prove it, and
must hand back the user's files byte for byte, whatever their line endings.
"""

import subprocess

import pytest

from kickass_loop_engineer.gates import DEFAULT_GATE_CMD
from kickass_loop_engineer.verifier import prove


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.mark.parametrize(("autocrlf", "eol"), [("true", b"\r\n"), ("false", b"\n")])
def test_proof_proves_a_real_change_and_restores_it_byte_for_byte(tmp_path, autocrlf, eol):
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
    assert (repo / "answer.py").read_bytes() == change
