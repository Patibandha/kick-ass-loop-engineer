"""An attempt's changes survive its worktree as a patch, so a stalled or blocked run does not lose its work."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

from kickass_loop_engineer.orchestrator import Orchestrator
from kickass_loop_engineer.worktree import WorktreeManager


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                          capture_output=True)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "existing.py").write_bytes(b"A = 1\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def test_an_attempts_changes_are_kept_as_a_patch_that_applies(tmp_path):
    repo = _repo(tmp_path)
    worktree = Path(WorktreeManager(str(repo), base_dir=str(tmp_path / "wts")).create("build"))
    (worktree / "existing.py").write_bytes(b"A = 2\n")
    (worktree / "new_module.py").write_bytes(b"B = 3\n")

    Orchestrator._keep_attempt_patch(SimpleNamespace(run_id="r1", workspace=str(repo)), str(worktree))

    patch = repo / ".loop-engineer" / "attempts" / f"r1-{worktree.name}.patch"
    assert patch.is_file()
    _git(repo, "apply", str(patch))
    # git may convert line endings on apply (core.autocrlf); the content must match.
    assert (repo / "existing.py").read_bytes().replace(b"\r\n", b"\n") == b"A = 2\n"
    assert (repo / "new_module.py").read_bytes().replace(b"\r\n", b"\n") == b"B = 3\n"


def test_an_attempt_without_changes_leaves_no_patch(tmp_path):
    repo = _repo(tmp_path)
    worktree = WorktreeManager(str(repo), base_dir=str(tmp_path / "wts")).create("build")

    Orchestrator._keep_attempt_patch(SimpleNamespace(run_id="r1", workspace=str(repo)), worktree)

    assert not (repo / ".loop-engineer" / "attempts").exists()
