"""Tests for the reproduce-first fix stage (emit → RED check → commit).

Real git fixture repos with a planted bug; FakeProvider-backed builders. The
RED check runs the real allowlisted ``python3 -m pytest <path> -q`` command,
so these tests prove the deterministic fail-on-buggy-code contract end to end.
"""
import subprocess

import pytest

from kickass_loop_engineer.agents import Builder
from kickass_loop_engineer.guardrails import VerificationPolicy
from kickass_loop_engineer.objective import Objective
from kickass_loop_engineer.reproduce import (
    REPRO_SYSTEM,
    ReproResult,
    generate_repro,
    repro_check_command,
    slugify,
)

from helpers import FakeProvider

GOAL = "fix double bug"
SLUG = "fix-double-bug"
REPRO_PATH = f"tests/test_repro_{SLUG}.py"

FAILING_REPRO = (
    f"=== FILE: {REPRO_PATH} ===\n"
    "from feature import double\n\n\n"
    "def test_double_reproduces_bug():\n"
    "    assert double(2) == 4\n"
    "=== END FILE ===\n"
)

PASSING_REPRO = (
    f"=== FILE: {REPRO_PATH} ===\n"
    "def test_always_green():\n"
    "    assert True\n"
    "=== END FILE ===\n"
)


def _bug_repo(tmp_path):
    """A committed git repo with a planted bug in feature.double."""
    repo = tmp_path / "bugrepo"
    repo.mkdir()
    (repo / "feature.py").write_text("def double(x):\n    return x + 1\n")
    # Build caches must be gitignored (verifier.prove precondition).
    (repo / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.pytest_cache/\n.loop-engineer/\n")
    for cmd in (["git", "init", "-q"],
                ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"],
                ["git", "add", "feature.py", ".gitignore"],
                ["git", "commit", "-qm", "planted bug baseline"]):
        subprocess.run(cmd, cwd=repo, check=True)
    return str(repo)


def _objective():
    return Objective(goal=GOAL, done_when="python3 -m pytest -q passes")


def _run(workspace, responses, on_usage=None):
    builder = Builder(FakeProvider(list(responses)))
    result = generate_repro(builder, _objective(), workspace,
                            policy=VerificationPolicy(), on_usage=on_usage)
    return result, builder.provider


def _git_out(workspace, *args):
    return subprocess.run(["git", *args], cwd=workspace, check=True,
                          capture_output=True, text=True).stdout


@pytest.mark.parametrize("text,expected", [
    ("Fix the Double Bug!!", "fix-the-double-bug"),
    ("  fix   double  ", "fix-double"),
    ("!!!", "bug"),
    ("a" * 100, "a" * 40),
])
def test_slugify_lowercases_collapses_and_caps(text, expected):
    assert slugify(text) == expected


@pytest.mark.slow
def test_happy_path_red_repro_is_committed(tmp_path):
    workspace = _bug_repo(tmp_path)
    usage = []
    result, provider = _run(workspace, [FAILING_REPRO], on_usage=usage.append)

    assert result == ReproResult(path=REPRO_PATH, committed=True)
    assert not result.blocked
    subject = _git_out(workspace, "log", "-1", "--format=%s").strip()
    assert subject == f"test: reproduction for {SLUG} (engine-pinned)"
    committed = _git_out(workspace, "show", "--name-only", "--format=",
                         "HEAD").split()
    assert committed == [REPRO_PATH], "the commit stages ONLY the repro file"
    assert len(provider.calls) == 1
    assert len(usage) == 1, "the single provider call is ledgered"


@pytest.mark.slow
def test_passing_repro_regenerates_once_with_corrective_feedback_then_blocks(
        tmp_path):
    workspace = _bug_repo(tmp_path)
    usage = []
    result, provider = _run(workspace, [PASSING_REPRO, PASSING_REPRO],
                            on_usage=usage.append)

    assert result.blocked
    assert "cannot reproduce" in result.reason
    assert not result.committed
    assert len(provider.calls) == 2, "exactly ONE corrective regeneration"
    _system, second_prompt = provider.calls[1]
    assert "PASSED" in second_prompt, "feedback says the repro passed on buggy code"
    assert "must FAIL" in second_prompt
    assert len(usage) == 2, "BOTH provider calls are ledgered"
    subject = _git_out(workspace, "log", "-1", "--format=%s").strip()
    assert subject == "planted bug baseline", "nothing was committed"


@pytest.mark.slow
def test_regeneration_producing_red_repro_is_committed(tmp_path):
    workspace = _bug_repo(tmp_path)
    usage = []
    result, provider = _run(workspace, [PASSING_REPRO, FAILING_REPRO],
                            on_usage=usage.append)

    assert result.committed and not result.blocked
    assert result.path == REPRO_PATH
    assert len(provider.calls) == 2
    assert len(usage) == 2
    subject = _git_out(workspace, "log", "-1", "--format=%s").strip()
    assert subject == f"test: reproduction for {SLUG} (engine-pinned)"


@pytest.mark.slow
def test_missing_file_block_regenerates_then_blocks(tmp_path):
    workspace = _bug_repo(tmp_path)
    result, provider = _run(workspace, ["no file blocks here", "still prose"])

    assert result.blocked
    assert "cannot reproduce" in result.reason
    assert len(provider.calls) == 2
    _system, second_prompt = provider.calls[1]
    assert REPRO_PATH in second_prompt


@pytest.mark.slow
def test_file_lands_at_slug_path_via_guarded_workspace(tmp_path):
    workspace = _bug_repo(tmp_path)
    goal = "Fix the Double Bug!!"
    repro_path = "tests/test_repro_fix-the-double-bug.py"
    block = (f"=== FILE: {repro_path} ===\n"
             "from feature import double\n\n\n"
             "def test_double():\n    assert double(2) == 4\n"
             "=== END FILE ===\n"
             "=== FILE: sneaky.py ===\nEXTRA = 1\n=== END FILE ===\n")
    builder = Builder(FakeProvider([block]))
    result = generate_repro(
        builder, Objective(goal=goal, done_when="tests pass"), workspace,
        policy=VerificationPolicy())

    assert result.committed
    assert result.path == repro_path
    assert (tmp_path / "bugrepo" / repro_path).is_file()
    assert not (tmp_path / "bugrepo" / "sneaky.py").exists(), \
        "only the named repro file is ever written"
    _system, prompt = builder.provider.calls[0]
    assert repro_path in prompt, "the brief names the exact destination path"


@pytest.mark.slow
def test_repro_prompt_uses_repro_system_and_forbids_fixes(tmp_path):
    workspace = _bug_repo(tmp_path)
    _result, provider = _run(workspace, [FAILING_REPRO])
    system, _prompt = provider.calls[0]
    assert system == REPRO_SYSTEM
    assert "ONE" in system and "test_repro_" in system
    assert "self-contained" in system and "conftest" in system, \
        "the repro must not rely on pytest configuration (it runs isolated)"


def test_repro_check_command_is_config_isolated_and_allowlisted():
    command = repro_check_command("tests/test_repro_x.py")
    assert command == ("python3 -m pytest tests/test_repro_x.py -q "
                       "--noconftest -o addopts=")
    ok, reason = VerificationPolicy().validate(command)
    assert ok, reason


@pytest.mark.slow
def test_red_check_ignores_hostile_conftest(tmp_path):
    # A committed conftest that skip-marks test_repro_* would make an honest
    # failing repro look green at the RED check (skipped tests exit 0) and
    # force a bogus "cannot reproduce". The config-isolated command must see
    # the genuine failure and commit the repro.
    workspace = _bug_repo(tmp_path)
    (tmp_path / "bugrepo" / "conftest.py").write_text(
        "import pytest\n\n\n"
        "def pytest_collection_modifyitems(items):\n"
        "    for item in items:\n"
        "        if 'test_repro_' in str(item.fspath):\n"
        "            item.add_marker(pytest.mark.skip(reason='flaky'))\n")
    subprocess.run(["git", "add", "conftest.py"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "hostile conftest"],
                   cwd=workspace, check=True)
    result, _provider = _run(workspace, [FAILING_REPRO])

    assert result.committed and not result.blocked


@pytest.mark.slow
def test_existing_repro_path_is_never_overwritten(tmp_path):
    workspace = _bug_repo(tmp_path)
    sentinel = "def test_preexisting():\n    assert True\n"
    preexisting = tmp_path / "bugrepo" / REPRO_PATH
    preexisting.parent.mkdir(parents=True)
    preexisting.write_text(sentinel)
    subprocess.run(["git", "add", REPRO_PATH], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "preexisting repro-named file"],
                   cwd=workspace, check=True)

    suffixed = f"tests/test_repro_{SLUG}-2.py"
    block = (f"=== FILE: {suffixed} ===\n"
             "from feature import double\n\n\n"
             "def test_double():\n    assert double(2) == 4\n"
             "=== END FILE ===\n")
    result, provider = _run(workspace, [block])

    assert result.committed
    assert result.path == suffixed, "colliding slug gets a numeric suffix"
    assert preexisting.read_text() == sentinel, "the existing file is untouched"
    _system, prompt = provider.calls[0]
    assert suffixed in prompt, "the brief names the suffixed destination"
    subject = _git_out(workspace, "log", "-1", "--format=%s").strip()
    assert subject == f"test: reproduction for {SLUG}-2 (engine-pinned)"
