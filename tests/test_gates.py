"""Tests for the gate registry and allowlist coverage."""
import unittest
from kickass_loop_engineer.gates import GATES, gate_categories
from kickass_loop_engineer.guardrails import VerificationPolicy


class GateRegistryTests(unittest.TestCase):
    def test_five_gate_categories_present(self):
        self.assertEqual(gate_categories(),
                         {"unit", "security", "data_leak", "performance", "smoke"})

    def test_every_gate_command_is_allowlisted(self):
        policy = VerificationPolicy()
        for name, spec in GATES.items():
            ok, reason = policy.validate(spec["example"])
            self.assertTrue(ok, f"gate {name} example not allowlisted: {reason}")

    def test_security_and_dataleak_tools_in_allowlist(self):
        policy = VerificationPolicy()
        for cmd in ("bandit -r .", "semgrep --error", "gitleaks detect", "detect-secrets scan"):
            self.assertTrue(policy.validate(cmd)[0], f"{cmd} should be allowed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
