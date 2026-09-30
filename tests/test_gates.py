"""Tests for the gate registry and allowlist coverage."""
import subprocess
import unittest
from unittest import mock
from kickass_loop_engineer.gates import GATES, GateSpec, gate_categories
from kickass_loop_engineer.guardrails import VerificationPolicy


class GateRegistryTests(unittest.TestCase):
    def test_seven_gate_categories_present(self):
        self.assertEqual(gate_categories(),
                         {"unit", "security", "data_leak", "performance", "smoke",
                          "ui", "architecture"})

    def test_ui_gate_registered_with_matching_category(self):
        self.assertIn("ui", GATES)
        self.assertEqual(GATES["ui"]["category"], "ui")

    def test_architecture_gate_registered_with_matching_category(self):
        self.assertIn("architecture", GATES)
        self.assertEqual(GATES["architecture"]["category"], "architecture")

    def test_every_gate_command_is_allowlisted(self):
        policy = VerificationPolicy()
        for name, spec in GATES.items():
            ok, reason = policy.validate(spec["example"])
            self.assertTrue(ok, f"gate {name} example not allowlisted: {reason}")

    def test_security_and_dataleak_tools_in_allowlist(self):
        policy = VerificationPolicy()
        for cmd in ("bandit -r .", "semgrep --error", "gitleaks detect", "detect-secrets scan"):
            self.assertTrue(policy.validate(cmd)[0], f"{cmd} should be allowed")

    def test_ui_and_architecture_tools_in_allowlist(self):
        policy = VerificationPolicy()
        for cmd in ("npx playwright test",
                    "npx playwright-cli snapshot http://localhost:3000",
                    "npx --yes @probelabs/maid check design.md",
                    "lint-imports"):
            self.assertTrue(policy.validate(cmd)[0], f"{cmd} should be allowed")

    def test_blanket_npx_stays_rejected(self):
        self.assertIs(VerificationPolicy().validate("npx some-random-package")[0], False)


class GateSpecTests(unittest.TestCase):
    def test_defaults(self):
        spec = GateSpec()
        self.assertEqual(spec.name, "unit")
        self.assertEqual(spec.command, "python3 -m pytest -q")
        self.assertIs(spec.prove, True)
        self.assertEqual(spec.observe, "")

    def test_importable_from_orchestrator_for_back_compat(self):
        from kickass_loop_engineer.orchestrator import GateSpec as OrchGateSpec
        self.assertIs(OrchGateSpec, GateSpec)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class GateExpectationTests(unittest.TestCase):
    """A gate may declare what its OUTPUT must prove, not just its exit code.

    The hole this closes, seen on 2026-09-21: a Docker image built from a cache
    holding zero-byte files ran `pytest -q tests/...`, collected NOTHING, exited
    0, and the gate passed. Every desk command in that image exited 0 too,
    because every module was empty. A green suite that ran no tests is not
    evidence, and `returncode == 0` alone cannot tell the difference.

    The subprocess is stubbed: these cases are about the verdict logic, and a
    real `python3` is not present on every platform the engine runs on.
    """

    GATE = "python3 -m pytest -q"

    def run_gate(self, stdout="", stderr="", returncode=0, **expectation):
        """Run one allowlisted gate with a stubbed subprocess result."""
        completed = subprocess.CompletedProcess(
            args=["python3"], returncode=returncode, stdout=stdout, stderr=stderr)
        with mock.patch("kickass_loop_engineer.guardrails.subprocess.run",
                        return_value=completed):
            return VerificationPolicy().run(self.GATE, cwd=".", **expectation)

    def test_a_gate_with_no_expectation_still_passes_on_exit_zero(self):
        self.assertTrue(self.run_gate(stdout="whatever").passed)

    def test_output_that_does_not_match_the_expectation_fails(self):
        """`no tests ran` exits 0; the expectation is what catches it."""
        result = self.run_gate(stdout="no tests ran in 0.04s", expect=r"(\d+) passed")
        self.assertFalse(result.passed)
        self.assertIn("expected output", result.error)
        self.assertIn("no tests ran", result.error)

    def test_output_that_matches_the_expectation_passes(self):
        self.assertTrue(
            self.run_gate(stdout="3216 passed in 41.54s", expect=r"(\d+) passed").passed)

    def test_a_count_below_the_minimum_fails_even_though_it_matched(self):
        """A suite that shrank is a regression, not a pass."""
        result = self.run_gate(stdout="12 passed in 0.1s",
                               expect=r"(\d+) passed", min_count=3216)
        self.assertFalse(result.passed)
        self.assertIn("12", result.error)
        self.assertIn("3216", result.error)

    def test_a_count_at_or_above_the_minimum_passes(self):
        self.assertTrue(self.run_gate(stdout="3216 passed in 41.5s",
                                      expect=r"(\d+) passed", min_count=3216).passed)

    def test_a_failing_exit_code_still_fails_whatever_the_output_says(self):
        result = self.run_gate(stdout="9999 passed", returncode=1,
                               expect=r"(\d+) passed", min_count=1)
        self.assertFalse(result.passed)

    def test_the_expectation_may_match_stderr(self):
        """Some tools report their summary on stderr."""
        self.assertTrue(self.run_gate(stderr="9 passed",
                                      expect=r"(\d+) passed", min_count=9).passed)

    def test_an_invalid_expectation_regex_fails_the_gate_loudly(self):
        result = self.run_gate(stdout="3216 passed", expect=r"(unclosed")
        self.assertFalse(result.passed)
        self.assertIn("not a valid regex", result.error)

    def test_a_min_count_needs_a_capturing_expectation(self):
        result = self.run_gate(stdout="3216 passed", expect=r"passed", min_count=1)
        self.assertFalse(result.passed)
        self.assertIn("capture a number", result.error)

    def test_gatespec_carries_the_expectation(self):
        spec = GateSpec(name="unit", command=self.GATE,
                        expect=r"(\d+) passed", min_count=3216)
        self.assertEqual(spec.expect, r"(\d+) passed")
        self.assertEqual(spec.min_count, 3216)

    def test_gatespec_defaults_to_no_expectation(self):
        self.assertEqual(GateSpec().expect, "")
        self.assertIsNone(GateSpec().min_count)


