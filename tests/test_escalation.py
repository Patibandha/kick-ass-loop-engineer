# tests/test_escalation.py
"""Tests for the escalation denylist."""
import unittest
from kickass_loop_engineer import guardrails
from kickass_loop_engineer.escalation import PROTECTED_GLOBS, should_escalate


class EscalationTests(unittest.TestCase):
    def test_safe_action_does_not_escalate(self):
        esc, reason = should_escalate(action="write a unit test", paths=["src/app.py"],
                                      files_touched=2, attempt=1)
        self.assertFalse(esc)
        self.assertEqual(reason, "")

    def test_risky_action_escalates(self):
        for act in ("deploy to prod", "delete the table", "spend $50", "DROP TABLE users"):
            esc, reason = should_escalate(action=act)
            self.assertTrue(esc, act)
            self.assertTrue(reason)

    def test_protected_path_escalates(self):
        for p in (".env", "secrets/key.pem", "migrations/001.sql", "src/auth/login.py",
                  "billing/payments.py"):
            esc, _ = should_escalate(paths=[p])
            self.assertTrue(esc, p)

    def test_too_many_files_escalates(self):
        self.assertTrue(should_escalate(files_touched=11, max_files=10)[0])
        self.assertFalse(should_escalate(files_touched=10, max_files=10)[0])

    def test_third_attempt_escalates(self):
        self.assertTrue(should_escalate(attempt=3, max_attempts=3)[0])
        self.assertFalse(should_escalate(attempt=2, max_attempts=3)[0])

    def test_escalation_globs_are_superset_of_write_policy_globs(self):
        self.assertTrue(set(guardrails.DEFAULT_PROTECTED) <= set(PROTECTED_GLOBS))

    def test_ssh_key_path_escalates(self):  # id_rsa was only in guardrails before
        escalate, reason = should_escalate(paths=["id_rsa"])
        self.assertTrue(escalate)
        self.assertIn("protected path", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
