# tests/test_worktree.py
"""Tests for isolated git worktrees."""
import os, subprocess, tempfile, unittest
from pathlib import Path
from kickass_loop_engineer.worktree import WorktreeManager


def _repo():
    r = tempfile.mkdtemp()
    for args in (["init"], ["config","user.email","t@t"], ["config","user.name","t"]):
        subprocess.run(["git", *args], cwd=r, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    open(os.path.join(r, "seed.txt"), "w").write("x")
    subprocess.run(["git","add","-A"], cwd=r, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git","commit","-m","seed"], cwd=r, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r


class WorktreeTests(unittest.TestCase):
    def test_create_makes_isolated_dir_then_remove(self):
        repo = _repo()
        mgr = WorktreeManager(repo)
        path = mgr.create("frontend")
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(os.path.exists(os.path.join(path, "seed.txt")))
        out = subprocess.run(["git","worktree","list"], cwd=repo, capture_output=True, text=True)
        self.assertIn(path, out.stdout)
        mgr.remove(path)
        out = subprocess.run(["git","worktree","list"], cwd=repo, capture_output=True, text=True)
        self.assertNotIn(path, out.stdout)

    def test_context_manager_cleans_up(self):
        repo = _repo()
        mgr = WorktreeManager(repo)
        with mgr.worktree("backend") as path:
            self.assertTrue(os.path.isdir(path))
        self.assertFalse(os.path.isdir(path))

    def test_non_git_dir_raises_clear_error(self):
        with self.assertRaises(Exception):
            WorktreeManager(tempfile.mkdtemp()).create("x")


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "existing.txt").write_text("original\n")
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=repo, check=True)
    return repo


def test_promote_copies_changed_and_new_files(tmp_path):
    repo = _make_repo(tmp_path)
    mgr = WorktreeManager(str(repo), base_dir=str(tmp_path / "wts"))
    wt = mgr.create("attempt")
    (Path(wt) / "new_module.py").write_text("VALUE = 1\n")
    (Path(wt) / "existing.txt").write_text("changed\n")
    (Path(wt) / ".loop-engineer").mkdir()
    (Path(wt) / ".loop-engineer" / "events.jsonl").write_text("{}\n")

    promoted = mgr.promote(wt, str(repo))

    assert sorted(promoted) == ["existing.txt", "new_module.py"]
    assert (repo / "new_module.py").read_text() == "VALUE = 1\n"
    assert (repo / "existing.txt").read_text() == "changed\n"
    assert not (repo / ".loop-engineer" / "events.jsonl").exists()
    mgr.remove(wt)


def test_promote_with_no_changes_returns_empty(tmp_path):
    repo = _make_repo(tmp_path)
    mgr = WorktreeManager(str(repo), base_dir=str(tmp_path / "wts"))
    wt = mgr.create("attempt")
    assert mgr.promote(wt, str(repo)) == []
    mgr.remove(wt)


def test_promote_skips_symlinks_pointing_outside_worktree(tmp_path):
    repo = _make_repo(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET\n")
    mgr = WorktreeManager(str(repo), base_dir=str(tmp_path / "wts"))
    wt = mgr.create("attempt")
    os.symlink(secret, Path(wt) / "leak.py")

    promoted = mgr.promote(wt, str(repo))

    assert "leak.py" not in promoted
    assert not (repo / "leak.py").exists()
    mgr.remove(wt)


def test_promote_handles_non_ascii_filenames(tmp_path):
    repo = _make_repo(tmp_path)
    mgr = WorktreeManager(str(repo), base_dir=str(tmp_path / "wts"))
    wt = mgr.create("attempt")
    (Path(wt) / "café.py").write_text("VALUE = 2\n")

    promoted = mgr.promote(wt, str(repo))

    assert promoted == ["café.py"]
    assert (repo / "café.py").read_text() == "VALUE = 2\n"
    mgr.remove(wt)


def test_promote_never_propagates_deletions(tmp_path):
    repo = _make_repo(tmp_path)
    mgr = WorktreeManager(str(repo), base_dir=str(tmp_path / "wts"))
    wt = mgr.create("attempt")
    (Path(wt) / "existing.txt").unlink()

    promoted = mgr.promote(wt, str(repo))

    assert promoted == []
    assert (repo / "existing.txt").read_text() == "original\n"
    mgr.remove(wt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
