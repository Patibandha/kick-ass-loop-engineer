"""Tests for the Claude Code provider with a stubbed subprocess.

Covers both hats the provider wears: the read-only prose path (``complete``)
and the agentic in-place builder path (``edit``), plus the shared transient
retry. The real ``claude`` binary is never invoked and no call ever sleeps —
the retry helper's sleep is injected and recorded.
"""
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from kickass_loop_engineer.providers import base
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


def _completed(stdout: str = _HAPPY_STDOUT, returncode: int = 0, stderr: str = ""):
    """Build a ``CompletedProcess`` standing in for one ``claude`` run."""
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def _recording_retry(recorded: list):
    """Return a ``retry_transient`` stand-in that records delays instead of sleeping.

    The real retry policy still runs (classification, attempt budget, backoff
    arithmetic); only the wait is replaced, so a retry test costs no wall time.
    """
    def spy(call, **kwargs):
        kwargs["sleep"] = recorded.append
        return base.retry_transient(call, **kwargs)
    return spy


class ClaudeCodeProseToolsTests(unittest.TestCase):
    """``complete`` is the READ-ONLY path: decomposers and reviewers never edit."""

    def test_default_allowed_tools_are_read_only(self):
        self.assertEqual(ClaudeCodeProvider().allowed_tools,
                         ("Read", "Glob", "Grep"))

    def test_complete_argv_carries_only_the_read_only_tools(self):
        with mock.patch("kickass_loop_engineer.providers.claude_code.subprocess.run",
                        return_value=_completed()) as run_mock:
            ClaudeCodeProvider().complete("sys", "user")
        command = run_mock.call_args.args[0]
        self.assertEqual(command[command.index("--allowedTools") + 1],
                         "Read,Glob,Grep")
        for forbidden in ("Edit", "Write", "Bash"):
            self.assertNotIn(forbidden, command[command.index("--allowedTools") + 1])

    def test_explicit_allowed_tools_still_win(self):
        provider = ClaudeCodeProvider(allowed_tools=("Read", "Bash"))
        self.assertEqual(provider.allowed_tools, ("Read", "Bash"))


