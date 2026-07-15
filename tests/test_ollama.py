"""Tests for the Ollama provider's usage parsing against a stub server."""
import json

from kickass_loop_engineer.providers.ollama import OllamaProvider

from helpers import StubServerTestCase

_HAPPY_BODY = json.dumps({
    "message": {"role": "assistant", "content": "hello from ollama stub"},
    "prompt_eval_count": 7,
    "eval_count": 5,
})


class OllamaUsageTests(StubServerTestCase):
    """Token accounting parsed from the Ollama chat response body."""

    def make_provider(self):
        return OllamaProvider(model="test-model", host=self.base_url, timeout_seconds=10.0)

    def test_happy_path_fills_prompt_completion_split(self):
        self.set_canned(_HAPPY_BODY)
        result = self.make_provider().complete("sys", "user")
        self.assertEqual(result.text, "hello from ollama stub")
        self.assertEqual(result.prompt_tokens, 7)
        self.assertEqual(result.completion_tokens, 5)
        self.assertEqual(result.tokens, 12)

    def test_null_usage_counts_yield_zero_tokens_without_raising(self):
        self.set_canned(json.dumps({
            "message": {"role": "assistant", "content": "x"},
            "prompt_eval_count": None,
            "eval_count": None,
        }))
        result = self.make_provider().complete("sys", "user")
        self.assertEqual(result.prompt_tokens, 0)
        self.assertEqual(result.completion_tokens, 0)
        self.assertEqual(result.tokens, 0)

    def test_non_numeric_usage_counts_yield_zero_tokens_without_raising(self):
        self.set_canned(json.dumps({
            "message": {"role": "assistant", "content": "x"},
            "prompt_eval_count": "not-a-number",
            "eval_count": 5,
        }))
        result = self.make_provider().complete("sys", "user")
        self.assertEqual(result.prompt_tokens, 0)
        self.assertEqual(result.completion_tokens, 5)
        self.assertEqual(result.tokens, 5)
