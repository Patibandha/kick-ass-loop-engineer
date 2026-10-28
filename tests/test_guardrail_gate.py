"""Gate fail-open and allowlist-matching tests, written for Windows first.

The rules these tests pin (nothing here depends on the host OS for the matching itself):

* The allowlist is matched on the raw command text: after surrounding whitespace is stripped, the
  command must EQUAL an allowed prefix or START WITH ``<prefix> `` (a literal space).
* That match is case-sensitive and does not normalise the program name. ``PYTEST``, ``pytest.exe``,
  ``python.exe -m pytest`` and any spelling with a folder (``C:\\Python\\python.exe``, ``./pytest``) are
  therefore REJECTED, not folded onto the allowed spelling. A rejected spelling is a clear error; an
  accepted look-alike would be a way to run something the user never allowlisted.
* Gates run as an argument list (never ``shell=True``). Any command that could reach ``cmd.exe`` (a
  ``.cmd``/``.bat`` shim that Windows runs through it) must not carry ``&``, ``^`` or ``%VAR%`` through.
* Every way a gate can fail to run (rejected, not found, timeout, OS error, non-zero exit) is a
  FAILED result: nothing fails open into ``passed=True``.
"""

import os
import subprocess

import pytest

from kickass_loop_engineer.config import build_verification_policy, parse_gates
from kickass_loop_engineer.gates import GateSpec
from kickass_loop_engineer.guardrails import (
    DEFAULT_VERIFY_PREFIXES,
    VerificationPolicy,
)
from kickass_loop_engineer.verifier import run_gate, run_gates

pytestmark = pytest.mark.guardrail_gate

NOT_ALLOWLISTED = "command not in verification allowlist"
METACHARACTERS = "shell metacharacters are not allowed"


class _SpyRun:
    """Stands in for ``subprocess.run``: records every call and never starts a process."""

    def __init__(self, returncode=0, stdout="", raises=None):
        self.returncode = returncode
        self.stdout = stdout
        self.raises = raises
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "")


@pytest.fixture
def spy(monkeypatch):
    """Replace ``subprocess.run`` with a recorder that reports success."""
    recorder = _SpyRun()
    monkeypatch.setattr(subprocess, "run", recorder)
    return recorder


@pytest.fixture
def workspace(tmp_path):
    folder = tmp_path / "ws"
    folder.mkdir()
    return str(folder)


@pytest.fixture
def bin_dir(tmp_path, monkeypatch):
    """An empty PATH folder outside the workspace: the only place gate programs are found."""
    folder = tmp_path / "bin"
    folder.mkdir()
    monkeypatch.setenv("PATH", str(folder))
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    return folder


def _install(folder, name, ext=".exe"):
    """Put a fake program called *name* in *folder* (an empty file is enough: nothing really runs)."""
    path = folder / (name + ext if os.name == "nt" else name)
    path.write_text("", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o755)
    return path


# --------------------------------------------------------------------------- default gate string


def test_the_default_gate_string_is_what_an_unconfigured_run_uses():
    configured = parse_gates({})
    assert [g.command for g in configured] == [GateSpec().command]
    assert configured[0].command.strip()


@pytest.mark.parametrize("policy", [VerificationPolicy(), build_verification_policy({})],
                         ids=["bare-policy", "configured-policy"])
def test_the_default_gate_string_passes_the_real_allowlist(policy):
    ok, reason = policy.validate(GateSpec().command)
    assert ok, reason
    assert reason == ""


def test_the_default_gate_runs_as_an_argument_list_without_a_shell(spy, workspace, bin_dir):
    program = _install(bin_dir, GateSpec().command.split()[0])
    result = VerificationPolicy().run(GateSpec().command, workspace)
    assert result.passed and result.returncode == 0 and result.error == ""
    assert len(spy.calls) == 1
    argv, kwargs = spy.calls[0]
    assert isinstance(argv, list)
    assert argv == [str(program), *GateSpec().command.split()[1:]]
    assert not kwargs.get("shell")
    assert kwargs["cwd"] == workspace