class GateExpectationPlumbingTests(unittest.TestCase):
    """A gate's declared expectation must survive config -> spec -> runner.

    The logic above is worth nothing if the configured value never reaches the
    subprocess verdict, which is exactly the class of gap that let a gate run
    against a container that did not exist.
    """

    def test_parse_gates_reads_expect_and_min_count(self):
        from kickass_loop_engineer.config import parse_gates
        specs = parse_gates({"gates": [{
            "name": "unit", "cmd": "python3 -m pytest -q", "prove": False,
            "expect": r"(\d+) passed", "min_count": 3216}]})
        self.assertEqual(specs[0].expect, r"(\d+) passed")
        self.assertEqual(specs[0].min_count, 3216)

    def test_a_gate_without_an_expectation_parses_to_none(self):
        from kickass_loop_engineer.config import parse_gates
        specs = parse_gates({"gates": [{"name": "unit", "cmd": "python3 -m pytest -q"}]})
        self.assertEqual(specs[0].expect, "")
        self.assertIsNone(specs[0].min_count)

    def test_run_gates_fails_the_hollow_suite_that_exits_zero(self):
        """End to end: the 2026-09-21 failure, now caught."""
        from kickass_loop_engineer.verifier import run_gates
        specs = [GateSpec(name="unit", command="python3 -m pytest -q", prove=False,
                          expect=r"(\d+) passed", min_count=3216)]
        completed = subprocess.CompletedProcess(
            args=["python3"], returncode=0, stdout="no tests ran in 0.04s", stderr="")
        with mock.patch("kickass_loop_engineer.guardrails.subprocess.run",
                        return_value=completed):
            result = run_gates(specs, workspace=".")
        self.assertFalse(result.passed)
        self.assertIn("expected output", result.records[0].evidence)

    def test_run_gates_passes_the_same_gate_on_a_real_suite(self):
        from kickass_loop_engineer.verifier import run_gates
        specs = [GateSpec(name="unit", command="python3 -m pytest -q", prove=False,
                          expect=r"(\d+) passed", min_count=3216)]
        completed = subprocess.CompletedProcess(
            args=["python3"], returncode=0, stdout="3216 passed in 41.54s", stderr="")
        with mock.patch("kickass_loop_engineer.guardrails.subprocess.run",
                        return_value=completed):
            result = run_gates(specs, workspace=".")
        self.assertTrue(result.passed)
