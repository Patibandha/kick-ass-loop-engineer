"""Gate confinement (PA-2): run a gate command so it can write only inside its working folder.

On Windows the command runs as a separate low-integrity process. Windows' mandatory integrity control stops a
low-integrity process from writing to anything labelled medium (every file of the user's by default), so the
working folder and a private temp folder are labelled low for the run and every other path stays out of reach:
the engine's own source, and the workspace and its run state when the gate runs elsewhere. A gate run in a
folder can write all of it, ``.git`` included (``loop-engineer verify --workspace`` does that, by request). ``<cwd>/.loop-engineer`` keeps an explicit medium
label, which stops a direct write but not a rename: the parent is low, so a gate can move the folder aside and
recreate it. Run state is therefore safe only because the orchestrator never confines a gate in the workspace that
holds it (attempt, final, fix-mode repro and audit gates run in throwaway worktrees). Every process the gate
starts runs in a kill-on-close Job Object and is killed before the folder is relabelled medium; the private folder
is then removed. A worktree's ``.git`` pointer is put back if the gate changed it, and the folder is quarantined if
it cannot be. These checks are what keep the engine's own git calls out of the gate's control; they mean nothing on
an unconfined host, where the gate can already write anywhere.

Linux and macOS are NOT confined: there the command runs as a plain subprocess and the caller is told it ran
unconfined, so the evidence says so. On Windows a confinement step that fails raises ``OSError``:
the gate fails, it never falls back to an unconfined run. Network access is not changed.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Optional

from .low_integrity import ConfinementError, run_low

#: True only where gates can be confined (Windows, through integrity levels).
CONFINEMENT_AVAILABLE = os.name == "nt"

#: Run-state folder inside a working folder that a confined gate must never write.
RUN_STATE_DIR = ".loop-engineer"

def _label(path: str, level: str) -> None:
    """Give *path* (a folder) an inheritable ``level`` integrity label with ``icacls``.

    Raises:
        ConfinementError: When ``icacls`` cannot run or reports a failure.
    """
    try:
        done = subprocess.run(["icacls", path, "/setintegritylevel", f"(OI)(CI){level}"],
                              capture_output=True, text=True, timeout=300, check=False,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfinementError(f"icacls could not label {path} {level}: {exc}") from exc
    if done.returncode != 0:
        detail = " ".join(((done.stdout or "") + (done.stderr or "")).split())[-300:]
        raise ConfinementError(f"icacls could not label {path} {level}: {detail}")


def _git_pointer(cwd: str) -> Optional[bytes]:
    """The bytes of ``<cwd>/.git`` when it is a plain file (a linked worktree's pointer), else ``None``."""
    path = os.path.join(cwd, ".git")
    if os.path.islink(path) or not os.path.isfile(path):
        return None
    with open(path, "rb") as handle:
        return handle.read()


def _write_pointer(path: str, pointer: bytes) -> None:
    """Replace whatever the gate left at *path* (a read-only file, a folder, a junction) with *pointer*."""
    if getattr(os.path, "isjunction", lambda _path: False)(path):  # Python 3.12+
        os.rmdir(path)
    elif os.path.isdir(path) and not os.path.islink(path):
        for root, _dirs, files in os.walk(path):
            for name in files:
                os.chmod(os.path.join(root, name), stat.S_IWRITE)
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.chmod(path, stat.S_IWRITE)  # a read-only file refuses os.remove on Windows
        os.remove(path)
    with open(path, "wb") as handle:
        handle.write(pointer)


def _restore_git_pointer(cwd: str, pointer: bytes) -> bool:
    """Put *pointer* back as ``<cwd>/.git`` if the gate changed it; True when it had to.

    The pointer sits inside the gate's writable folder. Redirected to a gitdir the gate built, the engine's next
    git call there would read that gitdir's config at the engine's own integrity. When it cannot be put back, the
    folder is quarantined (renamed aside) so no engine git call ever runs in it again.

    Raises:
        ConfinementError: When the pointer cannot be read or put back (after quarantining the folder).
    """
    path = os.path.join(cwd, ".git")
    try:
        if _git_pointer(cwd) == pointer:
            return False
        _write_pointer(path, pointer)
        if _git_pointer(cwd) == pointer:
            return True
        error: Exception = OSError("the restored pointer does not match")
    except OSError as exc:
        error = exc
    quarantine = f"{cwd}.quarantined-{os.getpid()}-{time.monotonic_ns()}"
    try:
        os.rename(cwd, quarantine)
    except OSError as exc:
        raise ConfinementError(f"gate changed {path}, it could not be restored ({error}) and {cwd} could not be "
                               f"quarantined ({exc}); never run git there") from exc
    raise ConfinementError(f"gate changed {path} and it could not be restored ({error}); quarantined the folder "
                           f"as {quarantine}") from error


def run_confined(argv: list, *, cwd: str, env: dict,
                 timeout: float) -> tuple[subprocess.CompletedProcess, bool]:
    """Run *argv* in *cwd* confined where the host allows it.

    Args:
        argv: The program and its arguments (the program already resolved).
        cwd: The working folder, the only user folder the command may write.
        env: The command's environment; ``TEMP``, ``TMP`` and ``PYTHONPYCACHEPREFIX`` are pointed at a private
            folder when confined.
        timeout: Seconds before the command is killed.

    Returns:
        The completed process (text stdout and stderr) and whether it ran confined.

    Raises:
        subprocess.TimeoutExpired: When the command outlives *timeout*.
        ConfinementError: When the command rewrote a worktree's ``.git`` file (put back first), or (Windows)
            when any confinement step fails; the command is never run unconfined.
        OSError: When the command cannot start.
    """
    pointer = _git_pointer(cwd)
    try:
        result = _run(argv, cwd=cwd, env=env, timeout=timeout)
    finally:
        changed = pointer is not None and _restore_git_pointer(cwd, pointer)
    if changed:
        raise ConfinementError(f"gate rewrote {os.path.join(cwd, '.git')}; restored it and failed the gate")
    return result


def _run(argv: list, *, cwd: str, env: dict, timeout: float) -> tuple[subprocess.CompletedProcess, bool]:
    """``run_confined`` without the ``.git`` pointer check."""
    if not CONFINEMENT_AVAILABLE:
        completed = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                                   timeout=timeout, check=False)
        return completed, False

    private = tempfile.mkdtemp(prefix="le-confine-")
    labelled = False
    try:
        scratch = os.path.join(private, "tmp")
        os.makedirs(os.path.join(scratch, "pycache"))
        _label(scratch, "low")
        labelled = True
        _label(cwd, "low")
        state = os.path.join(cwd, RUN_STATE_DIR)
        if os.path.isdir(state):
            _label(state, "medium")
        confined_env = dict(env, TEMP=scratch, TMP=scratch, PYTHONPYCACHEPREFIX=os.path.join(scratch, "pycache"))
        return run_low(argv, cwd, confined_env, timeout, private), True
    finally:
        try:
            if labelled:
                _label(cwd, "medium")
        finally:
            shutil.rmtree(private, ignore_errors=True)
