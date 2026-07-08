# tests/test_ensemble.py
"""Tests for ensemble verified selection."""
import unittest
from kickass_loop_engineer.ensemble import Attempt, select_winner, run_ensemble


def _ev(passed):
    class E:
        pass
    e = E(); e.passed = passed
    return e


class SelectWinnerTests(unittest.TestCase):
    def test_picks_the_passing_attempt(self):
        a = Attempt("a", "/ws/a", _ev(False), file_count=3)
        b = Attempt("b", "/ws/b", _ev(True), file_count=5)
        self.assertEqual(select_winner([a, b]).id, "b")

    def test_tiebreak_prefers_fewer_files(self):
        a = Attempt("a", "/ws/a", _ev(True), file_count=8)
        b = Attempt("b", "/ws/b", _ev(True), file_count=2)
        self.assertEqual(select_winner([a, b]).id, "b")

    def test_none_when_no_attempt_passes(self):
        a = Attempt("a", "/ws/a", _ev(False), file_count=1)
        self.assertIsNone(select_winner([a]))


class RunEnsembleTests(unittest.TestCase):
    def test_runs_n_attempts_and_selects_verified_winner(self):
        def build_fn(i):
            return (f"/ws/{i}", i + 2)
        def gate_fn(ws):
            return _ev(ws == "/ws/1")
        attempts = run_ensemble(2, build_fn, gate_fn)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(select_winner(attempts).id, "1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
