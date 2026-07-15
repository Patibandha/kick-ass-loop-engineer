"""Tests for the Claude Code provider's usage parsing with a stubbed subprocess."""
import json
import subprocess
import unittest
from unittest import mock

from kickass_loop_engineer.providers.base import ProviderError
from kickass_loop_engineer.providers.claude_code import ClaudeCodeProvider

_HAPPY_STDOUT = json.dumps({
    "is_error": False,
    "result": "hello from claude stub",
    "usage": {"input_tokens": 3, "output_tokens": 4},
    "total_cost_usd": 0.01,
})


class ClaudeCodeUsageTests(unittest.TestCase):
    """Token accounting parsed from Claude Code's JSON ``usage`` block."""

    def complete(self, stdout: str):
        """Run one completion with ``subprocess.run`` stubbed to return stdout."""
        completed = subprocess.CompletedProcess(args=["claude"], returncode=0, stdout=stdout, stderr="")
        with mock.patch("kickass_loop_engineer.providers.claude_code.subprocess.run", return_value=completed):
            return ClaudeCodeProvider(model="test-model").complete("sys", "user")

    def test_happy_path_fills_prompt_completion_split(self):
        result = self.complete(_HAPPY_STDOUT)
        self.assertEqual(result.text, "hello from claude stub")
        self.assertEqual(result.prompt_tokens, 3)
        self.assertEqual(result.completion_tokens, 4)
        self.assertEqual(result.tokens, 7)
        self.assertEqual(result.cost_usd, 0.01)

    def test_null_usage_counts_yield_zero_tokens_without_raising(self):
        result = self.complete(json.dumps({
            "is_error": False,
            "result": "x",
            "usage": {"input_tokens": None, "output_tokens": None},
        }))
        self.assertEqual(result.prompt_tokens, 0)
        self.assertEqual(result.completion_tokens, 0)
        self.assertEqual(result.tokens, 0)

    def test_missing_usage_block_yields_zero_tokens(self):
        result = self.complete(json.dumps({"is_error": False, "result": "x"}))
        self.assertEqual(result.prompt_tokens, 0)
        self.assertEqual(result.completion_tokens, 0)
        self.assertEqual(result.tokens, 0)


class ClaudeCodeCwdTests(unittest.TestCase):
    """The configured ``cwd`` must reach ``subprocess.run`` unchanged."""

    def run_kwargs(self, **provider_kwargs):
        """Run one completion and return the kwargs ``subprocess.run`` received."""
        completed = subprocess.CompletedProcess(args=["claude"], returncode=0,
                                                stdout=_HAPPY_STDOUT, stderr="")
        with mock.patch("kickass_loop_engineer.providers.claude_code.subprocess.run",
                        return_value=completed) as run_mock:
            ClaudeCodeProvider(model="test-model", **provider_kwargs).complete("sys", "user")
        return run_mock.call_args.kwargs

    def test_configured_cwd_is_passed_to_subprocess_run(self):
        self.assertEqual(self.run_kwargs(cwd="/tmp/x")["cwd"], "/tmp/x")

    def test_default_cwd_is_none(self):
        self.assertIsNone(self.run_kwargs()["cwd"])

class ClaudeCodeFileNotFoundAttributionTests(unittest.TestCase):
    """A FileNotFoundError must name the true culprit: missing binary vs bad cwd."""

    def complete_raising(self, exc, **provider_kwargs):
        """Run one completion with ``subprocess.run`` stubbed to raise *exc*."""
        with mock.patch("kickass_loop_engineer.providers.claude_code.subprocess.run",
                        side_effect=exc):
            ClaudeCodeProvider(model="test-model", **provider_kwargs).complete("sys", "user")

    def test_missing_binary_is_attributed_to_the_binary(self):
        exc = FileNotFoundError(2, "No such file or directory")
        exc.filename = "claude"
        with self.assertRaises(ProviderError) as ctx:
            self.complete_raising(exc, cwd="/tmp")
        self.assertIn("claude binary not found", str(ctx.exception))

    def test_missing_cwd_is_attributed_to_the_cwd(self):
        exc = FileNotFoundError(2, "No such file or directory")
        exc.filename = "/gone/workspace"
        with self.assertRaises(ProviderError) as ctx:
            self.complete_raising(exc, cwd="/gone/workspace")
        message = str(ctx.exception)
        self.assertIn("cwd does not exist", message)
        self.assertIn("/gone/workspace", message)
        self.assertNotIn("binary not found", message)
