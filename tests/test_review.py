# tests/test_review.py
"""Tests for the cross-model reviewer."""
import unittest
from kickass_loop_engineer.review import model_family, CrossModelReviewer, CrossModelReviewError
from kickass_loop_engineer.agents import Reviewer
from kickass_loop_engineer.objective import Objective
from kickass_loop_engineer.providers.base import Provider, ProviderResult

from helpers import FakeProvider  # plain import: pytest inserts tests/ on sys.path (rootdir)


class ScriptedProvider(Provider):
    def __init__(self, text): self.text = text
    def complete(self, system, user): return ProviderResult(text=self.text, tokens=1, cost_usd=0.0)


OBJ = Objective(goal="build a todo CLI", done_when="pytest passes")


class ModelFamilyTests(unittest.TestCase):
    def test_families(self):
        self.assertEqual(model_family("kimi-k2.7-code:cloud"), "kimi")
        self.assertEqual(model_family("qwen2.5"), "qwen")
        self.assertEqual(model_family("claude-opus-4-8"), "claude")
        self.assertEqual(model_family("anthropic/claude"), "claude")
        self.assertEqual(model_family("something-else"), "unknown")


class CrossModelReviewerTests(unittest.TestCase):
    def _reviewer(self, text):
        return Reviewer(ScriptedProvider(text))

    def test_rejects_same_family_builder_and_reviewer(self):
        with self.assertRaises(CrossModelReviewError):
            CrossModelReviewer("kimi-k2.7-code", "kimi-k2-thinking", self._reviewer("NO FINDINGS"))

    def test_cross_family_review_runs(self):
        cmr = CrossModelReviewer("kimi-k2.7-code", "qwen2.5", self._reviewer("NO FINDINGS"))
        review = cmr.review(Objective("g", "done"), "snapshot")
        self.assertEqual(review.findings, [])

    def test_two_unknown_families_rejected(self):
        with self.assertRaises(CrossModelReviewError):
            CrossModelReviewer("mystery-a", "mystery-b", self._reviewer("NO FINDINGS"))


class ReviewerFindingsTests(unittest.TestCase):
    def test_reviewer_parses_finding_lines(self):
        provider = FakeProvider(["Looks mostly fine.\nFINDING: no error handling in load()\n"
                                  "FINDING: magic number 42 in cli.py\n"])
        review = Reviewer(provider).review(OBJ, "snapshot")
        self.assertEqual(
            review.findings,
            ["no error handling in load()", "magic number 42 in cli.py"],
        )
        self.assertIn("Looks mostly fine.", review.feedback)

    def test_reviewer_parses_indented_finding_lines(self):
        provider = FakeProvider(["  FINDING: missing null check\nFINDING: second\n"])
        review = Reviewer(provider).review(OBJ, "snapshot")
        self.assertEqual(review.findings, ["missing null check", "second"])

    def test_reviewer_matches_finding_prefix_case_insensitively(self):
        provider = FakeProvider(["finding: lower case works\n"])
        review = Reviewer(provider).review(OBJ, "snapshot")
        self.assertEqual(review.findings, ["lower case works"])

    def test_reviewer_ignores_bare_finding_prefix(self):
        provider = FakeProvider(["FINDING:\nFINDING: real one\n"])
        review = Reviewer(provider).review(OBJ, "snapshot")
        self.assertEqual(review.findings, ["real one"])

    def test_reviewer_finding_lines_win_over_no_findings(self):
        provider = FakeProvider(["NO FINDINGS\nFINDING: real\n"])
        review = Reviewer(provider).review(OBJ, "snapshot")
        self.assertEqual(review.findings, ["real"])

    def test_reviewer_with_no_findings_returns_empty_list(self):
        provider = FakeProvider(["NO FINDINGS\n"])
        review = Reviewer(provider).review(OBJ, "snapshot")
        self.assertEqual(review.findings, [])

    def test_review_has_no_approved_attribute(self):
        provider = FakeProvider(["NO FINDINGS\n"])
        review = Reviewer(provider).review(OBJ, "snapshot")
        self.assertFalse(hasattr(review, "approved"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