def test_the_default_gate_with_no_interpreter_on_path_fails_closed(spy, workspace, bin_dir):
    # e.g. a Windows box where only the Microsoft Store "python3" stub exists: the tool is absent.
    result = VerificationPolicy().run(GateSpec().command, workspace)
    assert result.passed is False
    assert result.error == "verification tool not found"
    assert spy.calls == []


def test_the_default_gate_through_run_gate_and_run_gates(spy, workspace, bin_dir):
    _install(bin_dir, GateSpec().command.split()[0])
    record = run_gate("unit", GateSpec().command, workspace)
    assert record.passed and record.evidence.startswith("exit=0")
    outcome = run_gates([GateSpec(prove=False)], workspace)
    assert outcome.passed and [r.gate for r in outcome.records] == ["unit"]


def test_a_missing_default_interpreter_never_passes_a_gate_run(spy, workspace, bin_dir):
    record = run_gate("unit", GateSpec().command, workspace)
    assert record.passed is False
    assert "verification tool not found" in record.evidence
    outcome = run_gates([GateSpec(prove=False)], workspace)
    assert outcome.passed is False
    assert spy.calls == []


# --------------------------------------------------------------------------- off-list interpreters

OFF_LIST_COMMANDS = [
    "cmd /c pytest -q",
    "cmd.exe /c echo hi",
    "powershell -Command pytest -q",
    "powershell.exe -NoProfile -File x.ps1",
    "pwsh -c pytest",
    "bash -c pytest",
    "sh -c pytest",
    "wsl pytest -q",
    "wscript x.vbs",
    "cscript x.js",
    "node x.js",
    "perl x.pl",
    "ruby x.rb",
    "py -m pytest",
    "python x.py",
    "python -c pass",
    "python3 -c pass",
    "python -m http.server",
    "python -m pip install requests",
    "python3 -m pip install requests",
    "python -m pytestx",
    "python -m pytest2",
    "python3 -m pytest_evil",
    "pip install requests",
    "curl http://example.invalid",
    "echo hi",
    "npx some-package",
    "npm install left-pad",
]


@pytest.mark.parametrize("command", OFF_LIST_COMMANDS)
def test_an_off_list_command_is_rejected_before_anything_runs(command, spy, workspace, bin_dir):
    ok, reason = VerificationPolicy().validate(command)
    assert ok is False
    assert NOT_ALLOWLISTED in reason

    for name in ("cmd", "powershell", "pwsh", "bash", "sh", "node", "perl", "python", "py", "pip", "echo"):
        _install(bin_dir, name)  # the program exists: only the allowlist stands in the way
    result = VerificationPolicy().run(command, workspace)
    assert result.passed is False
    assert result.returncode is None
    assert NOT_ALLOWLISTED in result.error
    assert spy.calls == []


@pytest.mark.parametrize("command", ["", "   ", "\t", "\n"])
def test_an_empty_command_is_rejected(command, spy, workspace):
    result = VerificationPolicy().run(command, workspace)
    assert result.passed is False and result.error == "empty command"
    assert spy.calls == []


def test_a_missing_command_is_rejected(spy, workspace):
    result = VerificationPolicy().run(None, workspace)
    assert result.passed is False and result.error == "empty command"
    assert spy.calls == []


def test_the_allowlist_has_no_shell_and_no_open_ended_interpreter():
    shells = {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "bash", "sh", "wsl",
              "wscript", "cscript", "node", "perl", "ruby", "py", "pip"}
    bare = {"python", "python3", "npx", "npm", "yarn", "pnpm", "go", "cargo", "make", "node"}
    for prefix in DEFAULT_VERIFY_PREFIXES:
        tokens = prefix.split()
        assert tokens, "an empty prefix would allow everything"
        assert tokens[0] not in shells, prefix
        assert prefix not in bare, f"{prefix!r} would allow arbitrary scripts or packages"
        if tokens[0] in ("python", "python3"):
            assert tokens[1:2] == ["-m"] and len(tokens) == 3, f"{prefix!r} must pin one module"
            assert tokens[2] in {"pytest", "unittest", "mypy", "pytest_benchmark"}, prefix


