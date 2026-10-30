"""Gate confinement (PA-2): run a gate command so it can write only inside its working folder.

On Windows the command runs as a separate low-integrity process. Windows' mandatory integrity control stops a
low-integrity process from writing to anything labelled medium (every file of the user's by default), so the
working folder and a private temp folder are labelled low for the run and every other path stays out of reach:
the main workspace, its run state and the engine's own source. ``<cwd>/.loop-engineer`` keeps an explicit medium
label, so a gate cannot write run state even in its own working folder. After the run the working folder is
labelled medium again and the private folder is removed.

Where the host has no such mechanism (Linux, macOS) the command runs as a plain subprocess and the caller is
told it ran unconfined, so the evidence can say so. On Windows a confinement step that fails raises ``OSError``:
the gate fails, it never falls back to an unconfined run. Network access is not changed.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import NamedTuple, Optional, Sequence

#: True only where gates can be confined (Windows, through integrity levels).
CONFINEMENT_AVAILABLE = os.name == "nt"

#: Run-state folder inside a working folder that a confined gate must never write.
RUN_STATE_DIR = ".loop-engineer"

_LOW_INTEGRITY_SID = "S-1-16-4096"


class ConfinedResult(NamedTuple):
    """Outcome of ``run_confined``: unpacks as ``(completed, confined)`` and exposes attributes."""

    completed: subprocess.CompletedProcess
    confined: bool

    @property
    def returncode(self) -> int:
        return self.completed.returncode

    @property
    def stdout(self) -> str:
        return self.completed.stdout

    @property
    def stderr(self) -> str:
        return self.completed.stderr


def _seatbelt_profile(cwd: str, scratch: str) -> str:
    """Generate an Apple Seatbelt (``sandbox-exec``) SBPL profile confining writes to *cwd* and *scratch*."""
    state_dir = f"{cwd.rstrip('/')}/{RUN_STATE_DIR}"
    return (
        "(version 1) "
        "(allow default) "
        "(deny network*) "
        '(deny file-write* (subpath "/")) '
        f'(allow file-write* (subpath "{cwd}")) '
        f'(allow file-write* (subpath "{scratch}")) '
        f'(deny file-write* (subpath "{state_dir}"))'
    )


def _bwrap_argv(argv: Sequence[str], cwd: str, scratch: str) -> list[str]:
    """Build the ``bwrap`` command line confining writes to *cwd* and *scratch*."""
    cmd: list[str] = [
        "bwrap",
        "--ro-bind", "/", "/",
        "--bind", cwd, cwd,
    ]
    state = os.path.join(cwd, RUN_STATE_DIR)
    if os.path.exists(state):
        cmd.extend(["--ro-bind", state, state])
    cmd.extend([
        "--bind", scratch, "/tmp",
        "--unshare-net",
        "--die-with-parent",
        "--chdir", cwd,
        *argv,
    ])
    return cmd


def _detect_posix_backend() -> Optional[str]:
    """Return ``'seatbelt'`` on macOS or ``'bwrap'`` on Linux when the binary is on PATH, else ``None``."""
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return "seatbelt"
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return "bwrap"
    return None


def _run_posix_confined(
    argv: Sequence[str],
    *,
    cwd: str,
    env: dict,
    timeout: float,
    backend: str,
) -> ConfinedResult:
    """Execute *argv* under ``sandbox-exec`` or ``bwrap`` while protecting ``.loop-engineer``."""
    private = tempfile.mkdtemp(prefix="le-confine-")
    state = os.path.join(cwd, RUN_STATE_DIR)
    state_existed = os.path.isdir(state)
    old_mode: Optional[int] = None
    try:
        scratch = os.path.join(private, "tmp")
        os.makedirs(os.path.join(scratch, "pycache"), exist_ok=True)
        if state_existed:
            try:
                old_mode = stat.S_IMODE(os.stat(state).st_mode)
                os.chmod(state, 0o555)
            except OSError:
                old_mode = None
        if backend == "seatbelt":
            profile = _seatbelt_profile(cwd, scratch)
            cmd = ["sandbox-exec", "-p", profile, *argv]
        elif backend == "bwrap":
            cmd = _bwrap_argv(argv, cwd, scratch)
        else:
            raise ConfinementError(f"unsupported POSIX confinement backend: {backend!r}")
        confined_env = dict(
            env,
            TEMP=scratch,
            TMP=scratch,
            TMPDIR=scratch,
            PYTHONPYCACHEPREFIX=os.path.join(scratch, "pycache"),
        )
        completed = subprocess.run(
            cmd,
            cwd=cwd,
            env=confined_env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return ConfinedResult(completed, True)
    finally:
        try:
            if state_existed and old_mode is not None:
                try:
                    os.chmod(state, old_mode)
                except OSError:
                    pass
        finally:
            shutil.rmtree(private, ignore_errors=True)


class ConfinementError(OSError):
    """A confinement step failed (labelling a folder, building the low-integrity token): the gate must fail."""


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


def _environment_block(env: dict) -> str:
    """A Unicode environment block for ``CreateProcessW``: sorted ``NAME=value`` entries, double-NUL ended."""
    entries = [f"{k}={v}" for k, v in sorted(env.items(), key=lambda item: item[0].upper())]
    return "\0".join(entries) + "\0\0"


def _win32():
    """The Win32 functions the launcher calls, each with ``argtypes``/``restype`` declared (64-bit needs them)."""
    import ctypes
    from ctypes import wintypes

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class TokenMandatoryLabel(ctypes.Structure):
        _fields_ = [("Label", SidAndAttributes)]

    class StartupInfo(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                    ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                    ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class ProcessInformation(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    def declare(fn, restype, *argtypes):
        fn.restype, fn.argtypes = restype, list(argtypes)
        return fn

    handle_p = ctypes.POINTER(wintypes.HANDLE)
    api = {
        "GetCurrentProcess": declare(kernel32.GetCurrentProcess, wintypes.HANDLE),
        "CloseHandle": declare(kernel32.CloseHandle, wintypes.BOOL, wintypes.HANDLE),
        "LocalFree": declare(kernel32.LocalFree, ctypes.c_void_p, ctypes.c_void_p),
        "WaitForSingleObject": declare(kernel32.WaitForSingleObject, wintypes.DWORD,
                                       wintypes.HANDLE, wintypes.DWORD),
        "GetExitCodeProcess": declare(kernel32.GetExitCodeProcess, wintypes.BOOL,
                                      wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)),
        "TerminateProcess": declare(kernel32.TerminateProcess, wintypes.BOOL, wintypes.HANDLE, wintypes.UINT),
        "OpenProcessToken": declare(advapi32.OpenProcessToken, wintypes.BOOL,
                                    wintypes.HANDLE, wintypes.DWORD, handle_p),
        "DuplicateTokenEx": declare(advapi32.DuplicateTokenEx, wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD,
                                    ctypes.c_void_p, ctypes.c_int, ctypes.c_int, handle_p),
        "ConvertStringSidToSidW": declare(advapi32.ConvertStringSidToSidW, wintypes.BOOL,
                                          wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)),
        "GetLengthSid": declare(advapi32.GetLengthSid, wintypes.DWORD, ctypes.c_void_p),
        "SetTokenInformation": declare(advapi32.SetTokenInformation, wintypes.BOOL, wintypes.HANDLE,
                                       ctypes.c_int, ctypes.c_void_p, wintypes.DWORD),
        "CreateProcessAsUserW": declare(advapi32.CreateProcessAsUserW, wintypes.BOOL, wintypes.HANDLE,
                                        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                        wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                                        ctypes.POINTER(StartupInfo), ctypes.POINTER(ProcessInformation)),
    }
    types = {"SidAndAttributes": SidAndAttributes, "TokenMandatoryLabel": TokenMandatoryLabel,
             "StartupInfo": StartupInfo, "ProcessInformation": ProcessInformation}
    return ctypes, wintypes, api, types


def _failed(ctypes, call: str, error=OSError) -> OSError:
    """An *error* naming the Win32 *call* that failed and its last error (``OSError`` maps it to a subclass)."""
    code = ctypes.get_last_error()
    return error(code, f"{call} failed: {ctypes.FormatError(code).strip()}")


def _low_integrity_token(ctypes, wintypes, api, types):
    """A primary copy of this process's token with its integrity level set to low.

    Raises:
        ConfinementError: When any token call fails.
    """
    token_duplicate, token_query, token_adjust_default, token_assign_primary = 0x0002, 0x0008, 0x0080, 0x0001
    security_impersonation, token_primary, token_integrity_level, se_group_integrity = 2, 1, 25, 0x20
    own = wintypes.HANDLE()
    access = token_duplicate | token_query | token_adjust_default | token_assign_primary
    if not api["OpenProcessToken"](api["GetCurrentProcess"](), access, ctypes.byref(own)):
        raise _failed(ctypes, "OpenProcessToken", ConfinementError)
    low = wintypes.HANDLE()
    sid = ctypes.c_void_p()
    try:
        if not api["DuplicateTokenEx"](own, 0, None, security_impersonation, token_primary, ctypes.byref(low)):
            raise _failed(ctypes, "DuplicateTokenEx", ConfinementError)
        if not api["ConvertStringSidToSidW"](_LOW_INTEGRITY_SID, ctypes.byref(sid)):
            raise _failed(ctypes, "ConvertStringSidToSidW", ConfinementError)
        label = types["TokenMandatoryLabel"]()
        label.Label.Sid, label.Label.Attributes = sid.value, se_group_integrity
        size = ctypes.sizeof(label) + api["GetLengthSid"](sid)
        if not api["SetTokenInformation"](low, token_integrity_level, ctypes.byref(label), size):
            raise _failed(ctypes, "SetTokenInformation", ConfinementError)
    except OSError:
        if low.value:
            api["CloseHandle"](low)
        raise
    finally:
        if sid.value:
            api["LocalFree"](sid)
        api["CloseHandle"](own)
    return low


def _inheritable_handle(stream) -> int:
    """The OS handle behind an open file, made inheritable so the child can write to it."""
    import msvcrt

    handle = msvcrt.get_osfhandle(stream.fileno())
    os.set_handle_inheritable(handle, True)
    return handle


def _run_low(argv: list, cwd: str, env: dict, timeout: float, private: str) -> subprocess.CompletedProcess:
    """Launch *argv* at low integrity with its output in files under *private*; wait, read and return it.

    Raises:
        subprocess.TimeoutExpired: When the process outlives *timeout* (it is terminated first).
        ConfinementError: When the low-integrity token cannot be built.
        OSError: When the process cannot be created or waited on.
    """
    ctypes, wintypes, api, types = _win32()
    startf_usestdhandles, create_no_window, create_unicode_environment = 0x100, 0x08000000, 0x400
    wait_object_0, wait_timeout = 0x0, 0x102
    command_line = subprocess.list2cmdline(argv)
    out_path, err_path = os.path.join(private, "stdout"), os.path.join(private, "stderr")
    token = _low_integrity_token(ctypes, wintypes, api, types)
    try:
        with open(out_path, "wb") as out, open(err_path, "wb") as err, open(os.devnull, "rb") as null:
            info = types["StartupInfo"]()
            info.cb = ctypes.sizeof(info)
            info.dwFlags = startf_usestdhandles
            info.hStdInput = _inheritable_handle(null)
            info.hStdOutput = _inheritable_handle(out)
            info.hStdError = _inheritable_handle(err)
            process = types["ProcessInformation"]()
            block = ctypes.create_unicode_buffer(_environment_block(env))
            buffer = ctypes.create_unicode_buffer(command_line)
            if not api["CreateProcessAsUserW"](token, None, buffer, None, None, True,
                                               create_no_window | create_unicode_environment,
                                               ctypes.cast(block, ctypes.c_void_p), cwd,
                                               ctypes.byref(info), ctypes.byref(process)):
                raise _failed(ctypes, "CreateProcessAsUserW")
        try:
            waited = api["WaitForSingleObject"](process.hProcess, max(0, int(timeout * 1000)))
            if waited == wait_timeout:
                api["TerminateProcess"](process.hProcess, 1)
                api["WaitForSingleObject"](process.hProcess, 10_000)
                raise subprocess.TimeoutExpired(argv, timeout)
            if waited != wait_object_0:
                raise _failed(ctypes, "WaitForSingleObject")
            code = wintypes.DWORD()
            if not api["GetExitCodeProcess"](process.hProcess, ctypes.byref(code)):
                raise _failed(ctypes, "GetExitCodeProcess")
        finally:
            api["CloseHandle"](process.hThread)
            api["CloseHandle"](process.hProcess)
    finally:
        api["CloseHandle"](token)
    returncode = ctypes.c_int32(code.value).value
    # Decoded as ``subprocess.run(text=True)`` would: the locale encoding, universal newlines.
    with open(out_path, errors="replace") as out, open(err_path, errors="replace") as err:
        return subprocess.CompletedProcess(argv, returncode, out.read(), err.read())


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
        ConfinementError: (Windows) When any confinement step fails; the command is never run unconfined.
        OSError: When the command cannot start.
    """
    if not CONFINEMENT_AVAILABLE:
        completed = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                                   timeout=timeout, check=False)
        return completed, False

    posix_backend = _detect_posix_backend()
    if posix_backend is not None:
        return _run_posix_confined(argv, cwd=cwd, env=env, timeout=timeout, backend=posix_backend)

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
        return ConfinedResult(_run_low(argv, cwd, confined_env, timeout, private), True)
    finally:
        try:
            if labelled:
                _label(cwd, "medium")
        finally:
            shutil.rmtree(private, ignore_errors=True)
