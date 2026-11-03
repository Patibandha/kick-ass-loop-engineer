"""A gate must never steer the engine's own git calls: each test is a reproduced escape that once ran gate code
at the engine's integrity (a marker file written outside the gate's reach)."""

import os
import subprocess
import sys


import pytest

from kickass_loop_engineer import confine, verifier
from kickass_loop_engineer.workspace import changed_paths

pytestmark = pytest.mark.guardrail_gate_confinement

EVIL_GITDIR = r"""
import os, stat, subprocess, sys, time
marker = sys.argv[1].replace("\\", "/")
HOOK = "#!/bin/sh\necho pwned > '" + marker + "'\nexit 1\n"

def plant_hook(folder):
    hook = os.path.abspath(os.path.join(folder, "fsm.sh")).replace("\\", "/")
    with open(hook, "w", newline="\n") as f:
        f.write(HOOK)
    os.chmod(hook, 0o755)
    return hook

def evil_gitdir():
    subprocess.run(["git", "init", "--bare", "-q", "evil.git"], check=True)
    with open(os.path.join("evil.git", "config"), "a") as f:
        f.write("[core]\n\tbare = false\n\tfsmonitor = " + plant_hook("evil.git") + "\n")
"""

READ_ONLY_POINTER = EVIL_GITDIR + r"""
evil_gitdir()
os.remove(".git")
with open(".git", "w") as f:
    f.write("gitdir: evil.git\n")
os.chmod(".git", stat.S_IREAD)
"""

LINGERING_CHILD = EVIL_GITDIR + r"""
if sys.argv[2:] == ["child"]:
    handle = open(".git", "r+b")  # opened while the folder is still writable
    evil_gitdir()
    open("ready", "w").close()
    time.sleep(5)  # the gate has exited and the engine has checked the pointer by now
    handle.seek(0); handle.write(b"gitdir: evil.git\n"); handle.truncate(); handle.close()
    sys.exit(0)
subprocess.Popen([sys.executable, __file__, sys.argv[1], "child"], creationflags=0x8, close_fds=True,
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
while not os.path.exists("ready"):
    time.sleep(0.05)
"""

SUBMODULE_CONFIG = EVIL_GITDIR + r"""
subprocess.run(["git", "init", "-q", "sub"], check=True)
with open(os.path.join("sub", ".git", "config"), "a") as f:
    f.write("[core]\n\tfsmonitor = " + plant_hook("sub") + "\n")
"""


def _git(cwd, *args):
    subprocess.run(["git", "-c", "protocol.file.allow=always", *args], cwd=cwd, check=True, capture_output=True)


def _repo(path):
    path.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        _git(path, *args)
    (path / "a.txt").write_text("a\n")
    return path


def _worktree(tmp_path, with_submodule=False):
    repo = _repo(tmp_path / "repo")
    if with_submodule:
        lib = _repo(tmp_path / "lib")
        _git(lib, "add", "-A")
        _git(lib, "commit", "-q", "-m", "l")
        _git(repo, "submodule", "add", "-q", str(lib), "sub")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "i")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt))
    return wt


def _gate(tmp_path, wt, script, marker):
    payload = tmp_path / "payload.py"
    payload.write_text(script, encoding="utf-8")
    return confine.run_confined([sys.executable, str(payload), str(marker)], cwd=str(wt),
                                env=dict(os.environ), timeout=60)


def test_a_read_only_redirected_pointer_is_still_put_back(tmp_path):
    wt, marker = _worktree(tmp_path), tmp_path / "PWNED.txt"
    pointer = (wt / ".git").read_bytes()

    with pytest.raises(confine.ConfinementError, match=r"\.git"):
        _gate(tmp_path, wt, READ_ONLY_POINTER, marker)

    assert (wt / ".git").read_bytes() == pointer
    changed_paths(str(wt))
    assert not marker.exists()


def test_a_worktree_whose_pointer_cannot_be_restored_is_quarantined(tmp_path, monkeypatch):
    wt, marker = _worktree(tmp_path), tmp_path / "PWNED.txt"
    monkeypatch.setattr(confine, "_write_pointer", lambda *_a: (_ for _ in ()).throw(PermissionError("denied")))

    with pytest.raises(confine.ConfinementError, match="quarantined"):
        _gate(tmp_path, wt, READ_ONLY_POINTER, marker)

    assert not wt.exists(), "git must never run in that folder again"
    with pytest.raises((subprocess.CalledProcessError, OSError)):
        changed_paths(str(wt))
    assert not marker.exists()


def test_a_process_the_gate_leaves_behind_dies_with_the_gate(tmp_path):
    if not confine.CONFINEMENT_AVAILABLE:
        # Unconfined hosts run a gate as a plain subprocess; nothing contains what it starts.
        assert _gate(tmp_path, _worktree(tmp_path), "print(1)", tmp_path / "m")[1] is False
        return
    wt, marker = _worktree(tmp_path), tmp_path / "PWNED.txt"
    pointer = (wt / ".git").read_bytes()

    _gate(tmp_path, wt, LINGERING_CHILD, marker)
    import time
    time.sleep(7)  # past the moment the lingering child would rewrite the pointer

    assert (wt / ".git").read_bytes() == pointer
    changed_paths(str(wt))
    assert not marker.exists()


def test_a_submodule_the_gate_populates_never_runs_its_config(tmp_path):
    wt, marker = _worktree(tmp_path, with_submodule=True), tmp_path / "PWNED.txt"

    _gate(tmp_path, wt, SUBMODULE_CONFIG, marker)
    changed_paths(str(wt))

    assert not marker.exists()


def test_proof_status_never_enters_a_submodule_even_if_gitmodules_asks(tmp_path):
    wt = _worktree(tmp_path, with_submodule=True)
    # .gitmodules `ignore = none` beats the engine's diff.ignoreSubmodules config; only the flag beats it.
    _git(wt, "config", "-f", ".gitmodules", "submodule.sub.ignore", "none")
    _git(tmp_path, "clone", "-q", str(tmp_path / "lib"), str(wt / "sub"))
    (wt / "sub" / "a.txt").write_text("moved\n")
    _git(wt / "sub", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-am", "moved")
    paths = [path for _, path in verifier._changed_entries(str(wt))]
    assert paths == [".gitmodules"]
