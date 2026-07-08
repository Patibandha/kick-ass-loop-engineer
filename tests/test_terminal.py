"""Tests for the terminal-state taxonomy."""
import unittest
from kickass_loop_engineer.terminal import TerminalState, RunOutcome


class TerminalStateTests(unittest.TestCase):
    def test_states_cover_the_spec_taxonomy(self):
        names = {s.value for s in TerminalState}
        self.assertEqual(names, {"success", "stalled", "blocked",
                                 "budget_exceeded", "approval_required", "oscillation",
                                 "research_blocked"})

    def test_outcome_carries_evidence_and_is_serializable(self):
        outcome = RunOutcome(TerminalState.SUCCESS, reason="all gates green",
                             evidence=[{"gate": "unit", "passed": True}])
        d = outcome.to_dict()
        self.assertEqual(d["state"], "success")
        self.assertEqual(d["reason"], "all gates green")
        self.assertEqual(d["evidence"][0]["gate"], "unit")

    def test_success_requires_evidence(self):
        with self.assertRaises(ValueError):
            RunOutcome(TerminalState.SUCCESS, reason="done", evidence=[])

    def test_research_blocked_is_a_terminal_state(self):
        self.assertEqual(TerminalState.RESEARCH_BLOCKED.value, "research_blocked")

    def test_research_blocked_outcome_needs_no_evidence(self):
        outcome = RunOutcome(TerminalState.RESEARCH_BLOCKED,
                             reason="load-bearing claim is assumed")
        self.assertEqual(outcome.to_dict()["state"], "research_blocked")
        self.assertEqual(outcome.evidence, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
