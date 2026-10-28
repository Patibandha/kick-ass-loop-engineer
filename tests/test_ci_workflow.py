"""Tests for ``.github/workflows/ci.yml``, ``tests/expected-guardrail-ids.txt`` and ``scripts/check_expected_tests.py``.

The workflow is loaded with ``yaml.safe_load`` and held to its security shape: push and pull_request triggers
only (never ``pull_request_target``), read-only token, credentials not persisted, actions pinned by commit SHA,
no secrets. The expected-ID file must equal the guardrail tests pytest collects right now, so adding, renaming or
deleting a guardrail test without regenerating the file fails here. The checker script is exercised on small
JUnit samples and on a real pytest report.
"""

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.ci_workflow

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
EXPECTED = ROOT / "tests" / "expected-guardrail-ids.txt"
SCRIPT = ROOT / "scripts" / "check_expected_tests.py"

GUARDRAIL_EXPRESSION = (
    "(guardrail_gate or guardrail_isolation or guardrail_proof) and not guardrail_isolation_capability"
)
OPERATING_SYSTEMS = {"ubuntu-latest", "macos-latest", "windows-latest"}
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def _load_script():
    """Import the script by path: ``scripts/`` is not a package."""
    spec = importlib.util.spec_from_file_location("check_expected_tests", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cet = _load_script()


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def workflow_text():
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def job(workflow):
    jobs = workflow["jobs"]
    assert list(jobs) == ["test"]
    return jobs["test"]


@pytest.fixture(scope="module")
def steps(job):
    return job["steps"]


def _triggers(workflow):
    """YAML 1.1 reads a bare ``on`` key as ``True``; accept either spelling."""
    return workflow.get("on", workflow.get(True))


def _step_using(steps, action):
    matches = [s for s in steps if s.get("uses", "").split("@")[0] == action]
    assert len(matches) == 1, f"expected exactly one {action} step"
    return matches[0]


def _run_commands(steps):
    return [s["run"].strip() for s in steps if "run" in s]


# --------------------------------------------------------------------------- workflow shape


def test_triggers_are_push_and_pull_request_only(workflow, workflow_text):
    assert set(_triggers(workflow)) == {"push", "pull_request"}
    assert "pull_request_target" not in workflow_text


def test_token_is_read_only_everywhere(workflow, job):
    assert workflow["permissions"] == {"contents": "read"}
    assert job.get("permissions", {"contents": "read"}) == {"contents": "read"}


def test_matrix_is_three_operating_systems_on_python_3_12(job):
    strategy = job["strategy"]
    assert strategy["fail-fast"] is False
    matrix = strategy["matrix"]
    assert set(matrix["os"]) == OPERATING_SYSTEMS and len(matrix["os"]) == 3
    assert [str(v) for v in matrix["python-version"]] == ["3.12"]
    assert job["runs-on"] == "${{ matrix.os }}"


def test_checkout_does_not_persist_credentials(steps):
    checkout = _step_using(steps, "actions/checkout")
    assert checkout["with"]["persist-credentials"] is False


def test_setup_python_uses_the_matrix_version(steps):
    setup = _step_using(steps, "actions/setup-python")
    assert setup["with"]["python-version"] == "${{ matrix.python-version }}"


def test_every_action_is_pinned_by_full_commit_sha_with_a_version_comment(steps, workflow_text):
    used = [s["uses"] for s in steps if "uses" in s]
    assert {u.split("@")[0] for u in used} >= {"actions/checkout", "actions/setup-python"}
    for uses in used:
        name, _, ref = uses.partition("@")
        assert FULL_SHA.match(ref), f"{name} is not pinned by a full commit SHA: {ref!r}"
        assert re.search(rf"uses:\s*{re.escape(uses)}\s+#\s*v\d", workflow_text), f"{name} lacks a version comment"


def test_no_secrets_and_no_environment_printing(workflow, workflow_text):
    assert "secrets" not in workflow_text
    assert "GITHUB_TOKEN" not in workflow_text
    commands = " ".join(_run_commands(workflow["jobs"]["test"]["steps"])).lower()
    for leak in ("printenv", "env:", "$env", "set ", "export", "echo", "dump", "tojson"):
        assert leak not in commands, leak


def test_steps_install_test_mutate_and_check_in_order(steps):
    commands = _run_commands(steps)
    assert len(commands) == 4
    install, tests, mutations, expected = commands
    assert "pip install -e" in install and ".[dev]" in install
    assert tests == "python -m pytest -q --junitxml=junit.xml"
    assert mutations == "python scripts/check_mutations.py"
    assert expected == "python scripts/check_expected_tests.py junit.xml tests/expected-guardrail-ids.txt"


def test_results_are_uploaded_per_os_even_on_failure(steps):
    upload = _step_using(steps, "actions/upload-artifact")
    assert upload["if"] == "always()"
    assert "${{ matrix.os }}" in upload["with"]["name"]
    paths = upload["with"]["path"].split()
    assert paths == ["junit.xml", "tests/expected-guardrail-ids.txt"]


# --------------------------------------------------------------------------- expected guardrail IDs


def _collected_guardrail_ids():
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "-m", GUARDRAIL_EXPRESSION],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=env, timeout=300, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return sorted({line.strip() for line in completed.stdout.splitlines() if "::" in line})


