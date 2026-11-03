"""Diagnostic audit monitor: logs filesystem and subprocess events raised by engine code. Never enforces.

Load it with ``PYTHONPATH=tests python -m pytest -p audit_monitor --audit-log audit.jsonl`` (or set
``KICKASS_AUDIT_LOG``). Without a log path it installs nothing. The hook only observes: every failure
inside it is swallowed, so it cannot raise, block or change what the engine does. It is not a sandbox, and
its pass/fail is never guardrail evidence.

One JSON line per event: ``event``, ``args`` (first three, truncated; never the environment), ``file``
(repo-relative engine file), ``line`` and ``func`` of the nearest non-stdlib frame, which must be inside
``src/kickass_loop_engineer``. Events started by test code or third-party code are ignored.
"""

import json
import os
import sys
import threading

ENV_VAR = "KICKASS_AUDIT_LOG"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "src", "kickass_loop_engineer") + os.sep
STDLIB = os.path.dirname(os.__file__)
EVENTS = {"os.remove", "os.rename", "os.replace", "shutil.rmtree", "os.mkdir", "subprocess.Popen"}
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND

_path = None
_busy = threading.local()


def pytest_addoption(parser):
    parser.addoption("--audit-log", default=None, help="write the diagnostic audit log (JSON lines) here")


def pytest_configure(config):
    install(config.getoption("audit_log") or os.environ.get(ENV_VAR))


def install(path):
    """Start logging to *path* (no-op when empty). The hook cannot be removed, so install once."""
    global _path
    if not path:
        return
    first = _path is None
    _path = path
    if first:
        sys.addaudithook(_hook)


def _is_write(args):
    mode, flags = args[1], args[2]
    if isinstance(mode, str) and mode:
        return any(c in mode for c in "wax+")
    return isinstance(flags, int) and bool(flags & WRITE_FLAGS)


def _engine_frame(event):
    """Nearest non-stdlib frame as ``(relative file, line, function)``, or None when it is not engine code.

    Import-time bytecode writes and the steps inside one ``shutil.rmtree`` are not engine operations.
    """
    frame = sys._getframe(2)
    while frame is not None:
        name = os.path.abspath(frame.f_code.co_filename)
        func = frame.f_code.co_name
        if frame.f_code.co_filename.startswith("<frozen importlib"):
            return None
        if "rmtree" in func and event != "shutil.rmtree" and name.startswith(STDLIB):
            return None
        if frame.f_code.co_filename.startswith("<") or (
                name.startswith(STDLIB) and "site-packages" not in name):
            frame = frame.f_back
            continue
        if name.startswith(ENGINE):
            return os.path.relpath(name, ROOT).replace(os.sep, "/"), frame.f_lineno, frame.f_code.co_name
        return None
    return None


def _hook(event, args):
    if _path is None or getattr(_busy, "on", False):
        return
    _busy.on = True
    try:
        if event == "open":
            if not _is_write(args):
                return
        elif event not in EVENTS:
            return
        where = _engine_frame(event)
        if where is None:
            return
        line = json.dumps({"event": event, "args": [str(a)[:200] for a in args[:3]],
                           "file": where[0], "line": where[1], "func": where[2]})
        with open(_path, "a", encoding="utf-8") as log:
            log.write(line + "\n")
    except Exception:  # noqa: BLE001 - a diagnostic must never affect the code it watches
        pass
    finally:
        _busy.on = False
