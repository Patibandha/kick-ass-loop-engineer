"""Tests for the Anthropic API provider's usage parsing against a stub server."""
import json
from unittest import mock

from kickass_loop_engineer.providers import anthropic_api
from kickass_loop_engineer.providers.anthropic_api import AnthropicProvider

from helpers import StubServerTestCase

_HAPPY_BODY = json.dumps({
    "content": [{"type": "text", "text": "hello from anthropic stub"}],
    "usage": {"input_tokens": 3, "output_tokens": 4},
})


class AnthropicUsageTests(StubServerTestCase):
    """Token accounting parsed from the Messages API ``usage`` block."""

    def make_provider(self):
        provider = AnthropicProvider(model="test-model", api_key="test-key", timeout_seconds=10.0)
        return provider

    def complete(self):
        """Run one completion with the endpoint redirected to the stub server."""
        with mock.patch.object(anthropic_api, "_ENDPOINT", f"{self.base_url}/v1/messages"):
            return self.make_provider().complete("sys", "user")

    def test_happy_path_fills_prompt_completion_split(self):
        self.set_canned(_HAPPY_BODY)
        result = self.complete()
        self.assertEqual(result.text, "hello from anthropic stub")
        self.assertEqual(result.prompt_tokens, 3)
        self.assertEqual(result.completion_tokens, 4)
        self.assertEqual(result.tokens, 7)

    def test_null_usage_counts_yield_zero_tokens_without_raising(self):
        self.set_canned(json.dumps({
            "content": [{"type": "text", "text": "x"}],
            "usage": {"input_tokens": None, "output_tokens": None},
        }))
        result = self.complete()
        self.assertEqual(result.prompt_tokens, 0)
        self.assertEqual(result.completion_tokens, 0)
        self.assertEqual(result.tokens, 0)

    def test_missing_usage_block_yields_zero_tokens(self):
        self.set_canned(json.dumps({"content": [{"type": "text", "text": "x"}]}))
        result = self.complete()
        self.assertEqual(result.prompt_tokens, 0)
        self.assertEqual(result.completion_tokens, 0)
        self.assertEqual(result.tokens, 0)