# --------------------------------------------------------------------------- spelling variants

REJECTED_SPELLINGS = [
    # case: the match is case-sensitive, even though Windows file systems are not
    "PYTEST -q",
    "Pytest -q",
    "pyTest -q",
    "Python -m pytest",
    "PYTHON -m pytest",
    "python -m PYTEST",
    "python -m Pytest",
    "Python3 -m pytest -q",
    "PYTHON3 -M PYTEST -q",
    "NPM test",
    "Npm Test",
    "RUFF check .",
    "Make test",
    # extensions: no PATHEXT folding at validation time
    "pytest.exe -q",
    "pytest.EXE -q",
    "pytest.cmd -q",
    "pytest.bat -q",
    "python.exe -m pytest",
    "python3.exe -m pytest -q",
    "python.EXE -m pytest",
    "npm.cmd test",
    "ruff.exe check .",
    "pytest. -q",  # Windows drops a trailing dot, so this would name the real program
    # full and relative paths
    r"C:\Python312\python.exe -m pytest",
    r"C:\Python312\Scripts\pytest.exe -q",
    "C:/Python312/python.exe -m pytest",
    "C:/Python312/Scripts/pytest -q",
    "c:/python312/python -m pytest",
    "/usr/bin/pytest -q",
    "/usr/bin/python3 -m pytest -q",
    "./pytest -q",
    r".\pytest -q",
    r"..\pytest -q",
    r"tools\pytest.exe -q",
    r"\\server\share\pytest.exe -q",
    '"pytest" -q',
    '"C:\\Python312\\python.exe" -m pytest',
    "'python' -m pytest",
    # separators and prefix boundaries
    "pytest\t-q",
    "pytest\u00a0-q",
    "pytestx",
    "pytest-evil -q",
    "pytest_x -q",
    "pytest-benchmarkx",
    "python -m\tunittest",
    "python  -mpytest",
    "python -m pytest_",
    "ｐytest -q",  # fullwidth look-alike letter
]


@pytest.mark.parametrize("command", REJECTED_SPELLINGS)
def test_a_variant_spelling_is_rejected_not_folded_onto_an_allowed_command(command, spy, workspace, bin_dir):
    ok, reason = VerificationPolicy().validate(command)
    assert ok is False, f"{command!r} was accepted by accident"
    assert reason
    for name in ("pytest", "python", "python3", "npm", "ruff", "make"):
        _install(bin_dir, name)
    result = VerificationPolicy().run(command, workspace)
    assert result.passed is False
    assert spy.calls == []


ACCEPTED_SPELLINGS = [
    # (command, why the documented rule accepts it)
    ("pytest", "an allowed prefix on its own"),
    ("pytest -q", "prefix plus a space"),
    ("  pytest -q  ", "surrounding whitespace is stripped"),
    ("pytest -q\n", "a trailing newline is surrounding whitespace"),
    ("pytest  -q", "extra interior spaces after the prefix are shlex-split away"),
    ("python -m pytest -q", "exact lowercase interpreter spelling"),
    ("python3 -m pytest -q", "the default gate spelling"),
    ("python -m unittest", "an allowed prefix on its own"),
    ("pytest-benchmark", "a distinct allowed prefix"),
    ("npx playwright test", "a curated npx prefix"),
]


@pytest.mark.parametrize("command,why", ACCEPTED_SPELLINGS, ids=[c for c, _ in ACCEPTED_SPELLINGS])
def test_the_documented_acceptance_rule(command, why):
    ok, reason = VerificationPolicy().validate(command)
    assert ok, f"{command!r} should be accepted ({why}): {reason}"


def test_an_extra_prefix_is_matched_as_exactly_as_the_bundled_ones(spy, workspace):
    policy = VerificationPolicy(allowed_prefixes=("probe-tool",))
    assert policy.validate("probe-tool --version")[0]
    for variant in ("PROBE-TOOL --version", "probe-tool.exe --version", "./probe-tool --version",
                    "probe-toolx", "pytest -q"):
        assert policy.validate(variant)[0] is False, variant


