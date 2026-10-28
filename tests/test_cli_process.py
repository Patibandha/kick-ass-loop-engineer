"""Tests for the kill-tree CLI runner shared by subscription-CLI providers."""

import os
import subprocess
import sys
import textwrap
import time

import pytest

from kickass_loop_engineer.providers.cli_process import run_cli


def _python(code):
    return [sys.executable, "-c", textwrap.dedent(code)]


def test_returns_output_and_exit_code():
    result = run_cli(_python("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"),
                     stdin="", env=dict(os.environ), cwd=None, timeout=30)
    assert (result.returncode, result.stdout.strip(), result.stderr.strip()) == (3, "out", "err")


def test_delivers_stdin_as_utf8():
    result = run_cli(_python("import sys; sys.stdout.reconfigure(encoding='utf-8'); print(sys.stdin.read()[::-1])"),
                     stdin="héllo", env={**os.environ, "PYTHONIOENCODING": "utf-8"}, cwd=None, timeout=30)
    assert result.stdout.strip() == "olléh"


def test_timeout_kills_the_whole_process_tree(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    parent = f"""
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open({str(pid_file)!r}, "w").write(str(child.pid))
        time.sleep(60)
    """
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_cli(_python(parent), stdin="", env=dict(os.environ), cwd=None, timeout=3)
    assert time.monotonic() - started < 30
    grandchild = int(pid_file.read_text())
    time.sleep(1)
    assert not _alive(grandchild)


def test_missing_binary_raises_file_not_found():
    with pytest.raises(FileNotFoundError):
        run_cli(["definitely-not-a-real-binary-xyz"], stdin="", env=dict(os.environ), cwd=None, timeout=5)


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