class ClaudeCodeEditTests(unittest.TestCase):
    """The agentic in-place path: argv, working directory, and timeout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def edit_call(self, **provider_kwargs):
        """Run one edit and return the mock that captured ``subprocess.run``."""
        with mock.patch("kickass_loop_engineer.providers.claude_code.subprocess.run",
                        return_value=_completed()) as run_mock:
            ClaudeCodeProvider(**provider_kwargs).edit("sys", "user", cwd=self.tmp.name)
        return run_mock

    def test_provider_declares_in_place_editing(self):
        self.assertTrue(ClaudeCodeProvider().edits_in_place)

    def test_edit_argv_carries_the_edit_tools_permission_mode_and_model(self):
        command = self.edit_call(model="test-model").call_args.args[0]
        self.assertEqual(command[:4],
                         ["claude", "-p", "--output-format", "json"])
        self.assertEqual(command[command.index("--allowedTools") + 1],
                         "Read,Edit,Write,Glob,Grep,Bash")
        self.assertEqual(command[command.index("--permission-mode") + 1],
                         "acceptEdits")
        self.assertEqual(command[command.index("--model") + 1], "test-model")

    def test_edit_argv_honors_custom_tools_and_permission_mode(self):
        command = self.edit_call(edit_tools=("Read", "Write"),
                                 edit_permission_mode="bypassPermissions"
                                 ).call_args.args[0]
        self.assertEqual(command[command.index("--allowedTools") + 1], "Read,Write")
        self.assertEqual(command[command.index("--permission-mode") + 1],
                         "bypassPermissions")

    def test_edit_omits_the_model_flag_when_unset(self):
        self.assertNotIn("--model", self.edit_call().call_args.args[0])

    def test_edit_passes_the_given_cwd_and_edit_timeout(self):
        kwargs = self.edit_call(edit_timeout_seconds=900.0).call_args.kwargs
        self.assertEqual(kwargs["cwd"], self.tmp.name)
        self.assertEqual(kwargs["timeout"], 900.0)

    def test_edit_default_timeout_is_thirty_minutes(self):
        self.assertEqual(self.edit_call().call_args.kwargs["timeout"], 1800.0)

    def test_edit_ignores_the_constructor_cwd(self):
        kwargs = self.edit_call(cwd="/somewhere/else").call_args.kwargs
        self.assertEqual(kwargs["cwd"], self.tmp.name)

    def test_edit_returns_the_parsed_prose_summary(self):
        with mock.patch("kickass_loop_engineer.providers.claude_code.subprocess.run",
                        return_value=_completed()):
            result = ClaudeCodeProvider(model="test-model").edit(
                "sys", "user", cwd=self.tmp.name)
        self.assertEqual(result.text, "hello from claude stub")
        self.assertEqual(result.tokens, 7)

    def test_edit_strips_api_keys_when_forcing_subscription(self):
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k"}, clear=False):
            env = self.edit_call().call_args.kwargs["env"]
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_edit_rejects_a_missing_cwd_before_spawning_anything(self):
        with mock.patch("kickass_loop_engineer.providers.claude_code.subprocess.run") as run_mock:
            with self.assertRaises(ProviderError) as ctx:
                ClaudeCodeProvider().edit("sys", "user", cwd="/gone/workspace")
        message = str(ctx.exception)
        self.assertIn("requires an existing cwd", message)
        self.assertIn("/gone/workspace", message)
        run_mock.assert_not_called()

    def test_edit_rejects_an_empty_cwd(self):
        with self.assertRaises(ProviderError):
            ClaudeCodeProvider().edit("sys", "user", cwd="")

    def test_edit_rejects_a_file_as_cwd(self):
        with tempfile.NamedTemporaryFile() as handle:
            with self.assertRaises(ProviderError):
                ClaudeCodeProvider().edit("sys", "user", cwd=handle.name)


class ClaudeCodeRetryTests(unittest.TestCase):
    """Transient CLI failures are retried; permanent ones fail on the first call."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.delays = []

    def run_with(self, outcome, method="complete", **provider_kwargs):
        """Drive one call with ``subprocess.run`` stubbed by *outcome*.

        A list of results is served one per call; a single ``CompletedProcess``
        is returned for every call; an exception instance is raised by each.
        """
        stub = ({"return_value": outcome}
                if isinstance(outcome, subprocess.CompletedProcess)
                else {"side_effect": outcome})
        with mock.patch("kickass_loop_engineer.providers.claude_code.retry_transient",
                        _recording_retry(self.delays)):
            with mock.patch("kickass_loop_engineer.providers.claude_code.subprocess.run",
                            **stub) as run_mock:
                provider = ClaudeCodeProvider(**provider_kwargs)
                if method == "edit":
                    result = provider.edit("sys", "user", cwd=self.tmp.name)
                else:
                    result = provider.complete("sys", "user")
        return result, run_mock

    def test_overloaded_non_zero_exit_is_retried_then_succeeds(self):
        result, run_mock = self.run_with([
            _completed("", returncode=1, stderr="API Error: 529 overloaded_error"),
            _completed(),
        ])
        self.assertEqual(result.text, "hello from claude stub")
        self.assertEqual(run_mock.call_count, 2)
        self.assertEqual(self.delays, [2.0])

    def test_edit_retries_an_overloaded_turn_too(self):
        result, run_mock = self.run_with([
            _completed("", returncode=1, stderr="API Error: 529 overloaded_error"),
            _completed(),
        ], method="edit")
        self.assertEqual(result.text, "hello from claude stub")
        self.assertEqual(run_mock.call_count, 2)
        self.assertEqual(self.delays, [2.0])

    def test_reported_is_error_overload_is_retried(self):
        overloaded = json.dumps({"is_error": True, "result": "Overloaded"})
        result, run_mock = self.run_with([_completed(overloaded), _completed()])
        self.assertEqual(result.text, "hello from claude stub")
        self.assertEqual(run_mock.call_count, 2)

    def test_exhausted_retries_raise_the_last_transient_error(self):
        with self.assertRaises(ProviderError) as ctx:
            self.run_with(_completed("", returncode=1, stderr="HTTP 529 overloaded"))
        self.assertIn("529", str(ctx.exception))
        self.assertEqual(self.delays, [2.0, 4.0])

    def test_non_transient_non_zero_exit_is_not_retried(self):
        with self.assertRaises(ProviderError) as ctx:
            self.run_with(_completed("", returncode=1, stderr="invalid model name"))
        self.assertIn("invalid model name", str(ctx.exception))
        self.assertEqual(self.delays, [])

    def test_missing_binary_is_not_retried(self):
        exc = FileNotFoundError(2, "No such file or directory")
        exc.filename = "claude"
        with self.assertRaises(ProviderError) as ctx:
            self.run_with(exc)
        self.assertIn("binary not found", str(ctx.exception))
        self.assertEqual(self.delays, [])

    def test_retry_settings_are_configurable(self):
        with self.assertRaises(ProviderError):
            self.run_with(_completed("", returncode=1, stderr="HTTP 503"),
                          retry_attempts=4, retry_base_delay=0.5)
        self.assertEqual(self.delays, [0.5, 1.0, 2.0])

    def test_retry_attempts_of_one_disables_retrying(self):
        with self.assertRaises(ProviderError):
            self.run_with(_completed("", returncode=1, stderr="HTTP 529"),
                          retry_attempts=1)
        self.assertEqual(self.delays, [])


