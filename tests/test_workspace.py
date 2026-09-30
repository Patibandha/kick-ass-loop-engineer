"""Workspace behaviour: FILE-block parsing and in-place harvesting.

The harvest cases run against real (tiny) git repositories under ``tmp_path``
so the git plumbing behind ``fingerprint``/``harvest`` is exercised for real
rather than mocked.
"""
import os
import subprocess

import pytest

from kickass_loop_engineer.guardrails import WritePolicy
from kickass_loop_engineer.workspace import Workspace, WorkspaceError


def _block(body: str, path: str = "pyproject.toml") -> str:
    return f"=== FILE: {path} ===\n{body}\n=== END FILE ==="


def test_parse_strips_wrapping_code_fence():
    body = "```toml\n[project]\nname = \"x\"\n```"
    files = Workspace.parse(_block(body))
    assert files[0].content == "[project]\nname = \"x\""


def test_parse_strips_tilde_fence_with_language_tag():
    body = "~~~python\nprint('hi')\n~~~"
    files = Workspace.parse(_block(body, path="a.py"))
    assert files[0].content == "print('hi')"


def test_parse_keeps_unfenced_body_verbatim():
    body = "[project]\nname = \"x\""
    files = Workspace.parse(_block(body))
    assert files[0].content == body


def test_parse_keeps_unmatched_opening_fence():
    body = "```toml\n[project]\nname = \"x\""
    files = Workspace.parse(_block(body))
    assert files[0].content == body


def test_parse_keeps_interior_fences():
    body = "# doc\n```toml\nexample\n```\ntail"
    files = Workspace.parse(_block(body, path="README.md"))
    assert files[0].content == body


def test_worktree_seeds_uncommitted_files(tmp_path):
    """A new worktree contains the workspace's uncommitted additions."""
    import subprocess
    from kickass_loop_engineer.worktree import WorktreeManager

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "init"],
                   cwd=repo, check=True)
    (repo / "engine").mkdir()
    (repo / "engine" / "hardware.py").write_text("PROBE = 1\n")
    (repo / ".loop-engineer").mkdir()
    (repo / ".loop-engineer" / "state.json").write_text("{}")

    mgr = WorktreeManager(str(repo), base_dir=str(tmp_path / "wt"))
    path = mgr.create("slice")
    try:
        assert (tmp_path / "wt").exists()
        seeded = pathlib_read(path, "engine/hardware.py")
        assert seeded == "PROBE = 1\n"
        assert not (pathlib_exists(path, ".loop-engineer/state.json"))
    finally:
        mgr.remove(path)


def pathlib_read(root, rel):
    import os
    with open(os.path.join(root, rel), "r", encoding="utf-8") as fh:
        return fh.read()


def pathlib_exists(root, rel):
    import os
    return os.path.exists(os.path.join(root, rel))


