"""Tests for proof-of-test and gate evidence."""
import os
import subprocess
import tempfile
import unittest
from dataclasses import dataclass, field

from kickass_loop_engineer.gates import GateSpec
from kickass_loop_engineer.guardrails import VerificationPolicy, VerificationResult
from kickass_loop_engineer.verifier import (
    prove, ProofRecord, run_gate, run_gates, EvidenceRecord, GatesResult,
    OBSERVER_CAP, OBSERVER_TIMEOUT_SECONDS,
)


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _init_repo():
    """A git repo whose committed baseline gitignores Python caches."""
    repo = tempfile.mkdtemp()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    with open(os.path.join(repo, ".gitignore"), "w") as f:
        f.write("__pycache__/\n*.pyc\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


class ProofOfTestTests(unittest.TestCase):
    def test_proof_succeeds_when_change_is_real(self):
        repo = _init_repo()
        with open(os.path.join(repo, "test_feature.py"), "w") as f:
            f.write("from impl import answer\n\n"
                    "def test_answer():\n    assert answer() == 42\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "baseline")
        with open(os.path.join(repo, "impl.py"), "w") as f:
            f.write("def answer():\n    return 42\n")
        record = prove(gate_cmd="pytest -q", workspace=repo)
        self.assertIsInstance(record, ProofRecord)
        self.assertTrue(record.green_before, record.error)
        self.assertTrue(record.red_when_reverted, record.error)
        self.assertTrue(record.green_after, record.error)
        self.assertTrue(record.proven)

    def test_clean_tree_reports_needs_uncommitted_change(self):
        repo = _init_repo()
        with open(os.path.join(repo, "test_trivial.py"), "w") as f:
            f.write("def test_trivial():\n    assert True\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "all committed")   # nothing uncommitted
        record = prove(gate_cmd="pytest -q", workspace=repo)
        self.assertFalse(record.proven)
        self.assertIn("uncommitted change", record.error)
        self.assertFalse(record.aborted)              # clean, not a stash failure

    def test_proof_fails_when_gate_passes_without_the_change(self):
        repo = _init_repo()
        with open(os.path.join(repo, "test_trivial.py"), "w") as f:
            f.write("def test_trivial():\n    assert True\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "green baseline")
        with open(os.path.join(repo, "note.txt"), "w") as f:
            f.write("unrelated\n")
        record = prove(gate_cmd="pytest -q", workspace=repo)
        self.assertTrue(record.green_before)
        self.assertFalse(record.red_when_reverted)
        self.assertFalse(record.proven)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.repo = _init_repo()
        with open(os.path.join(self.repo, "test_ok.py"), "w") as f:
            f.write("def test_ok():\n    assert True\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "ok")

    def test_run_gate_returns_evidence_not_opinion(self):
        ev = run_gate("unit", "pytest -q", workspace=self.repo)
        self.assertIsInstance(ev, EvidenceRecord)
        self.assertEqual(ev.gate, "unit")
        self.assertTrue(ev.passed)
        self.assertIn("exit", ev.evidence.lower())

    def test_errored_gate_never_reports_passed(self):
        ev = run_gate("unit", "pytest /no/such/path", workspace=self.repo)
        self.assertFalse(ev.passed)

    def test_run_gate_rejects_unknown_gate_name(self):
        ev = run_gate("not_a_gate", "pytest -q", workspace=self.repo)
        self.assertFalse(ev.passed)
        self.assertIn("unknown gate", ev.evidence)
        self.assertIn("unit", ev.evidence)  # names the known gates


class ProofRobustnessTests(unittest.TestCase):
    def test_rejected_command_reports_real_cause_not_test_failure(self):
        repo = _init_repo()
        open(os.path.join(repo, "x.txt"), "w").write("hi")  # dirty tree
        # 'echo' is not in the verify allowlist -> rejection, not a red test
        record = prove(gate_cmd="echo hi", workspace=repo)
        self.assertFalse(record.green_before)
        self.assertIn("could not run", record.error)
        self.assertNotIn("not green", record.error)

    def test_pop_failure_preserves_change_and_flags_aborted(self):
        import kickass_loop_engineer.verifier as v
        repo = _init_repo()
        with open(os.path.join(repo, "test_feature.py"), "w") as f:
            f.write("from impl import answer\n\ndef test_answer():\n    assert answer()==42\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "baseline")
        with open(os.path.join(repo, "impl.py"), "w") as f:
            f.write("def answer():\n    return 42\n")
        real_git = v._git
        def fake_git(ws, *args, **kw):
            if args[:2] == ("stash", "pop"):
                raise subprocess.CalledProcessError(1, "git stash pop", stderr="CONFLICT")
            return real_git(ws, *args, **kw)
        v._git = fake_git
        try:
            record = prove(gate_cmd="pytest -q", workspace=repo)
        finally:
            v._git = real_git
            # clean up the dangling stash the fake created
            subprocess.run(["git", "stash", "drop"], cwd=repo,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertTrue(record.aborted)
        self.assertIn("stash", record.error)
        self.assertFalse(record.proven)


class RunGatesTests(unittest.TestCase):
    """Fail-fast multi-gate runner; the underlying run_gate is faked."""

    def setUp(self):
        import kickass_loop_engineer.verifier as v
        self.v = v
        self.calls = []
        self.real_run_gate = v.run_gate

    def tearDown(self):
        self.v.run_gate = self.real_run_gate

    def _fake_run_gate(self, passes_by_gate):
        def fake(name, command, workspace, with_proof=False, policy=None):
            self.calls.append({"name": name, "command": command, "workspace": workspace,
                               "with_proof": with_proof, "policy": policy})
            return EvidenceRecord(gate=name, command=command,
                                  passed=passes_by_gate[name])
        self.v.run_gate = fake

    def test_all_gates_green_passes_with_records_in_spec_order(self):
        self._fake_run_gate({"unit": True, "smoke": True})
        result = run_gates([GateSpec(name="unit", command="pytest -q"),
                            GateSpec(name="smoke", command="make test")],
                           workspace="/ws")
        self.assertIsInstance(result, GatesResult)
        self.assertTrue(result.passed)
        self.assertEqual([r.gate for r in result.records], ["unit", "smoke"])

    def test_first_gate_failure_stops_before_second_gate_runs(self):
        self._fake_run_gate({"unit": False, "smoke": True})
        result = run_gates([GateSpec(name="unit", command="pytest -q"),
                            GateSpec(name="smoke", command="make test")],
                           workspace="/ws")
        self.assertFalse(result.passed)
        self.assertEqual([r.gate for r in result.records], ["unit"])
        self.assertEqual(len(self.calls), 1)  # fail-fast: smoke never ran

    def test_to_dict_wraps_verdict_and_per_gate_record_dicts(self):
        self._fake_run_gate({"unit": True, "smoke": True})
        result = run_gates([GateSpec(name="unit", command="pytest -q"),
                            GateSpec(name="smoke", command="make test")],
                           workspace="/ws")
        d = result.to_dict()
        self.assertEqual(d, {"passed": True,
                             "gates": [r.to_dict() for r in result.records]})

    def test_empty_spec_list_never_passes_vacuously(self):
        self._fake_run_gate({})
        result = run_gates([], workspace="/ws")
        self.assertFalse(result.passed)
        self.assertEqual(result.to_dict(), {"passed": False, "gates": []})

    def test_per_gate_prove_flag_reaches_the_runner(self):
        self._fake_run_gate({"unit": True, "smoke": True})
        run_gates([GateSpec(name="unit", command="pytest -q", prove=True),
                   GateSpec(name="smoke", command="make test", prove=False)],
                  workspace="/ws")
        self.assertEqual([c["with_proof"] for c in self.calls], [True, False])

    def test_policy_is_threaded_to_every_gate_run(self):
        self._fake_run_gate({"unit": True, "smoke": True})
        sentinel = object()
        run_gates([GateSpec(name="unit", command="pytest -q"),
                   GateSpec(name="smoke", command="make test")],
                  workspace="/ws", policy=sentinel)
        self.assertEqual([c["policy"] for c in self.calls], [sentinel, sentinel])


@dataclass
class _ObserverStubPolicy(VerificationPolicy):
    """Records every run; mutable fields survive the dataclasses.replace clone."""

    log: list = field(default_factory=list)
    stdout: str = "A11Y TREE: button 'Save' visible"
    stderr: str = ""
    error: str = ""

    def run(self, command, cwd):
        self.log.append({"command": command, "cwd": cwd,
                         "timeout": self.timeout_seconds,
                         "tail_chars": self.tail_chars,
                         "prefixes": self.allowed_prefixes})
        if self.error:
            return VerificationResult(passed=False, command=command, error=self.error)
        return VerificationResult(passed=True, command=command, returncode=0,
                                  stdout_tail=self.stdout, stderr_tail=self.stderr)


@dataclass
class _ExplodingPolicy(VerificationPolicy):
    """A policy whose run always raises — the observer must swallow it."""

    def run(self, command, cwd):
        raise RuntimeError("boom")


class ObserverTests(unittest.TestCase):
    """Failing gates run their observe command — the eyes for the retry loop."""

    OBSERVE = "npx playwright-cli snapshot http://localhost:3000"

    def setUp(self):
        import kickass_loop_engineer.verifier as v
        self.v = v
        self.gate_calls = []
        self.real_run_gate = v.run_gate

    def tearDown(self):
        self.v.run_gate = self.real_run_gate

    def _fake_run_gate(self, passes_by_gate):
        def fake(name, command, workspace, with_proof=False, policy=None):
            self.gate_calls.append(name)
            return EvidenceRecord(gate=name, command=command,
                                  passed=passes_by_gate[name])
        self.v.run_gate = fake

    def test_failing_gate_with_observe_runs_observer_via_policy(self):
        self._fake_run_gate({"ui": False})
        policy = _ObserverStubPolicy(allowed_prefixes=("npx",))
        result = run_gates([GateSpec(name="ui", command="npx playwright test",
                                     prove=False, observe=self.OBSERVE)],
                           workspace="/ws", policy=policy)
        self.assertFalse(result.passed)
        self.assertEqual(result.records[-1].observed, policy.stdout)
        self.assertEqual(len(policy.log), 1)
        call = policy.log[0]
        self.assertEqual(call["command"], self.OBSERVE)
        self.assertEqual(call["cwd"], "/ws")
        self.assertEqual(call["timeout"], OBSERVER_TIMEOUT_SECONDS)
        self.assertEqual(call["tail_chars"], OBSERVER_CAP)
        self.assertEqual(call["prefixes"], ("npx",))  # SAME allowlist authority

    def test_passing_gates_never_run_observer(self):
        self._fake_run_gate({"ui": True})
        policy = _ObserverStubPolicy()
        result = run_gates([GateSpec(name="ui", command="npx playwright test",
                                     prove=False, observe=self.OBSERVE)],
                           workspace="/ws", policy=policy)
        self.assertTrue(result.passed)
        self.assertEqual(policy.log, [])
        self.assertEqual(result.records[0].observed, "")

    def test_failing_gate_without_observe_runs_no_observer(self):
        self._fake_run_gate({"unit": False})
        policy = _ObserverStubPolicy()
        result = run_gates([GateSpec(name="unit", command="pytest -q",
                                     prove=False)],
                           workspace="/ws", policy=policy)
        self.assertFalse(result.passed)
        self.assertEqual(policy.log, [])
        self.assertEqual(result.records[0].observed, "")

    def test_observer_error_warns_and_leaves_observed_empty(self):
        self._fake_run_gate({"ui": False})
        policy = _ObserverStubPolicy(error="command not in verification allowlist")
        with self.assertLogs("kickass_loop_engineer.verifier", level="WARNING"):
            result = run_gates([GateSpec(name="ui", command="npx playwright test",
                                         prove=False, observe=self.OBSERVE)],
                               workspace="/ws", policy=policy)
        self.assertFalse(result.passed)          # the gate failure still stands
        self.assertEqual(result.records[-1].observed, "")

    def test_observer_exception_never_crashes_the_run(self):
        self._fake_run_gate({"ui": False})
        with self.assertLogs("kickass_loop_engineer.verifier", level="WARNING"):
            result = run_gates([GateSpec(name="ui", command="npx playwright test",
                                         prove=False, observe=self.OBSERVE)],
                               workspace="/ws", policy=_ExplodingPolicy())
        self.assertFalse(result.passed)
        self.assertEqual(result.records[-1].observed, "")

    def test_observed_output_is_capped(self):
        self._fake_run_gate({"ui": False})
        policy = _ObserverStubPolicy(stdout="x" * (OBSERVER_CAP * 3))
        result = run_gates([GateSpec(name="ui", command="npx playwright test",
                                     prove=False, observe=self.OBSERVE)],
                           workspace="/ws", policy=policy)
        self.assertEqual(len(result.records[-1].observed), OBSERVER_CAP)

    def test_stderr_tail_is_folded_into_observed(self):
        self._fake_run_gate({"ui": False})
        policy = _ObserverStubPolicy(stdout="dom snapshot",
                                     stderr="console error: undefined is not a function")
        result = run_gates([GateSpec(name="ui", command="npx playwright test",
                                     prove=False, observe=self.OBSERVE)],
                           workspace="/ws", policy=policy)
        observed = result.records[-1].observed
        self.assertIn("dom snapshot", observed)
        self.assertIn("undefined is not a function", observed)

    def test_fail_fast_unchanged_observer_runs_after_failing_gate(self):
        self._fake_run_gate({"unit": False, "smoke": True})
        policy = _ObserverStubPolicy()
        result = run_gates([GateSpec(name="unit", command="pytest -q",
                                     prove=False, observe="pytest --collect-only -q"),
                            GateSpec(name="smoke", command="make test",
                                     prove=False, observe=self.OBSERVE)],
                           workspace="/ws", policy=policy)
        self.assertEqual(self.gate_calls, ["unit"])   # smoke never ran
        self.assertEqual(len(policy.log), 1)          # one observer, for the failure
        self.assertEqual(policy.log[0]["command"], "pytest --collect-only -q")
        self.assertEqual(result.records[-1].observed, policy.stdout)


class EvidenceObservedSerializationTests(unittest.TestCase):
    def test_to_dict_omits_observed_when_empty(self):
        record = EvidenceRecord(gate="ui", command="npx playwright test", passed=False)
        self.assertNotIn("observed", record.to_dict())

    def test_to_dict_includes_observed_when_nonempty(self):
        record = EvidenceRecord(gate="ui", command="npx playwright test",
                                passed=False, observed="A11Y TREE")
        self.assertEqual(record.to_dict()["observed"], "A11Y TREE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
