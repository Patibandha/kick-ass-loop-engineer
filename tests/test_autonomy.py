# tests/test_autonomy.py
"""Tests for L3-gated autonomy."""
import unittest
from kickass_loop_engineer.autonomy import AutonomyLevel, ReadinessChecklist, resolve_level


class AutonomyTests(unittest.TestCase):
    def _ready(self, **over):
        base = dict(verifier_proven=True, budget_set=True,
                    denylist_active=True, workspace_isolated=True)
        base.update(over)
        return ReadinessChecklist(**base)

    def test_levels(self):
        self.assertEqual(AutonomyLevel.L1.value, "report_only")
        self.assertEqual(AutonomyLevel.L2.value, "assisted")
        self.assertEqual(AutonomyLevel.L3.value, "unattended")

    def test_checklist_ready_and_gaps(self):
        self.assertTrue(self._ready().ready())
        c = self._ready(budget_set=False, verifier_proven=False)
        self.assertFalse(c.ready())
        self.assertIn("budget_set", c.gaps())
        self.assertIn("verifier_proven", c.gaps())

    def test_l3_engages_when_ready(self):
        self.assertEqual(resolve_level(AutonomyLevel.L3, self._ready()), AutonomyLevel.L3)

    def test_l3_auto_drops_to_l2_when_not_ready(self):
        self.assertEqual(resolve_level(AutonomyLevel.L3, self._ready(verifier_proven=False)),
                         AutonomyLevel.L2)

    def test_l1_and_l2_pass_through_regardless(self):
        not_ready = self._ready(budget_set=False)
        self.assertEqual(resolve_level(AutonomyLevel.L1, not_ready), AutonomyLevel.L1)
        self.assertEqual(resolve_level(AutonomyLevel.L2, not_ready), AutonomyLevel.L2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