def _git(repo, *args):
    """Run one git command inside *repo*, failing loudly."""
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _repo(tmp_path, name="repo"):
    """Create an initialised git repo with one empty commit."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-q", "--allow-empty", "-m", "init")
    return repo


def _commit(repo, rel, body):
    """Write and commit one file, so later edits show as MODIFICATIONS."""
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    _git(repo, "add", "--", rel)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-q", "-m", f"add {rel}")


def test_fingerprint_lists_untracked_and_modified_files_with_digests(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "tracked.py", "ORIGINAL = 1\n")
    (repo / "tracked.py").write_text("CHANGED = 2\n")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "new.py").write_text("NEW = 3\n")

    digests = Workspace(root=str(repo)).fingerprint()

    assert set(digests) == {"tracked.py", "pkg/new.py"}
    assert all(len(value) == 64 for value in digests.values())
    assert digests["tracked.py"] != digests["pkg/new.py"]


def test_fingerprint_excludes_engine_run_artifacts(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".loop-engineer").mkdir()
    (repo / ".loop-engineer" / "state.json").write_text("{}")
    (repo / "kept.py").write_text("KEPT = 1\n")

    assert set(Workspace(root=str(repo)).fingerprint()) == {"kept.py"}


def test_fingerprint_is_empty_on_a_clean_repo(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "tracked.py", "ORIGINAL = 1\n")
    assert Workspace(root=str(repo)).fingerprint() == {}


def test_fingerprint_skips_symlinks(tmp_path):
    repo = _repo(tmp_path)
    (repo / "real.py").write_text("REAL = 1\n")
    os.symlink(str(repo / "real.py"), str(repo / "link.py"))
    assert set(Workspace(root=str(repo)).fingerprint()) == {"real.py"}


def test_fingerprint_outside_a_git_repo_raises_workspace_error(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorkspaceError) as info:
        Workspace(root=str(plain)).fingerprint()
    assert "workspace status" in str(info.value)


def test_harvest_reports_only_files_changed_since_before(tmp_path):
    repo = _repo(tmp_path)
    # A pre-seeded untracked file the builder never touches: present in the
    # baseline with the SAME digest, so it must not be claimed as this
    # round's work.
    (repo / "seeded.py").write_text("SEEDED = 1\n")
    workspace = Workspace(root=str(repo))
    before = workspace.fingerprint()
    assert "seeded.py" in before

    (repo / "fresh.py").write_text("FRESH = 1\n")

    outcome = workspace.harvest(before)

    assert outcome.written == ["fresh.py"]
    assert outcome.rejected == []
    assert workspace.written == {"fresh.py"}


def test_harvest_reports_a_modified_pre_existing_file(tmp_path):
    repo = _repo(tmp_path)
    (repo / "seeded.py").write_text("SEEDED = 1\n")
    workspace = Workspace(root=str(repo))
    before = workspace.fingerprint()

    (repo / "seeded.py").write_text("SEEDED = 2\n")

    assert workspace.harvest(before).written == ["seeded.py"]


def test_harvest_reports_every_survivor_sorted(tmp_path):
    repo = _repo(tmp_path)
    workspace = Workspace(root=str(repo))
    before = workspace.fingerprint()
    for name in ("c.py", "a.py", "b.py"):
        (repo / name).write_text(f"X = '{name}'\n")

    assert workspace.harvest(before).written == ["a.py", "b.py", "c.py"]


@pytest.mark.parametrize("rel", [".env", "secrets/key.pem"])
def test_harvest_reverts_a_protected_untracked_path(tmp_path, rel):
    repo = _repo(tmp_path)
    workspace = Workspace(root=str(repo))
    before = workspace.fingerprint()

    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("SECRET=1\n")
    (repo / "ok.py").write_text("OK = 1\n")

    outcome = workspace.harvest(before)

    assert outcome.written == ["ok.py"]
    assert [path for path, _reason in outcome.rejected] == [rel]
    assert "protected pattern" in outcome.rejected[0][1]
    assert not target.exists(), "a protected file must be removed, not left on disk"


def test_harvest_restores_a_protected_tracked_file_from_git(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, ".gitignore", "build/\n")
    workspace = Workspace(root=str(repo))
    before = workspace.fingerprint()

    (repo / ".gitignore").write_text("EVERYTHING\n")

    outcome = workspace.harvest(before)

    assert outcome.written == []
    assert [path for path, _reason in outcome.rejected] == [".gitignore"]
    assert (repo / ".gitignore").read_text() == "build/\n"


def test_harvest_reverts_an_oversized_file(tmp_path):
    repo = _repo(tmp_path)
    workspace = Workspace(root=str(repo), policy=WritePolicy(max_file_bytes=64))
    before = workspace.fingerprint()

    (repo / "huge.py").write_text("x" * 200)
    (repo / "small.py").write_text("y" * 10)

    outcome = workspace.harvest(before)

    assert outcome.written == ["small.py"]
    assert [path for path, _reason in outcome.rejected] == ["huge.py"]
    assert "exceeds cap" in outcome.rejected[0][1]
    assert not (repo / "huge.py").exists()


def test_harvest_keeps_every_file_over_the_count_cap_but_records_it(tmp_path):
    repo = _repo(tmp_path)
    workspace = Workspace(root=str(repo), policy=WritePolicy(max_files_per_round=2))
    before = workspace.fingerprint()
    for name in ("a.py", "b.py", "c.py"):
        (repo / name).write_text(f"X = '{name}'\n")

    outcome = workspace.harvest(before)

    # Blast radius is the orchestrator's escalation call; reverting a subset
    # of one coherent change would leave the tree inconsistent.
    assert outcome.written == ["a.py", "b.py", "c.py"]
    assert all((repo / name).exists() for name in ("a.py", "b.py", "c.py"))
    assert outcome.rejected == [
        ("(extra files)", "exceeded max files per round: 3 > 2")]


def test_harvest_ignores_deletions(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "doomed.py", "DOOMED = 1\n")
    workspace = Workspace(root=str(repo))
    before = workspace.fingerprint()

    (repo / "doomed.py").unlink()

    assert workspace.harvest(before).written == []


def test_harvest_ignores_engine_run_artifacts(tmp_path):
    repo = _repo(tmp_path)
    workspace = Workspace(root=str(repo))
    before = workspace.fingerprint()

    (repo / ".loop-engineer").mkdir()
    (repo / ".loop-engineer" / "events.jsonl").write_text("{}\n")

    assert workspace.harvest(before).written == []


def test_harvest_accumulates_into_the_workspace_written_set(tmp_path):
    repo = _repo(tmp_path)
    workspace = Workspace(root=str(repo))
    before = workspace.fingerprint()

    (repo / "first.py").write_text("FIRST = 1\n")
    workspace.harvest(before)
    (repo / "second.py").write_text("SECOND = 1\n")
    workspace.harvest(before)

    assert workspace.written == {"first.py", "second.py"}


def _status(repo) -> str:
    """Return ``git status --porcelain`` for *repo*, exactly as the engine reads it."""
    return subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                          check=True, capture_output=True, text=True).stdout


def test_head_returns_sha(tmp_path):
    repo = _repo(tmp_path)
    expected = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()

    head = Workspace(root=str(repo)).head()

    assert head == expected
    assert len(head) == 40


def test_uncommit_to_restores_working_tree_changes(tmp_path):
    """A builder's commit becomes an ordinary working-tree change again."""
    repo = _repo(tmp_path)
    workspace = Workspace(root=str(repo))
    base = workspace.head()
    _commit(repo, "feature.py", "FEATURE = 1\n")

    undone = workspace.uncommit_to(base)

    assert undone == 1
    assert workspace.head() == base
    assert _status(repo).strip() == "?? feature.py"
    assert (repo / "feature.py").read_text() == "FEATURE = 1\n"


