"""Run a subscription CLI with a timeout that kills the WHOLE process tree.

``subprocess.run(timeout=...)`` only kills the direct child. On Windows the
direct child of an npm-installed CLI (``codex``) is a ``.cmd`` shim's
``cmd.exe``; killing it leaves the real ``node`` process running and holding
the output pipes, so ``communicate`` waits forever. This runner starts the CLI
in its own process group and, on timeout, kills the tree (``taskkill /T /F``
on Windows, ``killpg`` on POSIX) before re-raising ``TimeoutExpired``.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from typing import Optional

#: Windows creation flags: no console window, own process group.
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200
#: Seconds allowed for the kill and the final reap.
_KILL_TIMEOUT_S = 10


def run_cli(argv: list, *, stdin: str, env: dict, cwd: Optional[str],
            timeout: float) -> subprocess.CompletedProcess:
    """Run *argv* with *stdin* (UTF-8) and return the completed process.

    Args:
        argv: Program and arguments; the program is resolved on PATH so
            Windows ``.cmd``/``.bat`` shims run.
        stdin: Text written to the child's stdin (prompts never go in argv).
        env: Full environment for the child.
        cwd: Working directory, or None to inherit.
        timeout: Wall-clock cap in seconds.

    Returns:
        The ``CompletedProcess`` with decoded stdout/stderr.

    Raises:
        FileNotFoundError: The program or *cwd* does not exist.
        subprocess.TimeoutExpired: The cap was hit; the whole tree is killed first.
    """
    windows = os.name == "nt"
    program = [shutil.which(argv[0]) or argv[0], *argv[1:]]
    proc = subprocess.Popen(
        program, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", env=env, cwd=cwd,
        start_new_session=not windows,
        creationflags=(_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP) if windows else 0)
    try:
        stdout, stderr = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        raise
    return subprocess.CompletedProcess(program, proc.returncode, stdout, stderr)


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill *proc* and every descendant, then reap it (best effort, never raises)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                           timeout=_KILL_TIMEOUT_S, creationflags=_CREATE_NO_WINDOW)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate(timeout=_KILL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        proc.kill()
