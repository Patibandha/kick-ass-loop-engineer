"""Tests for BuildRound token accounting carried from the provider result."""
import tempfile
import unittest

from kickass_loop_engineer.agents import Builder
from kickass_loop_engineer.objective import Objective
from kickass_loop_engineer.providers.base import ProviderError, ProviderResult
from kickass_loop_engineer.session import BuildRound, BuildSession
from kickass_loop_engineer.workspace import Workspace

from helpers import FakeProvider

_BUILDER_TEXT = "=== FILE: hello.txt ===\nhi\n=== END FILE ===\n"
_FENCE_REMINDER = "=== FILE: relative/path.ext ===\n<full contents>\n=== END FILE ==="


class _PerCallProvider(FakeProvider):
    """FakeProvider variant whose queued responses carry per-call usage."""

    def __init__(self, results):
        super().__init__([r.text for r in results])
        self._results = list(results)

    def complete(self, system, user):
        self.calls.append((system, user))
        return self._results.pop(0)


def _session(provider, root, **kwargs):
    """Build a BuildSession around a fake provider and a temp workspace."""
    return BuildSession(
        builder=Builder(provider),
        workspace=Workspace(root=root),
        objective=Objective(goal="say hi", done_when="hello.txt exists"),
        **kwargs,
    )


class BuildRoundFieldTests(unittest.TestCase):
    """BuildRound carries the prompt/completion token split."""

    def test_token_split_fields_default_to_zero(self):
        round_ = BuildRound(round_no=1)
        self.assertEqual(round_.prompt_tokens, 0)
        self.assertEqual(round_.completion_tokens, 0)

    def test_to_dict_includes_token_split(self):
        data = BuildRound(round_no=1, prompt_tokens=6, completion_tokens=4).to_dict()
        self.assertEqual(data["prompt_tokens"], 6)
        self.assertEqual(data["completion_tokens"], 4)


class BuildRoundCarriesUsageTests(unittest.TestCase):
    """build_round copies the provider's token split into the round result."""

    def test_build_round_copies_token_split_from_provider_result(self):
        provider = FakeProvider(
            [_BUILDER_TEXT], tokens=10, prompt_tokens=6, completion_tokens=4
        )
        with tempfile.TemporaryDirectory() as root:
            session = BuildSession(
                builder=Builder(provider),
                workspace=Workspace(root=root),
                objective=Objective(goal="say hi", done_when="hello.txt exists"),
            )
            round_ = session.build_round(1)
        self.assertEqual(round_.tokens, 10)
        self.assertEqual(round_.prompt_tokens, 6)
        self.assertEqual(round_.completion_tokens, 4)
        self.assertEqual(round_.files_written, ["hello.txt"])


class FormatRetryTests(unittest.TestCase):
    """build_round issues one corrective retry when no valid FILE blocks appear."""

    def test_prose_then_valid_block_writes_on_corrective_call(self):
        provider = FakeProvider(
            ["sure, here is the file you asked for", _BUILDER_TEXT]
        )
        with tempfile.TemporaryDirectory() as root:
            round_ = _session(provider, root).build_round(1)
        self.assertEqual(round_.files_written, ["hello.txt"])
        self.assertEqual(len(provider.calls), 2)

    def test_corrective_prompt_contains_format_error_and_fence_reminder(self):
        provider = FakeProvider(["sure, here is the file", _BUILDER_TEXT])
        with tempfile.TemporaryDirectory() as root:
            _session(provider, root).build_round(1)
        _system, user = provider.calls[1]
        self.assertIn("FORMAT ERROR", user)
        self.assertIn(_FENCE_REMINDER, user)

    def test_both_responses_malformed_stops_after_two_calls(self):
        provider = FakeProvider(["prose one, no blocks", "prose two, no blocks"])
        with tempfile.TemporaryDirectory() as root:
            round_ = _session(provider, root).build_round(1)
        self.assertEqual(round_.files_written, [])
        self.assertEqual(len(provider.calls), 2)

    def test_format_retries_zero_makes_single_call(self):
        provider = FakeProvider(["prose only, no blocks"])
        with tempfile.TemporaryDirectory() as root:
            round_ = _session(provider, root, format_retries=0).build_round(1)
        self.assertEqual(round_.files_written, [])
        self.assertEqual(len(provider.calls), 1)

    def test_format_retries_two_allows_at_most_three_calls(self):
        provider = FakeProvider(["prose one", "prose two", "prose three"])
        with tempfile.TemporaryDirectory() as root:
            round_ = _session(provider, root, format_retries=2).build_round(1)
        self.assertEqual(round_.files_written, [])
        self.assertEqual(len(provider.calls), 3)

    def test_empty_response_is_not_retried(self):
        provider = FakeProvider([""])
        with tempfile.TemporaryDirectory() as root:
            round_ = _session(provider, root).build_round(1)
        self.assertEqual(round_.files_written, [])
        self.assertEqual(len(provider.calls), 1)

    def test_valid_first_response_makes_single_call(self):
        provider = FakeProvider([_BUILDER_TEXT])
        with tempfile.TemporaryDirectory() as root:
            round_ = _session(provider, root).build_round(1)
        self.assertEqual(round_.files_written, ["hello.txt"])
        self.assertEqual(len(provider.calls), 1)

    def test_retried_round_sums_usage_and_carries_last_text(self):
        provider = _PerCallProvider([
            ProviderResult(text="no blocks here", tokens=10, cost_usd=0.1,
                           model="fake-model", prompt_tokens=6,
                           completion_tokens=4),
            ProviderResult(text=_BUILDER_TEXT, tokens=20, cost_usd=0.3,
                           model="fake-model", prompt_tokens=12,
                           completion_tokens=8),
        ])
        with tempfile.TemporaryDirectory() as root:
            round_ = _session(provider, root).build_round(1)
        self.assertEqual(round_.tokens, 30)
        self.assertAlmostEqual(round_.cost_usd, 0.4)
        self.assertEqual(round_.prompt_tokens, 18)
        self.assertEqual(round_.completion_tokens, 12)
        self.assertEqual(round_.builder_text, _BUILDER_TEXT)

