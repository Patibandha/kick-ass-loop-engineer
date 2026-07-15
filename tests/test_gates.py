"""Tests for the gate registry and allowlist coverage."""
import unittest
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
