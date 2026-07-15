# tests/test_review.py
"""Tests for the cross-model reviewer and the builder/reviewer agents."""
import logging
import unittest
from kickass_loop_engineer.review import model_family, CrossModelReviewer, CrossModelReviewError
from kickass_loop_engineer.agents import BUILDER_SYSTEM, Builder, Reviewer
from kickass_loop_engineer.objective import Objective
from kickass_loop_engineer.providers.base import Provider, ProviderError, ProviderResult

from helpers import FakeProvider  # plain import: pytest inserts tests/ on sys.path (rootdir)


class ScriptedProvider(Provider):
    def __init__(self, text): self.text = text
    def complete(self, system, user): return ProviderResult(text=self.text, tokens=1, cost_usd=0.0)


class SequencedResultProvider(Provider):
    """Pops queued full ProviderResults; records prompts for assertions."""

    name = "sequenced"

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def complete(self, system, user):
        self.calls.append((system, user))
        return self.results.pop(0)


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

    def test_two_unknown_families_error_hints_at_family_declaration(self):
        with self.assertRaises(CrossModelReviewError) as ctx:
            CrossModelReviewer("mystery-a", "mystery-b", self._reviewer("NO FINDINGS"))
        self.assertIn("family:", str(ctx.exception))


class DeclaredFamilyTests(unittest.TestCase):
    """Config-declared families override detection (3.0); rejection semantics unchanged."""

    def _reviewer(self, text):
        return Reviewer(ScriptedProvider(text))

    def test_declared_distinct_families_allow_unknown_models(self):
        cmr = CrossModelReviewer("mystery-a", "mystery-b", self._reviewer("NO FINDINGS"),
                                 builder_family="alpha", reviewer_family="beta")
        self.assertEqual(cmr.builder_family, "alpha")
        self.assertEqual(cmr.reviewer_family, "beta")

    def test_declared_same_family_rejected(self):
        with self.assertRaises(CrossModelReviewError):
            CrossModelReviewer("mystery-a", "mystery-b", self._reviewer("NO FINDINGS"),
                               builder_family="alpha", reviewer_family="alpha")

    def test_declared_builder_family_vs_detected_reviewer_passes(self):
        cmr = CrossModelReviewer("mystery-a", "qwen2.5", self._reviewer("NO FINDINGS"),
                                 builder_family="alpha")
        self.assertEqual(cmr.builder_family, "alpha")
        self.assertEqual(cmr.reviewer_family, "qwen")

    def test_declared_builder_family_equal_to_detected_reviewer_rejected(self):
        with self.assertRaises(CrossModelReviewError):
            CrossModelReviewer("mystery-a", "qwen2.5", self._reviewer("NO FINDINGS"),
                               builder_family="qwen")


class WinnerIdentityCheckTests(unittest.TestCase):
    """Layer-2 invariant: the WINNING attempt's constructed provider object is
    re-verified against the reviewer family at review time — a label is never
    identity."""

    def _cmr(self, builder_model="kimi-k2.7-code", reviewer_model="qwen2.5", **kw):
        return CrossModelReviewer(builder_model, reviewer_model,
                                  Reviewer(ScriptedProvider("NO FINDINGS")), **kw)

    def test_check_passes_for_cross_family_winner_provider(self):
        cmr = self._cmr()
        cmr.check(FakeProvider([], model="kimi-k2-thinking"))  # must not raise

    def test_check_rejects_winner_provider_in_the_reviewer_family(self):
        cmr = self._cmr()
        with self.assertRaises(CrossModelReviewError):
            cmr.check(FakeProvider([], model="qwen2.5-coder"))

    def test_check_reads_identity_off_the_provider_object_not_a_label(self):
        # The reviewer was constructed believing the builder is kimi; the
        # actual winner provider object carries a qwen identity — the object
        # wins, the construction-time label must not bypass the guard.
        cmr = self._cmr(builder_model="kimi-claimed-label")
        with self.assertRaises(CrossModelReviewError):
            cmr.check(FakeProvider([], model="qwen2.5"))

    def test_check_falls_back_to_provider_name_when_model_is_empty(self):
        cmr = self._cmr(reviewer_model="claude-opus-4-8")
        provider = FakeProvider([], model="")
        provider.name = "claude_code"
        with self.assertRaises(CrossModelReviewError):
            cmr.check(provider)

    def test_check_rejects_two_unknown_families(self):
        cmr = self._cmr(reviewer_model="mystery-r")
        with self.assertRaises(CrossModelReviewError):
            cmr.check(FakeProvider([], model="mystery-w"))

    def test_check_declared_family_asserts_independence_for_unknown_models(self):
        cmr = self._cmr(reviewer_model="mystery-r", reviewer_family="beta")
        cmr.check(FakeProvider([], model="mystery-w"), family="alpha")  # no raise

    def test_check_declared_family_equal_to_reviewer_family_rejected(self):
        cmr = self._cmr()
        with self.assertRaises(CrossModelReviewError):
            cmr.check(FakeProvider([], model="mystery-w"), family="qwen")


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