class ClaudeCodeNonZeroExitTests(unittest.TestCase):
    """A non-zero exit reports whatever the CLI said, stderr first."""

    def complete(self, completed):
        with mock.patch("kickass_loop_engineer.providers.claude_code.subprocess.run",
                        return_value=completed):
            return ClaudeCodeProvider(retry_attempts=1).complete("sys", "user")

    def test_stderr_is_surfaced(self):
        with self.assertRaises(ProviderError) as ctx:
            self.complete(_completed("", returncode=1, stderr="boom on stderr"))
        self.assertIn("boom on stderr", str(ctx.exception))

    def test_stdout_is_the_fallback_detail(self):
        with self.assertRaises(ProviderError) as ctx:
            self.complete(_completed("detail on stdout", returncode=1))
        self.assertIn("detail on stdout", str(ctx.exception))

    def test_silent_failure_has_a_default_message(self):
        with self.assertRaises(ProviderError) as ctx:
            self.complete(_completed("", returncode=2))
        self.assertIn("non-zero exit", str(ctx.exception))


class PromptOnStdinTests(unittest.TestCase):
    """The prompt travels on stdin, never in argv.

    Windows caps a process command line at 32767 characters, and a brief in
    ``enhance`` mode carries the repo map and every relevant file. Passing that
    as an argv element raises WinError 206, which Python surfaces as
    ``FileNotFoundError`` — indistinguishable, to the caller, from a missing
    ``claude`` binary. stdin has no such limit on any platform.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    @staticmethod
    def _run_mock():
        return mock.patch(
            "kickass_loop_engineer.providers.claude_code.subprocess.run",
            return_value=_completed())

    def test_complete_sends_the_prompt_on_stdin(self):
        with self._run_mock() as run_mock:
            ClaudeCodeProvider().complete("sys", "user")
        self.assertEqual(run_mock.call_args.kwargs["input"], "sys\n\nuser")

    def test_complete_keeps_the_prompt_out_of_argv(self):
        with self._run_mock() as run_mock:
            ClaudeCodeProvider().complete("sys", "user")
        command = run_mock.call_args.args[0]
        self.assertNotIn("sys\n\nuser", command)
        self.assertEqual(command[:4], ["claude", "-p", "--output-format", "json"])

    def test_edit_sends_the_prompt_on_stdin(self):
        with self._run_mock() as run_mock:
            ClaudeCodeProvider().edit("sys", "user", cwd=self.tmp.name)
        self.assertEqual(run_mock.call_args.kwargs["input"], "sys\n\nuser")

    def test_edit_keeps_the_prompt_out_of_argv(self):
        with self._run_mock() as run_mock:
            ClaudeCodeProvider().edit("sys", "user", cwd=self.tmp.name)
        command = run_mock.call_args.args[0]
        self.assertNotIn("sys\n\nuser", command)
        self.assertEqual(command[:4], ["claude", "-p", "--output-format", "json"])

    def test_a_prompt_far_over_the_windows_argv_limit_stays_out_of_argv(self):
        """The regression this class exists for: 120k of brief, none of it argv."""
        huge = "x" * 120_000
        with self._run_mock() as run_mock:
            ClaudeCodeProvider().edit("sys", huge, cwd=self.tmp.name)
        command = run_mock.call_args.args[0]
        self.assertLess(sum(len(part) for part in command), 1000)
        self.assertIn(huge, run_mock.call_args.kwargs["input"])


if __name__ == "__main__":
    unittest.main()


class ClaudeCodeReviewerIsolationTests(unittest.TestCase):
    """``isolated`` + ``profile_dir`` keep a reviewer's context apart from builders'."""

    def _run(self, **kwargs):
        seen = {}

        def fake_run(command, **kw):
            seen["command"] = command
            seen["cwd"] = kw.get("cwd")
            seen["env"] = kw.get("env")
            seen["cwd_existed"] = bool(kw.get("cwd")) and os.path.isdir(kw["cwd"])
            return _completed()

        with mock.patch("kickass_loop_engineer.providers.claude_code.subprocess.run",
                        side_effect=fake_run):
            ClaudeCodeProvider(**kwargs).complete("system", "user")
        return seen

    def test_isolated_review_adds_isolation_flags_and_a_fresh_scratch_dir(self):
        seen = self._run(isolated=True)
        for flag in ("--no-session-persistence", "--strict-mcp-config",
                     "--disable-slash-commands"):
            self.assertIn(flag, seen["command"])
        self.assertEqual(seen["command"][seen["command"].index("--setting-sources") + 1],
                         "project")
        self.assertTrue(seen["cwd_existed"])
        self.assertFalse(os.path.exists(seen["cwd"]), "scratch dir is removed after the call")

    def test_profile_dir_reaches_the_child_as_claude_config_dir(self):
        seen = self._run(profile_dir="~/.claude-reviewer")
        self.assertEqual(seen["env"]["CLAUDE_CONFIG_DIR"],
                         os.path.expanduser("~/.claude-reviewer"))

    def test_default_provider_is_unchanged(self):
        seen = self._run()
        self.assertNotIn("--no-session-persistence", seen["command"])
        self.assertNotIn("CLAUDE_CONFIG_DIR", {k for k in seen["env"]
                                               if seen["env"][k] != os.environ.get(k)})