def test_uncommit_to_counts_every_commit_it_undid(tmp_path):
    repo = _repo(tmp_path)
    workspace = Workspace(root=str(repo))
    base = workspace.head()
    _commit(repo, "first.py", "FIRST = 1\n")
    _commit(repo, "second.py", "SECOND = 1\n")

    assert workspace.uncommit_to(base) == 2
    assert workspace.head() == base
    assert set(_status(repo).split()) == {"??", "first.py", "second.py"}


def test_uncommit_to_leaves_a_modified_tracked_file_modified(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "tracked.py", "ORIGINAL = 1\n")
    workspace = Workspace(root=str(repo))
    base = workspace.head()
    _commit(repo, "tracked.py", "CHANGED = 2\n")

    assert workspace.uncommit_to(base) == 1
    assert _status(repo).strip() == "M tracked.py"
    assert (repo / "tracked.py").read_text() == "CHANGED = 2\n"


def test_uncommit_to_noop_when_head_unchanged(tmp_path):
    repo = _repo(tmp_path)
    workspace = Workspace(root=str(repo))
    base = workspace.head()
    (repo / "edited.py").write_text("EDITED = 1\n")

    assert workspace.uncommit_to(base) == 0
    assert workspace.head() == base
    assert _status(repo).strip() == "?? edited.py"


def test_uncommit_to_raises_when_head_is_not_a_descendant(tmp_path):
    """A rebase or branch switch is never rewound on a guess."""
    repo = _repo(tmp_path)
    workspace = Workspace(root=str(repo))
    initial = workspace.head()
    _commit(repo, "abandoned.py", "ABANDONED = 1\n")
    base = workspace.head()
    _git(repo, "reset", "-q", "--hard", initial)

    with pytest.raises(WorkspaceError) as info:
        workspace.uncommit_to(base)

    assert "not a descendant" in str(info.value)
    assert workspace.head() == initial


def test_uncommit_to_makes_a_committed_change_visible_to_harvest(tmp_path):
    """The field failure: harvest reports what the builder committed."""
    repo = _repo(tmp_path)
    workspace = Workspace(root=str(repo))
    before = workspace.fingerprint()
    base = workspace.head()
    _commit(repo, "built.py", "BUILT = 1\n")

    assert workspace.harvest(before).written == []  # invisible while committed

    workspace.uncommit_to(base)

    assert workspace.harvest(before).written == ["built.py"]
