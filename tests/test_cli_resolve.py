"""Subscription CLIs installed as npm ``.cmd`` shims start without a shell (UAT-BUG-14)."""

import json
import os
import stat
import subprocess
import sys

import pytest

from kickass_loop_engineer.providers.claude_code import ClaudeCodeProvider
from kickass_loop_engineer.providers.cli_process import (
    UnsafeBatchArgument,
    find_program,
    resolve_argv,
    run_cli,
)

windows_only = pytest.mark.skipif(os.name != "nt", reason="PATHEXT and .cmd shims are Windows behaviour")
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX PATH lookup")


def _shim(folder, name, body):
    """Write a Windows batch shim *name*.cmd into *folder* and return its path."""
    path = folder / f"{name}.cmd"
    path.write_text(f"@echo off\r\n{body}\r\n", encoding="ascii")
    return path


@windows_only
def test_a_cmd_shim_on_path_is_found_through_pathext(tmp_path):
    shim = _shim(tmp_path, "tool", "echo hi")
    found = find_program("tool", {"PATH": str(tmp_path), "PATHEXT": ".COM;.EXE;.BAT;.CMD"})
    assert os.path.normcase(found) == os.path.normcase(str(shim))


@windows_only
def test_the_current_directory_is_never_searched(tmp_path, monkeypatch):
    planted, elsewhere = tmp_path / "worktree", tmp_path / "empty"
    planted.mkdir()
    elsewhere.mkdir()
    _shim(planted, "tool", "echo planted")
    monkeypatch.chdir(planted)
    assert find_program("tool", {"PATH": str(elsewhere)}) == "tool"


@windows_only
def test_relative_path_entries_are_skipped(tmp_path, monkeypatch):
    (tmp_path / "bin").mkdir()
    _shim(tmp_path / "bin", "tool", "echo hi")
    monkeypatch.chdir(tmp_path)
    assert find_program("tool", {"PATH": "bin"}) == "tool"


def test_a_name_with_a_folder_is_left_alone(tmp_path):
    explicit = str(tmp_path / "tool")
    assert find_program(explicit, {"PATH": str(tmp_path)}) == explicit


def test_an_unknown_program_stays_unresolved_so_the_caller_sees_file_not_found(tmp_path):
    assert resolve_argv(["no-such-cli-xyz", "-p"], {"PATH": str(tmp_path)}) == ["no-such-cli-xyz", "-p"]


@posix_only
def test_posix_lookup_uses_the_child_path(tmp_path):
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\necho hi\n")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    assert find_program("tool", {"PATH": str(tmp_path)}) == str(tool)


@windows_only
@pytest.mark.parametrize("arg", ["a&calc", "x|y", "%PATH%", "!v!", 'say "hi"', "a^b", "a<b", "a>b", "a\nb"])
def test_a_shim_never_receives_a_cmd_metacharacter(tmp_path, arg):
    _shim(tmp_path, "tool", "echo hi")
    with pytest.raises(UnsafeBatchArgument):
        resolve_argv(["tool", "-p", arg], {"PATH": str(tmp_path)})


@windows_only
def test_an_exe_may_receive_any_argument(tmp_path):
    exe = os.path.basename(sys.executable)
    folder = os.path.dirname(sys.executable)
    argv = resolve_argv([exe, "-c", "print('a&b')"], {"PATH": folder})
    assert argv[1:] == ["-c", "print('a&b')"]


@windows_only
def test_run_cli_starts_a_cmd_shim_by_its_bare_name(tmp_path):
    _shim(tmp_path, "tool", "echo shim %1")
    env = {**os.environ, "PATH": str(tmp_path)}
    result = run_cli(["tool", "hello"], stdin="", env=env, cwd=None, timeout=30)
    assert (result.returncode, result.stdout.strip()) == (0, "shim hello")


@windows_only
def test_the_claude_provider_runs_an_npm_style_claude_cmd(tmp_path, monkeypatch):
    answer = {"type": "result", "is_error": False, "result": "pong", "usage": {"input_tokens": 1}}
    (tmp_path / "answer.json").write_text(json.dumps(answer), encoding="ascii")
    _shim(tmp_path, "claude", 'type "%~dp0answer.json"')
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('SystemRoot', 'C:/Windows')}\\System32")
    result = ClaudeCodeProvider(retry_attempts=1).complete("sys", "ping")
    assert result.text == "pong"


@windows_only
def test_a_bare_name_is_what_failed_before_the_fix(tmp_path):
    _shim(tmp_path, "tool", "echo hi")
    with pytest.raises(FileNotFoundError):
        subprocess.run(["tool"], env={**os.environ, "PATH": str(tmp_path)}, capture_output=True, check=False)
