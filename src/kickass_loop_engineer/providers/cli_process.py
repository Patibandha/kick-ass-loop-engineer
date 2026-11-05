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
#: Windows runs a ``.cmd``/``.bat`` program through ``cmd.exe``, which re-parses
#: the command line: these characters can expand variables or chain commands
#: even inside the quotes ``subprocess`` adds, so no argument may carry them.
_BATCH_SUFFIXES = (".cmd", ".bat")
_BATCH_UNSAFE = frozenset('%!^"&|<>\r\n')
_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"


class UnsafeBatchArgument(OSError):
    """An argument cmd.exe would re-interpret was bound for a ``.cmd``/``.bat`` shim."""


def find_program(name: str, env: Optional[dict] = None) -> str:
    """Return the full path of *name* on the child's PATH, or *name* when not found.

    A bare ``subprocess`` call cannot start an npm ``.cmd`` shim on Windows:
    CreateProcess does not apply PATHEXT, so ``claude`` raises
    ``FileNotFoundError`` although ``claude.CMD`` is on PATH. On Windows the
    lookup walks only absolute PATH folders with PATHEXT and never the current
    directory (``shutil.which`` checks it first there, so a ``claude.cmd``
    planted in a worktree would win). A name with a folder part is returned
    unchanged; an unresolved name is returned so the caller still sees
    ``FileNotFoundError``.

    Args:
        name: Program name or path (``argv[0]``).
        env: The child's environment (its PATH/PATHEXT are used); None uses ours.

    Returns:
        The resolved program path, or *name* unchanged.
    """
    env = os.environ if env is None else env
    if os.path.dirname(name):
        return name
    search = env.get("PATH", os.environ.get("PATH", ""))
    if os.name != "nt":
        return shutil.which(name, path=search) or name
    suffixes = [""] if os.path.splitext(name)[1] else env.get("PATHEXT", _DEFAULT_PATHEXT).split(";")
    for folder in search.split(os.pathsep):
        folder = folder.strip().strip('"')
        if not folder or not os.path.isabs(folder):
            continue
        for suffix in suffixes:
            candidate = os.path.join(folder, name + suffix)
            if os.path.isfile(candidate):
                return candidate
    return name


def resolve_argv(argv: list, env: Optional[dict] = None) -> list:
    """Return *argv* with its program resolved by :func:`find_program`.

    Never uses ``shell=True``. When the program is a Windows ``.cmd``/``.bat``
    shim (CreateProcess runs it via ``cmd.exe``), every argument is checked for
    characters cmd.exe would re-interpret, so no argument can inject a command.

    Raises:
        UnsafeBatchArgument: A shim would receive an argument holding one of
            ``% ! ^ " & | < >`` or a line break.
    """
    program = find_program(argv[0], env)
    if os.name == "nt" and program.lower().endswith(_BATCH_SUFFIXES):
        unsafe = [arg for arg in argv[1:] if _BATCH_UNSAFE.intersection(arg)]
        if unsafe:
            raise UnsafeBatchArgument(
                f"refusing to pass {len(unsafe)} argument(s) holding cmd.exe metacharacters "
                f"to the batch shim {program!r}; install the CLI's native executable or drop "
                f"the characters")
    return [program, *argv[1:]]


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
        UnsafeBatchArgument: A batch shim would receive a cmd.exe metacharacter.
        subprocess.TimeoutExpired: The cap was hit; the whole tree is killed first.
    """
    windows = os.name == "nt"
    program = resolve_argv(argv, env)
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
