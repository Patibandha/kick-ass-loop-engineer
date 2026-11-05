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


def _shim(folder, name, body="echo hi"):
    """Write a Windows batch shim *name*.cmd into *folder* and return its path."""
    path = folder / f"{name}.cmd"
    path.write_text(f"@echo off\r\n{body}\r\n", encoding="ascii")
    return path


def _executable(folder, name):
    """Write a POSIX executable script *name* into *folder* and return its path."""
    path = folder / name
    path.write_text("#!/bin/sh\necho hi\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _same(left, right):
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


@windows_only
def test_a_cmd_shim_on_path_is_found_through_pathext(tmp_path):
    shim = _shim(tmp_path, "tool")
    assert _same(find_program("tool", {"PATH": str(tmp_path), "PATHEXT": ".COM;.EXE;.BAT;.CMD"}), shim)


@windows_only
def test_path_and_pathext_are_read_case_insensitively_from_the_child_env(tmp_path):
    shim = _shim(tmp_path, "tool")
    assert _same(find_program("tool", {"Path": str(tmp_path), "PathExt": ".CMD"}), shim)


@windows_only
def test_empty_pathext_entries_never_pick_npms_extensionless_sh_shim(tmp_path):
    (tmp_path / "tool").write_text("#!/bin/sh\n")
    shim = _shim(tmp_path, "tool")
    for pathext in ("", ";.CMD;", ".COM;.EXE;.BAT;.CMD;"):
        assert _same(find_program("tool", {"PATH": str(tmp_path), "PATHEXT": pathext}), shim)


def test_the_current_directory_is_never_searched(tmp_path, monkeypatch):
    planted, elsewhere = tmp_path / "worktree", tmp_path / "empty"
    planted.mkdir()
    elsewhere.mkdir()
    _shim(planted, "tool") if os.name == "nt" else _executable(planted, "tool")
    monkeypatch.chdir(planted)
    with pytest.raises(FileNotFoundError):
        find_program("tool", {"PATH": str(elsewhere)})


@pytest.mark.parametrize("entry", ["bin", ".", "", "./bin"])
def test_relative_and_empty_path_entries_are_skipped(tmp_path, monkeypatch, entry):
    (tmp_path / "bin").mkdir()
    for folder in (tmp_path, tmp_path / "bin"):
        _shim(folder, "tool") if os.name == "nt" else _executable(folder, "tool")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        find_program("tool", {"PATH": entry})


@windows_only
def test_a_rooted_folder_without_a_drive_is_skipped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bin").mkdir()
    _shim(tmp_path / "bin", "tool")
    rooted = os.path.splitdrive(str(tmp_path / "bin"))[1]
    with pytest.raises(FileNotFoundError):
        find_program("tool", {"PATH": rooted})


def test_a_given_env_without_path_does_not_fall_back_to_ours(monkeypatch):
    monkeypatch.setenv("PATH", os.path.dirname(sys.executable))
    with pytest.raises(FileNotFoundError):
        find_program(os.path.basename(sys.executable), {})


def test_a_name_with_a_folder_is_left_alone(tmp_path):
    explicit = str(tmp_path / "tool")
    assert find_program(explicit, {"PATH": str(tmp_path)}) == explicit


def test_an_unknown_program_fails_closed_instead_of_letting_the_os_search(tmp_path):
    with pytest.raises(FileNotFoundError) as exc:
        resolve_argv(["no-such-cli-xyz", "-p"], {"PATH": str(tmp_path)})
    assert exc.value.filename == "no-such-cli-xyz"


@posix_only
def test_posix_lookup_uses_the_child_path(tmp_path):
    tool = _executable(tmp_path, "tool")
    assert find_program("tool", {"PATH": str(tmp_path)}) == str(tool)


@windows_only
@pytest.mark.parametrize("arg", ["a&calc", "x|y", "%PATH%", "!v!", "a^b", "a<b", "a>b", "a\nb", "a\rb"])
def test_a_shim_never_receives_a_cmd_metacharacter(tmp_path, arg):
    _shim(tmp_path, "tool")
    with pytest.raises(UnsafeBatchArgument):
        resolve_argv(["tool", "-p", arg], {"PATH": str(tmp_path)})


@windows_only
@pytest.mark.parametrize("folder", ["a&whoami&b", "%TEMP%x"])
def test_a_shim_whose_own_path_holds_a_metacharacter_is_refused(tmp_path, folder):
    (tmp_path / folder).mkdir()
    _shim(tmp_path / folder, "tool")
    with pytest.raises(UnsafeBatchArgument):
        resolve_argv(["tool"], {"PATH": str(tmp_path / folder)})


@windows_only
def test_a_trailing_dot_does_not_hide_a_batch_file(tmp_path):
    shim = _shim(tmp_path, "tool")
    with pytest.raises(UnsafeBatchArgument):
        resolve_argv([f"{shim}.", "a&b"], {})


@windows_only
def test_codexs_quoted_config_override_still_reaches_a_shim(tmp_path):
    _shim(tmp_path, "codex")
    argv = resolve_argv(["codex", "exec", "-c", 'windows.sandbox="unelevated"'], {"PATH": str(tmp_path)})
    assert argv[1:] == ["exec", "-c", 'windows.sandbox="unelevated"']


@windows_only
def test_an_exe_may_receive_any_argument():
    argv = resolve_argv([os.path.basename(sys.executable), "-c", "print('a&b')"],
                        {"PATH": os.path.dirname(sys.executable)})
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
    system32 = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{system32}")
    result = ClaudeCodeProvider(retry_attempts=1).complete("sys", "ping")
    assert result.text == "pong"


@windows_only
def test_a_bare_name_is_what_failed_before_the_fix(tmp_path):
    _shim(tmp_path, "tool")
    with pytest.raises(FileNotFoundError):
        subprocess.run(["tool"], env={**os.environ, "PATH": str(tmp_path)}, capture_output=True, check=False)