class ParseFindingsFunctionTests(unittest.TestCase):
    """Module-level ``parse_findings`` — the promoted M2 parser entry point."""

    def test_parses_finding_lines_including_indented_ones(self):
        from kickass_loop_engineer.agents import parse_findings
        text = "prose\n  FINDING: missing null check\nFINDING: second\n"
        self.assertEqual(parse_findings(text),
                         ["missing null check", "second"])

    def test_matches_prefix_case_insensitively_and_ignores_bare_prefix(self):
        from kickass_loop_engineer.agents import parse_findings
        self.assertEqual(parse_findings("finding: lower\nFINDING:\n"),
                         ["lower"])

    def test_handles_none_and_empty_text(self):
        from kickass_loop_engineer.agents import parse_findings
        self.assertEqual(parse_findings(None), [])
        self.assertEqual(parse_findings(""), [])

    def test_reviewer_static_method_delegates_to_the_module_function(self):
        from kickass_loop_engineer.agents import parse_findings
        self.assertIs(Reviewer._parse_findings, parse_findings)


AGENTS_LOGGER = "kickass_loop_engineer.agents"


class BuilderContextWindowTests(unittest.TestCase):
    """Builder warns when the estimated prompt tops 75% of the context window."""

    def _big_objective(self):
        """An objective whose brief pushes the char//4 estimate past 750 tokens."""
        return Objective(goal="x" * 4000, done_when="pytest -q passes")

    def _small_objective(self):
        return Objective(goal="tiny goal", done_when="pytest -q passes")

    def test_should_warn_when_prompt_exceeds_75_percent_of_window(self):
        builder = Builder(FakeProvider(["ok"]), context_window=1000)
        with self.assertLogs(AGENTS_LOGGER, level="WARNING") as captured:
            builder.build(self._big_objective())
        self.assertTrue(any("context window" in line for line in captured.output))

    def test_warning_names_model_and_both_numbers(self):
        provider = FakeProvider(["ok"], model="fake-model")
        builder = Builder(provider, context_window=1000)
        objective = self._big_objective()
        estimated = (len(BUILDER_SYSTEM) + len(objective.builder_brief(""))) // 4
        with self.assertLogs(AGENTS_LOGGER, level="WARNING") as captured:
            builder.build(objective)
        message = captured.output[0]
        self.assertIn("fake-model", message)
        self.assertIn(str(estimated), message)
        self.assertIn("1000", message)

    def test_should_not_warn_below_75_percent_of_window(self):
        builder = Builder(FakeProvider(["ok"]), context_window=1000)
        with self.assertLogs(AGENTS_LOGGER, level="WARNING") as captured:
            logging.getLogger(AGENTS_LOGGER).warning("sentinel")
            builder.build(self._small_objective())
        self.assertEqual(len(captured.output), 1)  # only the sentinel

    def test_should_never_warn_when_window_is_zero_default(self):
        builder = Builder(FakeProvider(["ok"]))
        with self.assertLogs(AGENTS_LOGGER, level="WARNING") as captured:
            logging.getLogger(AGENTS_LOGGER).warning("sentinel")
            builder.build(self._big_objective())
        self.assertEqual(len(captured.output), 1)  # only the sentinel

    def test_warning_path_still_sends_the_same_brief_to_the_provider(self):
        provider = FakeProvider(["ok"])
        builder = Builder(provider, context_window=1000)
        objective = self._big_objective()
        with self.assertLogs(AGENTS_LOGGER, level="WARNING"):
            builder.build(objective)
        self.assertEqual(provider.calls[0][1], objective.builder_brief(""))


MALFORMED = "The code seems okay overall; a few loose thoughts follow."


