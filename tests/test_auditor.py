# tests/test_auditor.py
"""Tests for the loop-auditor verdicts."""
import unittest
from kickass_loop_engineer.auditor import classify_run


class AuditorTests(unittest.TestCase):
    def test_high_hit_low_waste_keep(self):
        self.assertEqual(classify_run(hit_rate=0.9, waste_ratio=0.1), "KEEP")

    def test_low_hit_high_waste_kill(self):
        self.assertEqual(classify_run(hit_rate=0.05, waste_ratio=0.9), "KILL")

    def test_decent_hit_high_waste_pivot(self):
        self.assertEqual(classify_run(hit_rate=0.5, waste_ratio=0.7), "PIVOT")

    def test_low_hit_low_waste_retire(self):
        self.assertEqual(classify_run(hit_rate=0.1, waste_ratio=0.2), "RETIRE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
