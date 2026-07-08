# tests/test_ledger.py
"""Tests for the cost ledger."""
import json, os, tempfile, unittest
from kickass_loop_engineer.ledger import CostLedger


class CostLedgerTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "ledger.json")

    def test_run_cap_blocks_overspend(self):
        led = CostLedger(self.path, run_cap_usd=10.0, month="2026-06")
        self.assertFalse(led.would_exceed_run(9.0))
        led.record(9.0)
        self.assertTrue(led.would_exceed_run(2.0))
        self.assertEqual(led.run_total(), 9.0)

    def test_monthly_total_persists_across_instances(self):
        CostLedger(self.path, month="2026-06").record(3.0)
        again = CostLedger(self.path, month="2026-06")
        self.assertEqual(again.month_total(), 3.0)

    def test_month_advisory_threshold(self):
        led = CostLedger(self.path, month_cap_usd=200.0, warn_ratio=0.8, month="2026-06")
        led.record(150.0)
        self.assertFalse(led.month_warning())
        led.record(20.0)
        self.assertTrue(led.month_warning())

    def test_separate_months_do_not_mix(self):
        CostLedger(self.path, month="2026-06").record(5.0)
        self.assertEqual(CostLedger(self.path, month="2026-07").month_total(), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