def test_expected_file_is_sorted_unique_and_one_id_per_line():
    lines = EXPECTED.read_text(encoding="utf-8").splitlines()
    assert lines, "expected-ID file is empty"
    assert lines == sorted(set(lines))
    assert all(line == line.strip() and "::" in line for line in lines)


def test_expected_file_equals_the_collected_guardrail_tests():
    expected = EXPECTED.read_text(encoding="utf-8").splitlines()
    collected = _collected_guardrail_ids()
    assert collected, "no guardrail tests collected"
    missing = sorted(set(collected) - set(expected))
    stale = sorted(set(expected) - set(collected))
    assert not missing and not stale, (
        f"regenerate {EXPECTED.name}: not listed {missing[:5]}, no longer collected {stale[:5]}"
    )


def test_expected_file_covers_every_guardrail_family():
    expected = EXPECTED.read_text(encoding="utf-8")
    for module in ("test_guardrail_gate.py", "test_guardrail_isolation.py", "test_guardrail_proof.py"):
        assert f"tests/{module}::" in expected, module


# --------------------------------------------------------------------------- check_expected_tests.py


def _junit(cases):
    """A minimal pytest-style JUnit report; *cases* is ``[(classname, name, outcome)]``."""
    body = []
    for classname, name, outcome in cases:
        inner = {"passed": "", "skipped": '<skipped message="skip" />', "failed": '<failure message="boom" />',
                 "error": '<error message="boom" />'}[outcome]
        body.append(f'<testcase classname="{classname}" name="{name}" time="0.001">{inner}</testcase>')
    return ('<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">'
            + "".join(body) + "</testsuite></testsuites>")


def _run_script(tmp_path, cases, ids):
    junit = tmp_path / "junit.xml"
    junit.write_text(_junit(cases), encoding="utf-8")
    expected = tmp_path / "expected.txt"
    expected.write_text("\n".join(ids) + "\n", encoding="utf-8")
    return subprocess.run([sys.executable, str(SCRIPT), str(junit), str(expected)],
                          capture_output=True, text=True, timeout=60, check=False)


IDS = ["tests/test_a.py::test_one", "tests/test_a.py::TestB::test_two[x-1]"]
ALL_RAN = [("tests.test_a", "test_one", "passed"), ("tests.test_a.TestB", "test_two[x-1]", "passed"),
           ("tests.test_a", "test_unrelated", "skipped")]


def test_all_expected_tests_ran_and_passed(tmp_path):
    completed = _run_script(tmp_path, ALL_RAN, IDS)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "OK" in completed.stdout


def test_one_expected_test_missing_fails_and_names_it(tmp_path):
    completed = _run_script(tmp_path, ALL_RAN[:1], IDS)
    assert completed.returncode == 1
    assert "missing: tests/test_a.py::TestB::test_two[x-1]" in completed.stdout
    assert "test_one" not in completed.stdout.replace("FAIL", "")


def test_one_expected_test_skipped_fails_and_names_it(tmp_path):
    cases = [ALL_RAN[0], ("tests.test_a.TestB", "test_two[x-1]", "skipped")]
    completed = _run_script(tmp_path, cases, IDS)
    assert completed.returncode == 1
    assert "skipped: tests/test_a.py::TestB::test_two[x-1]" in completed.stdout