class ReviewerFormatRetryTests(unittest.TestCase):
    def test_should_retry_once_and_parse_findings_from_second_reply(self):
        provider = FakeProvider([MALFORMED, "FINDING: missing tests\n"])
        review = Reviewer(provider).review(OBJ, "snapshot")
        self.assertEqual(review.findings, ["missing tests"])
        self.assertEqual(len(provider.calls), 2)

    def test_should_include_format_reminder_in_retry_prompt(self):
        provider = FakeProvider([MALFORMED, "FINDING: missing tests\n"])
        Reviewer(provider).review(OBJ, "snapshot")
        retry_prompt = provider.calls[1][1]
        self.assertIn("'FINDING: <defect>'", retry_prompt)
        self.assertIn("'NO FINDINGS'", retry_prompt)

    def test_should_not_retry_when_first_reply_is_no_findings(self):
        provider = FakeProvider(["NO FINDINGS\n"])
        review = Reviewer(provider).review(OBJ, "snapshot")
        self.assertEqual(review.findings, [])
        self.assertEqual(len(provider.calls), 1)

    def test_should_not_retry_when_first_reply_has_findings(self):
        provider = FakeProvider(["FINDING: one defect\n"])
        review = Reviewer(provider).review(OBJ, "snapshot")
        self.assertEqual(review.findings, ["one defect"])
        self.assertEqual(len(provider.calls), 1)

    def test_should_not_retry_when_format_retries_is_zero(self):
        provider = FakeProvider([MALFORMED, "FINDING: never reached\n"])
        review = Reviewer(provider, format_retries=0).review(OBJ, "snapshot")
        self.assertEqual(review.findings, [])
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(review.feedback, MALFORMED)

    def test_should_degrade_to_empty_findings_when_retry_still_malformed(self):
        provider = FakeProvider([MALFORMED, "still just prose, sorry"])
        review = Reviewer(provider).review(OBJ, "snapshot")
        self.assertEqual(review.findings, [])
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(review.feedback, "still just prose, sorry")

    def test_should_sum_tokens_and_cost_across_retry_calls(self):
        provider = SequencedResultProvider([
            ProviderResult(text=MALFORMED, tokens=7, cost_usd=0.5,
                           model="m-first", prompt_tokens=3, completion_tokens=4),
            ProviderResult(text="FINDING: real defect", tokens=11, cost_usd=0.25,
                           model="m-last", prompt_tokens=5, completion_tokens=6),
        ])
        review = Reviewer(provider).review(OBJ, "snapshot")
        self.assertEqual(review.result.tokens, 18)
        self.assertAlmostEqual(review.result.cost_usd, 0.75)
        self.assertEqual(review.result.prompt_tokens, 8)
        self.assertEqual(review.result.completion_tokens, 10)
        self.assertEqual(review.result.model, "m-last")
        self.assertEqual(review.result.text, "FINDING: real defect")

    def test_should_not_retry_when_first_reply_is_empty(self):
        provider = FakeProvider([""])
        review = Reviewer(provider).review(OBJ, "snapshot")
        self.assertEqual(review.findings, [])
        self.assertEqual(review.feedback, "")
        self.assertEqual(len(provider.calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

class _RetryRaisingReviewProvider(SequencedResultProvider):
    """Pops queued results, then raises ProviderError when exhausted."""

    def complete(self, system, user):
        if not self.results:
            self.calls.append((system, user))
            raise ProviderError("ollama connection refused")
        return super().complete(system, user)


class ReviewerPartialSpendTests(unittest.TestCase):
    """A ProviderError from the reviewer's corrective retry call must not lose
    the spend of the completed first call."""

    def test_retry_provider_error_carries_first_call_spend(self):
        provider = _RetryRaisingReviewProvider([
            ProviderResult(text="rambling prose, neither format", tokens=60,
                           cost_usd=0.4, model="paid-reviewer",
                           prompt_tokens=45, completion_tokens=15),
        ])
        with self.assertRaises(ProviderError) as ctx:
            Reviewer(provider).review(OBJ, "snapshot")
        partial = ctx.exception.partial_result
        self.assertEqual(partial.tokens, 60)
        self.assertAlmostEqual(partial.cost_usd, 0.4)
        self.assertEqual(partial.prompt_tokens, 45)
        self.assertEqual(partial.completion_tokens, 15)
        self.assertEqual(partial.model, "paid-reviewer")

    def test_first_call_provider_error_has_no_partial_result(self):
        provider = _RetryRaisingReviewProvider([])
        with self.assertRaises(ProviderError) as ctx:
            Reviewer(provider).review(OBJ, "snapshot")
        self.assertIsNone(getattr(ctx.exception, "partial_result", None))


class ReviewerOnRetryCallbackTests(unittest.TestCase):
    """The optional on_retry hook fires exactly once per corrective format
    retry, and its absence changes nothing."""

    def test_on_retry_fires_once_for_one_corrective_call(self):
        provider = FakeProvider([MALFORMED, "NO FINDINGS"])
        calls = []
        Reviewer(provider, on_retry=lambda: calls.append(1)).review(OBJ, "snap")
        self.assertEqual(len(calls), 1)

    def test_on_retry_not_called_on_wellformed_first_reply(self):
        provider = FakeProvider(["FINDING: one defect\n"])
        calls = []
        Reviewer(provider, on_retry=lambda: calls.append(1)).review(OBJ, "snap")
        self.assertEqual(calls, [])

    def test_on_retry_not_called_when_first_reply_is_empty(self):
        provider = FakeProvider([""])
        calls = []
        Reviewer(provider, on_retry=lambda: calls.append(1)).review(OBJ, "snap")
        self.assertEqual(calls, [])

    def test_on_retry_not_called_when_retries_disabled(self):
        provider = FakeProvider([MALFORMED, "FINDING: never reached\n"])
        calls = []
        Reviewer(provider, format_retries=0,
                 on_retry=lambda: calls.append(1)).review(OBJ, "snap")
        self.assertEqual(calls, [])

    def test_absent_callback_keeps_retry_behavior_unchanged(self):
        provider = FakeProvider([MALFORMED, "FINDING: missing tests\n"])
        review = Reviewer(provider).review(OBJ, "snapshot")
        self.assertEqual(review.findings, ["missing tests"])
        self.assertEqual(len(provider.calls), 2)