class _RetryRaisingProvider(_PerCallProvider):
    """Serves queued per-call results, then raises ProviderError when exhausted."""

    def complete(self, system, user):
        if not self._results:
            self.calls.append((system, user))
            raise ProviderError("ollama connection refused")
        return super().complete(system, user)


class PartialSpendOnRetryFailureTests(unittest.TestCase):
    """A ProviderError from the CORRECTIVE retry call must not lose the spend
    of the round's completed first call."""

    def test_retry_provider_error_carries_first_call_spend(self):
        provider = _RetryRaisingProvider([
            ProviderResult(text="prose without file blocks", tokens=40,
                           cost_usd=0.25, model="paid-model",
                           prompt_tokens=30, completion_tokens=10),
        ])
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ProviderError) as ctx:
                _session(provider, root).build_round(1)
        partial = ctx.exception.partial_result
        self.assertEqual(partial.tokens, 40)
        self.assertAlmostEqual(partial.cost_usd, 0.25)
        self.assertEqual(partial.prompt_tokens, 30)
        self.assertEqual(partial.completion_tokens, 10)
        self.assertEqual(partial.model, "paid-model")

    def test_first_call_provider_error_has_no_partial_result(self):
        provider = _RetryRaisingProvider([])
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ProviderError) as ctx:
                _session(provider, root).build_round(1)
        self.assertIsNone(getattr(ctx.exception, "partial_result", None))


class OnRetryCallbackTests(unittest.TestCase):
    """The optional on_retry hook fires exactly once per corrective format
    retry, and its absence changes nothing."""

    def test_on_retry_fires_once_for_one_corrective_call(self):
        provider = FakeProvider(["prose without blocks", _BUILDER_TEXT])
        calls = []
        with tempfile.TemporaryDirectory() as root:
            round_ = _session(provider, root,
                              on_retry=lambda: calls.append(1)).build_round(1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(round_.files_written, ["hello.txt"])

    def test_on_retry_fires_once_per_corrective_call(self):
        provider = FakeProvider(["prose one", "prose two", "prose three"])
        calls = []
        with tempfile.TemporaryDirectory() as root:
            _session(provider, root, format_retries=2,
                     on_retry=lambda: calls.append(1)).build_round(1)
        self.assertEqual(len(calls), 2)

    def test_on_retry_not_called_when_first_response_is_valid(self):
        provider = FakeProvider([_BUILDER_TEXT])
        calls = []
        with tempfile.TemporaryDirectory() as root:
            _session(provider, root,
                     on_retry=lambda: calls.append(1)).build_round(1)
        self.assertEqual(calls, [])

    def test_on_retry_not_called_for_empty_response(self):
        provider = FakeProvider([""])
        calls = []
        with tempfile.TemporaryDirectory() as root:
            _session(provider, root,
                     on_retry=lambda: calls.append(1)).build_round(1)
        self.assertEqual(calls, [])

    def test_absent_callback_keeps_retry_behavior_unchanged(self):
        provider = FakeProvider(["prose without blocks", _BUILDER_TEXT])
        with tempfile.TemporaryDirectory() as root:
            round_ = _session(provider, root).build_round(1)
        self.assertEqual(round_.files_written, ["hello.txt"])
        self.assertEqual(len(provider.calls), 2)
