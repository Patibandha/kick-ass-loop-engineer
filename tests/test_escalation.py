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


class TestWordBoundaryTokens:
    def test_linuxdeploy_should_not_read_as_deploy(self):
        from kickass_loop_engineer.escalation import should_escalate
        ok, reason = should_escalate(
            action="build the AppImage via linuxdeploy and appimagetool")
        assert ok is False
        assert reason == ""

    def test_a_real_deploy_should_still_escalate(self):
        from kickass_loop_engineer.escalation import should_escalate
        ok, reason = should_escalate(action="deploy the service to prod")
        assert ok is True
        assert "deploy" in reason


class TestAllowTokens:
    """`allow_tokens` relaxes the KEYWORD layer for one run — and nothing else."""

    def test_allowed_token_lets_a_legitimate_objective_through(self):
        ok, reason = should_escalate(
            action="deploy the unit file to /etc/systemd/system",
            allow_tokens=["deploy"])
        assert ok is False
        assert reason == ""

    def test_other_risky_tokens_still_escalate(self):
        ok, reason = should_escalate(action="delete the table",
                                     allow_tokens=["deploy"])
        assert ok is True
        assert "delete" in reason

    def test_allow_list_is_case_insensitive_and_stripped(self):
        for allowed in (["DEPLOY"], ["  Deploy  "], ["dEpLoY"]):
            ok, _ = should_escalate(action="deploy the unit file",
                                    allow_tokens=allowed)
            assert ok is False, allowed

    def test_none_and_empty_keep_the_default_denylist(self):
        for allowed in (None, [], ()):
            ok, reason = should_escalate(action="deploy to prod",
                                         allow_tokens=allowed)
            assert ok is True, allowed
            assert "deploy" in reason

    def test_allow_list_never_relaxes_protected_paths(self):
        ok, reason = should_escalate(action="deploy the unit file",
                                     paths=["secrets/key.pem"],
                                     allow_tokens=["deploy", "delete", "spend"])
        assert ok is True
        assert "protected path" in reason

    def test_allow_list_never_relaxes_the_file_cap(self):
        ok, reason = should_escalate(action="deploy the unit file",
                                     files_touched=11, max_files=10,
                                     allow_tokens=["deploy"])
        assert ok is True
        assert "too many files" in reason

    def test_allow_list_never_relaxes_the_attempt_cap(self):
        ok, reason = should_escalate(action="deploy the unit file",
                                     attempt=3, max_attempts=3,
                                     allow_tokens=["deploy"])
        assert ok is True
        assert "attempt 3" in reason

    def test_spend_can_be_exempted_for_a_model_spend_cap_objective(self):
        ok, _ = should_escalate(action="add a model spend cap to the ledger",
                                allow_tokens=["spend"])
        assert ok is False
