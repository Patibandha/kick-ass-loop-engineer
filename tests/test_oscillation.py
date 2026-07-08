# tests/test_oscillation.py
"""Tests for oscillation detection."""
import unittest
from kickass_loop_engineer.oscillation import OscillationDetector


class OscillationTests(unittest.TestCase):
    def test_same_findings_two_rounds_triggers_stop(self):
        d = OscillationDetector(window=2)
        self.assertFalse(d.observe(["bug: x", "bug: y"]))
        self.assertTrue(d.observe(["bug: y", "bug: x"]))

    def test_new_evidence_resets(self):
        d = OscillationDetector(window=2)
        self.assertFalse(d.observe(["bug: x"]))
        self.assertFalse(d.observe(["bug: x", "bug: z"]))
        self.assertTrue(d.observe(["bug: z", "bug: x"]))

    def test_empty_findings_never_oscillates(self):
        d = OscillationDetector(window=2)
        self.assertFalse(d.observe([]))
        self.assertFalse(d.observe([]))

    def test_history_is_bounded(self):
        det = OscillationDetector(window=2)
        for i in range(1000):
            det.observe([f"finding-{i}"])
        self.assertLessEqual(len(det._history), det._history.maxlen)
        self.assertLessEqual(det._history.maxlen, 64)


if __name__ == "__main__":
    unittest.main(verbosity=2)
