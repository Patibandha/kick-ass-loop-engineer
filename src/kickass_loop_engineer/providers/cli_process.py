"""Run a subscription CLI with a timeout that kills the WHOLE process tree.

``subprocess.run(timeout=...)`` only kills the direct child. On Windows the
direct child of an npm-installed CLI (``codex``) is a ``.cmd`` shim's
``cmd.exe``; killing it leaves the real ``node`` process running and holding
the output pipes, so ``communicate`` waits forever. This runner starts the CLI
in its own process group and, on timeout, kills the tree (``taskkill /T /F``
on Windows, ``killpg`` on POSIX) before re-raising ``TimeoutExpired``.
"""

from __future__ import annotations

import errno
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
#: the command line: these characters expand variables or chain/redirect
#: commands, and since a ``"`` in an argument can flip cmd's quote state, none
#: of them may appear anywhere. A ``"`` alone only moves quoting, so it is
#: allowed (codex's ``-c windows.sandbox="unelevated"`` needs it).
_BATCH_SUFFIXES = (".cmd", ".bat")
_BATCH_UNSAFE = frozenset("%!^&|<>\r\n")
_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"


class UnsafeBatchArgument(OSError):
    """An argument cmd.exe would re-interpret was bound for a ``.cmd``/``.bat`` shim."""


def _env_value(env: dict, name: str) -> str:
    """Return *name* from *env* ("" when absent); Windows variable names are case-insensitive."""
    if name in env:
        return env[name]
    if os.name == "nt":
        return next((value for key, value in env.items() if key.upper() == name), "")
    return ""


def _search_folders(env: dict) -> list:
    """The child's PATH folders that cannot point into the current directory.

    Empty, ``.`` and relative entries resolve against the parent's cwd, and on
    Windows a rooted path without a drive letter (``/bin``) against its current drive.
    """
    folders = []
    for folder in _env_value(env, "PATH").split(os.pathsep):
        folder = folder.strip().strip('"')
        if folder and os.path.isabs(folder) and (os.name != "nt" or os.path.splitdrive(folder)[0]):
            folders.append(folder)
    return folders


def _find_on_windows(name: str, folders: list, env: dict) -> Optional[str]:
    """Walk *folders* with PATHEXT (never the current directory, unlike ``shutil.which``)."""
    pathext = _env_value(env, "PATHEXT") or _DEFAULT_PATHEXT
    has_suffix = bool(os.path.splitext(name)[1])
    # An empty suffix would match npm's extensionless ``#!/bin/sh`` shim, which CreateProcess cannot start.
    suffixes = [""] if has_suffix else [s.strip() for s in pathext.split(";") if s.strip()]
    for folder in folders:
        for suffix in suffixes:
            candidate = os.path.join(folder, name + suffix)
            if os.path.isfile(candidate):
                return candidate
    return None


def find_program(name: str, env: Optional[dict] = None) -> str:
    """Return the full path of *name* on the child's PATH.

    A bare ``subprocess`` call cannot start an npm ``.cmd`` shim on Windows:
    CreateProcess does not apply PATHEXT, so ``claude`` raises
    ``FileNotFoundError`` although ``claude.CMD`` is on PATH. The lookup uses
    only the child's absolute PATH folders and never the current directory
    (``shutil.which`` checks it first on Windows, and CreateProcess searches it
    for a bare name), so a ``claude.cmd`` or ``claude.exe`` planted in a
    worktree cannot win. A name with a folder part is returned unchanged.

    Args:
        name: Program name or path (``argv[0]``).
        env: The child's environment (its PATH/PATHEXT are used); None uses ours.

    Returns:
        The resolved program path.

    Raises:
        FileNotFoundError: *name* is not in any absolute folder on the child's PATH.
    """
    env = os.environ if env is None else env
    if os.path.dirname(name):
        return name
    folders = _search_folders(env)
    if os.name == "nt":
        found = _find_on_windows(name, folders, env)
    else:
        found = shutil.which(name, path=os.pathsep.join(folders)) if folders else None
    if not found:
        raise FileNotFoundError(errno.ENOENT, f"{name!r} is not in any absolute folder on PATH", name)
    return found


def resolve_argv(argv: list, env: Optional[dict] = None) -> list:
    """Return *argv* with its program resolved by :func:`find_program`.

    Never uses ``shell=True``. When the program is a Windows ``.cmd``/``.bat``
    shim (CreateProcess runs it via ``cmd.exe``), its path and every argument
    are checked for characters cmd.exe would re-interpret, so nothing can
    inject a command.

    Raises:
        FileNotFoundError: The program is not on the child's PATH.
        UnsafeBatchArgument: A shim's path or argument holds one of
            ``% ! ^ & | < >`` or a line break.
    """
    program = find_program(argv[0], env)
    # Win32 drops trailing dots and spaces, so "claude.cmd." still runs through cmd.exe.
    if os.name == "nt" and program.rstrip(" .").lower().endswith(_BATCH_SUFFIXES):
        unsafe = [arg for arg in [program, *argv[1:]] if _BATCH_UNSAFE.intersection(arg)]
        if unsafe:
            raise UnsafeBatchArgument(
                f"refusing to start the batch shim {os.path.basename(program)!r}: {len(unsafe)} "
                f"value(s) in its path or arguments hold cmd.exe metacharacters; install the "
                f"CLI's native executable or drop the characters")
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