# --------------------------------------------------------------------------- shell metacharacters


@pytest.mark.parametrize("meta", ["&", "&&", ";", "|", "||", "`", "$", ">", "<", ">>", "(", ")", "{", "}",
                                  "$(calc)", "`calc`", "^", "%", "%COMSPEC%", "%%a"])
def test_shell_metacharacters_are_rejected_wherever_they_sit(meta, spy, workspace, bin_dir):
    _install(bin_dir, "pytest")
    for command in (f"pytest -q {meta} calc", f"pytest -q x{meta}y", f"pytest -q{meta}"):
        ok, reason = VerificationPolicy().validate(command)
        assert ok is False, command
        assert reason == METACHARACTERS, command
        result = VerificationPolicy().run(command, workspace)
        assert result.passed is False and result.error == METACHARACTERS
    assert spy.calls == []


@pytest.mark.parametrize("command", ["pytest -q\ncalc", "pytest -q\r\ncalc", "pytest -q\n& calc",
                                     "pytest -q\rcalc"])
def test_line_breaks_inside_a_command_are_rejected(command, spy, workspace, bin_dir):
    _install(bin_dir, "pytest")
    result = VerificationPolicy().run(command, workspace)
    assert result.passed is False and result.error == METACHARACTERS
    assert spy.calls == []


def test_an_ampersand_chain_cannot_smuggle_an_off_list_program(spy, workspace, bin_dir):
    _install(bin_dir, "pytest")
    _install(bin_dir, "calc")
    for command in ("pytest -q & calc", "pytest -q && calc", "pytest -q &calc", "pytest -q&calc"):
        result = VerificationPolicy().run(command, workspace)
        assert result.passed is False and result.error == METACHARACTERS, command
    assert spy.calls == []


CMD_EXE_SYNTAX = [
    ("pytest", "pytest -q ^x"),
    ("pytest", "pytest -q x^"),
    ("pytest", "pytest -q ^&"),
    ("pytest", "pytest -q %COMSPEC%"),
    ("pytest", "pytest -q %PATH%"),
    ("pytest", "pytest -q %1"),
    ("pytest", "pytest -q %%a"),
    ("pytest", "pytest -q --basetemp=%TEMP%"),
    ("python3", "python3 -m pytest -q %USERPROFILE%"),
    ("npm", "npm test %COMSPEC%"),
    ("npm", "npm test ^x"),
    ("npm", "npm run lint -- %PATH%"),
]


@pytest.mark.parametrize("program,command", CMD_EXE_SYNTAX, ids=[c for _, c in CMD_EXE_SYNTAX])
def test_cmd_exe_syntax_never_reaches_a_shell(program, command, spy, workspace, bin_dir):
    """``^`` and ``%VAR%`` are only meaningful to cmd.exe, which Windows puts in front of ``.cmd`` shims
    (``npm`` is one). They are rejected on every host, so a Linux run catches their removal too."""
    _install(bin_dir, program, ext=".cmd")
    result = VerificationPolicy().run(command, workspace)
    assert result.passed is False and result.error == METACHARACTERS
    assert spy.calls == []


@pytest.mark.parametrize("command", ["pytest -q ^x", "pytest -q %COMSPEC%", "pytest -q %PATH%"])
def test_cmd_exe_syntax_is_rejected_even_for_an_exe_gate_program(command, spy, workspace, bin_dir):
    """The gate cannot know whether a program is a ``.cmd`` shim on some other host, so ``^`` and ``%`` are
    refused for every program, not only for shims."""
    _install(bin_dir, "pytest")
    result = VerificationPolicy().run(command, workspace)
    assert result.passed is False and result.error == METACHARACTERS
    assert spy.calls == []