@pytest.mark.parametrize("outcome", ["failed", "error"])
def test_one_expected_test_failed_fails_and_names_it(tmp_path, outcome):
    cases = [(ALL_RAN[0][0], ALL_RAN[0][1], outcome), ALL_RAN[1]]
    completed = _run_script(tmp_path, cases, IDS)
    assert completed.returncode == 1
    assert "failed: tests/test_a.py::test_one" in completed.stdout


def test_a_rerun_keeps_the_worst_outcome(tmp_path):
    cases = [*ALL_RAN, ("tests.test_a", "test_one", "failed")]
    assert _run_script(tmp_path, cases, IDS).returncode == 1


def test_an_empty_expected_list_is_a_usage_error_not_a_pass(tmp_path):
    completed = _run_script(tmp_path, ALL_RAN, ["# nothing here", ""])
    assert completed.returncode == 2
    assert "lists no tests" in completed.stderr


def test_a_malformed_report_is_a_usage_error(tmp_path):
    junit = tmp_path / "junit.xml"
    junit.write_text("<testsuites><testcase", encoding="utf-8")
    expected = tmp_path / "expected.txt"
    expected.write_text(IDS[0] + "\n", encoding="utf-8")
    assert cet.main([str(junit), str(expected)]) == 2


def test_a_missing_report_is_a_usage_error(tmp_path):
    expected = tmp_path / "expected.txt"
    expected.write_text(IDS[0] + "\n", encoding="utf-8")
    assert cet.main([str(tmp_path / "absent.xml"), str(expected)]) == 2


@pytest.mark.parametrize("node_id,key", [
    ("tests/test_a.py::test_one", ("tests.test_a", "test_one")),
    ("tests/test_a.py::TestB::test_two", ("tests.test_a.TestB", "test_two")),
    ("tests/test_a.py::TestB::TestC::test_three", ("tests.test_a.TestB.TestC", "test_three")),
    ("tests/test_a.py::test_p[a::b]", ("tests.test_a", "test_p[a::b]")),
    ("tests/test_a.py::test_p[./x.py -q]", ("tests.test_a", "test_p[./x.py -q]")),
    ("test_top.py::test_x", ("test_top", "test_x")),
])
def test_node_ids_map_to_junit_keys(node_id, key):
    assert cet.junit_key(node_id) == key


@pytest.mark.parametrize("bad", ["tests/test_a.py", "tests/test_a::test_one", "tests/test_a.py::", "::test_x"])
def test_malformed_node_ids_are_rejected(bad):
    with pytest.raises(cet.ExpectedTestsError):
        cet.junit_key(bad)


def test_the_checker_agrees_with_a_real_pytest_report(tmp_path):
    """Node IDs from ``--collect-only`` must match the JUnit keys of the same run, skips included."""
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    sample = tmp_path / "tests" / "test_sample.py"
    sample.parent.mkdir()
    sample.write_text(
        "import pytest\n\n"
        "def test_plain():\n    pass\n\n"
        "@pytest.mark.parametrize('p', ['a', './b -q', 'c::d'])\n"
        "def test_param(p):\n    pass\n\n"
        "class TestGroup:\n    def test_method(self):\n        pass\n\n"
        "def test_skipped():\n    pytest.skip('not here')\n",
        encoding="utf-8",
    )
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.pop("PYTHONPATH", None)
    base = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--rootdir", str(tmp_path)]
    collected = subprocess.run([*base, "--collect-only", "-q", "tests"], cwd=tmp_path, capture_output=True,
                               text=True, encoding="utf-8", env=env, timeout=120, check=False)
    ids = sorted(line.strip() for line in collected.stdout.splitlines() if "::" in line)
    assert len(ids) == 6, collected.stdout + collected.stderr
    junit = tmp_path / "junit.xml"
    subprocess.run([*base, "-q", f"--junitxml={junit}", "tests"], cwd=tmp_path, capture_output=True,
                   env=env, timeout=120, check=False)
    results = cet.read_junit(str(junit))

    problems = cet.check(ids, results)
    assert problems == [("tests/test_sample.py::test_skipped", cet.SKIPPED)]
