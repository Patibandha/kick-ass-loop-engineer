# tests/test_ensemble.py
"""Tests for ensemble verified selection."""
import unittest
from kickass_loop_engineer.ensemble import (
    Attempt,
    AttemptSpec,
    default_specs,
    run_ensemble,
    select_winner,
)


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


class DefaultSpecsTests(unittest.TestCase):
    def test_single_attempt_uses_the_low_baseline_temperature(self):
        self.assertEqual(default_specs(1), [AttemptSpec(temperature=0.2)])

    def test_three_attempts_follow_the_ladder(self):
        self.assertEqual([s.temperature for s in default_specs(3)],
                         [0.2, 0.7, 1.0])

    def test_five_attempts_cap_at_one_point_five(self):
        self.assertEqual([s.temperature for s in default_specs(5)],
                         [0.2, 0.7, 1.0, 1.3, 1.5])

    def test_specs_are_deterministic(self):
        self.assertEqual(default_specs(4), default_specs(4))

    def test_default_specs_carry_no_model_or_provider_override(self):
        for spec in default_specs(3):
            self.assertEqual(spec.model, "")
            self.assertEqual(spec.provider, "")
            self.assertEqual(spec.family, "")


class RunEnsembleSpecsTests(unittest.TestCase):
    def test_specs_are_attached_to_their_attempts(self):
        specs = default_specs(2)
        attempts = run_ensemble(2, lambda i: (f"/ws/{i}", 1),
                                lambda ws: _ev(True), specs=specs)
        self.assertEqual([a.spec for a in attempts], specs)

    def test_without_specs_attempt_spec_stays_none(self):
        attempts = run_ensemble(2, lambda i: (f"/ws/{i}", 1),
                                lambda ws: _ev(True))
        self.assertEqual([a.spec for a in attempts], [None, None])


if __name__ == "__main__":
    unittest.main(verbosity=2)
