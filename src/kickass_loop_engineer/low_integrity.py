"""Launch a command as a low-integrity Windows process inside a kill-on-close Job Object (see ``confine``).

The job holds every process the command starts, however it detaches: when the command exits (or times out) the
whole job is killed and waited for, so nothing the gate started outlives the gate and its folder's relabel.
"""

from __future__ import annotations

import os
import subprocess
import time

_LOW_INTEGRITY_SID = "S-1-16-4096"


class ConfinementError(OSError):
    """A confinement step failed (labelling a folder, building the low-integrity token): the gate must fail."""


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
        "ResumeThread": declare(kernel32.ResumeThread, wintypes.DWORD, wintypes.HANDLE),
        "CreateJobObjectW": declare(kernel32.CreateJobObjectW, wintypes.HANDLE, ctypes.c_void_p, wintypes.LPCWSTR),
        "SetInformationJobObject": declare(kernel32.SetInformationJobObject, wintypes.BOOL, wintypes.HANDLE,
                                           ctypes.c_int, ctypes.c_void_p, wintypes.DWORD),
        "QueryInformationJobObject": declare(kernel32.QueryInformationJobObject, wintypes.BOOL, wintypes.HANDLE,
                                             ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p),
        "AssignProcessToJobObject": declare(kernel32.AssignProcessToJobObject, wintypes.BOOL,
                                            wintypes.HANDLE, wintypes.HANDLE),
        "TerminateJobObject": declare(kernel32.TerminateJobObject, wintypes.BOOL, wintypes.HANDLE, wintypes.UINT),
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


#: How long the engine waits for a killed job's processes to be gone before failing the gate.
_JOB_EXIT_SECONDS = 10


def _kill_on_close_job(ctypes, wintypes, api):
    """A Job Object whose processes all die when it is terminated or its last handle closes.

    Raises:
        ConfinementError: When the job cannot be created or configured.
    """
    class BasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    extended_limit_information, kill_on_job_close = 9, 0x2000
    job = api["CreateJobObjectW"](None, None)
    if not job:
        raise _failed(ctypes, "CreateJobObjectW", ConfinementError)
    limits = ExtendedLimit()
    limits.BasicLimitInformation.LimitFlags = kill_on_job_close
    if not api["SetInformationJobObject"](job, extended_limit_information, ctypes.byref(limits),
                                          ctypes.sizeof(limits)):
        api["CloseHandle"](job)
        raise _failed(ctypes, "SetInformationJobObject", ConfinementError)
    return job


def _end_job(ctypes, wintypes, api, job) -> None:
    """Kill every process in *job*, wait until none is left, and close it.

    Raises:
        ConfinementError: When a process in the job is still alive after ``_JOB_EXIT_SECONDS``.
    """
    class Accounting(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                    ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                    ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

    basic_accounting_information = 1
    try:
        api["TerminateJobObject"](job, 1)
        deadline = time.monotonic() + _JOB_EXIT_SECONDS
        info = Accounting()
        while True:
            if not api["QueryInformationJobObject"](job, basic_accounting_information, ctypes.byref(info),
                                                    ctypes.sizeof(info), None):
                raise _failed(ctypes, "QueryInformationJobObject", ConfinementError)
            if info.ActiveProcesses == 0:
                return
            if time.monotonic() > deadline:
                raise ConfinementError(f"{info.ActiveProcesses} gate process(es) survived the kill")
            time.sleep(0.05)
    finally:
        api["CloseHandle"](job)


def _inheritable_handle(stream) -> int:
    """The OS handle behind an open file, made inheritable so the child can write to it."""
    import msvcrt

    handle = msvcrt.get_osfhandle(stream.fileno())
    os.set_handle_inheritable(handle, True)
    return handle


def run_low(argv: list, cwd: str, env: dict, timeout: float, private: str) -> subprocess.CompletedProcess:
    """Launch *argv* at low integrity with its output in files under *private*; wait, read and return it.

    Raises:
        subprocess.TimeoutExpired: When the process outlives *timeout* (its whole job is killed first).
        ConfinementError: When the low-integrity token or the job cannot be built, or a job process survives.
        OSError: When the process cannot be created or waited on.
    """
    ctypes, wintypes, api, types = _win32()
    startf_usestdhandles, create_no_window, create_unicode_environment = 0x100, 0x08000000, 0x400
    create_suspended = 0x4
    wait_object_0, wait_timeout = 0x0, 0x102
    command_line = subprocess.list2cmdline(argv)
    out_path, err_path = os.path.join(private, "stdout"), os.path.join(private, "stderr")
    token = _low_integrity_token(ctypes, wintypes, api, types)
    job = None
    try:
        job = _kill_on_close_job(ctypes, wintypes, api)
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
                                               create_no_window | create_unicode_environment | create_suspended,
                                               ctypes.cast(block, ctypes.c_void_p), cwd,
                                               ctypes.byref(info), ctypes.byref(process)):
                raise _failed(ctypes, "CreateProcessAsUserW")
        try:
            # Suspended until it is in the job, so nothing it starts can escape the kill below.
            if not api["AssignProcessToJobObject"](job, process.hProcess):
                api["TerminateProcess"](process.hProcess, 1)
                raise _failed(ctypes, "AssignProcessToJobObject", ConfinementError)
            api["ResumeThread"](process.hThread)
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
        try:
            if job is not None:
                _end_job(ctypes, wintypes, api, job)  # before the caller relabels the folder
        finally:
            api["CloseHandle"](token)
    returncode = ctypes.c_int32(code.value).value
    # Decoded as ``subprocess.run(text=True)`` would: the locale encoding, universal newlines.
    with open(out_path, errors="replace") as out, open(err_path, errors="replace") as err:
        return subprocess.CompletedProcess(argv, returncode, out.read(), err.read())