def test_a_gate_never_runs_with_shell_true(spy, workspace, bin_dir):
    for name in ("pytest", "python3", "ruff", "make"):
        _install(bin_dir, name)
    for command in ("pytest -q", "python3 -m pytest -q", "ruff check .", "make test"):
        assert VerificationPolicy().run(command, workspace).passed
    assert len(spy.calls) == 4
    for argv, kwargs in spy.calls:
        assert isinstance(argv, list)
        assert kwargs.get("shell", False) is False


# --------------------------------------------------------------------------- fail-open


def test_a_rejected_gate_is_a_failed_record_and_starts_no_process(spy, workspace):
    record = run_gate("unit", "powershell -Command exit 0", workspace)
    assert record.passed is False
    assert NOT_ALLOWLISTED in record.evidence
    assert spy.calls == []


def test_a_rejected_gate_is_never_rescued_by_proof_of_test(spy, workspace):
    record = run_gate("unit", "PYTEST -q", workspace, with_proof=True)
    assert record.passed is False
    assert record.proof is None
    assert spy.calls == []


def test_run_gates_stops_at_a_rejected_gate_and_does_not_pass(spy, workspace, bin_dir):
    _install(bin_dir, "pytest")
    specs = [GateSpec(name="unit", command="pytest.exe -q", prove=False),
             GateSpec(name="smoke", command="pytest -q", prove=False)]
    outcome = run_gates(specs, workspace)
    assert outcome.passed is False
    assert [r.gate for r in outcome.records] == ["unit"]
    assert spy.calls == []


def test_an_unknown_gate_name_never_passes(spy, workspace):
    record = run_gate("no_such_gate", GateSpec().command, workspace)
    assert record.passed is False
    assert spy.calls == []


@pytest.mark.parametrize("failure", [
    FileNotFoundError("no such file"),
    PermissionError("access is denied"),
    OSError("%1 is not a valid Win32 application"),
    subprocess.TimeoutExpired(cmd="pytest", timeout=1),
], ids=["not-found", "denied", "bad-image", "timeout"])
def test_a_process_that_cannot_complete_is_a_failure(failure, monkeypatch, workspace, bin_dir):
    _install(bin_dir, "pytest")
    recorder = _SpyRun(raises=failure)
    monkeypatch.setattr(subprocess, "run", recorder)
    result = VerificationPolicy(timeout_seconds=1).run("pytest -q", workspace)
    assert result.passed is False
    assert result.returncode is None
    assert result.error
    assert len(recorder.calls) == 1


@pytest.mark.parametrize("code", [1, 2, 5, 255, -1])
def test_a_non_zero_exit_is_a_failure(code, monkeypatch, workspace, bin_dir):
    _install(bin_dir, "pytest")
    monkeypatch.setattr(subprocess, "run", _SpyRun(returncode=code))
    result = VerificationPolicy().run("pytest -q", workspace)
    assert result.passed is False and result.returncode == code
    assert run_gate("unit", "pytest -q", workspace).passed is False


def test_a_zero_exit_that_misses_its_expected_output_is_a_failure(monkeypatch, workspace, bin_dir):
    _install(bin_dir, "pytest")
    monkeypatch.setattr(subprocess, "run", _SpyRun(stdout="no tests ran in 0.01s"))
    result = VerificationPolicy().run("pytest -q", workspace, expect=r"(\d+) passed")
    assert result.passed is False
    assert "expected output matching" in result.error


def test_an_invalid_expectation_regex_is_a_failure_not_a_pass(monkeypatch, workspace, bin_dir):
    _install(bin_dir, "pytest")
    monkeypatch.setattr(subprocess, "run", _SpyRun(stdout="3 passed"))
    result = VerificationPolicy().run("pytest -q", workspace, expect="(unclosed")
    assert result.passed is False and "not a valid regex" in result.error


def test_a_program_planted_in_the_workspace_is_not_run_even_with_an_allowed_name(spy, tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    planted = workspace / "bin"
    planted.mkdir()
    _install(planted, "pytest")
    monkeypatch.setenv("PATH", str(planted))
    result = VerificationPolicy().run("pytest -q", str(workspace))
    assert result.passed is False and result.error == "verification tool not found"
    assert spy.calls == []
