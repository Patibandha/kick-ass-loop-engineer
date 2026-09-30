"""Tests for the Gemini (Antigravity CLI) provider with a stubbed subprocess.

The real ``agy`` binary is never invoked: every case drives the provider through
a mocked ``subprocess.run`` so the suite stays hermetic and offline. Cases that
exercise a TRANSIENT failure pin ``retry_attempts=1`` so the shared backoff
never makes the suite wait; the retry policy itself is covered in
``tests/test_providers_retry.py``.
"""
import json
import subprocess
import tempfile
import unittest
from unittest import mock

from kickass_loop_engineer.providers import PROVIDERS, build_provider
from kickass_loop_engineer.providers import base
from kickass_loop_engineer.providers.base import ProviderError
from kickass_loop_engineer.providers.gemini_cli import GeminiCliProvider
from kickass_loop_engineer.review import model_family

_HAPPY_STDOUT = json.dumps({
    "conversation_id": "conv-1",
    "status": "SUCCESS",
    "response": "hello from agy stub",
    "duration_seconds": 1.5,
    "num_turns": 2,
    "usage": {
        "input_tokens": 3,
        "output_tokens": 4,
        "thinking_tokens": 5,
        "cache_read_tokens": 1,
        "total_tokens": 13,
    },
})


def _completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    """Build a ``CompletedProcess`` standing in for one ``agy`` run."""
    return subprocess.CompletedProcess(args=["agy"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class GeminiCliEnvelopeTests(unittest.TestCase):
    """Parsing of the ``agy --output-format json`` envelope."""

    def complete(self, stdout: str, **provider_kwargs):
        """Run one completion with ``subprocess.run`` stubbed to return stdout."""
        with mock.patch("kickass_loop_engineer.providers.gemini_cli.subprocess.run",
                        return_value=_completed(stdout)):
            return GeminiCliProvider(model="gemini-3-pro", **provider_kwargs).complete(
                "sys", "user")

    def test_success_envelope_yields_response_text(self):
        self.assertEqual(self.complete(_HAPPY_STDOUT).text, "hello from agy stub")

    def test_thinking_tokens_count_on_the_completion_side(self):
        result = self.complete(_HAPPY_STDOUT)
        self.assertEqual(result.prompt_tokens, 3)
        self.assertEqual(result.completion_tokens, 9)

    def test_reported_total_tokens_is_authoritative(self):
        self.assertEqual(self.complete(_HAPPY_STDOUT).tokens, 13)

    def test_missing_total_falls_back_to_the_split_sum(self):
        result = self.complete(json.dumps({
            "status": "SUCCESS",
            "response": "x",
            "usage": {"input_tokens": 2, "output_tokens": 3},
        }))
        self.assertEqual(result.tokens, 5)

    def test_null_usage_counts_yield_zero_tokens_without_raising(self):
        result = self.complete(json.dumps({
            "status": "SUCCESS",
            "response": "x",
            "usage": {"input_tokens": None, "output_tokens": None,
                      "thinking_tokens": None, "total_tokens": None},
        }))
        self.assertEqual((result.prompt_tokens, result.completion_tokens, result.tokens),
                         (0, 0, 0))

    def test_missing_usage_block_yields_zero_tokens(self):
        result = self.complete(json.dumps({"status": "SUCCESS", "response": "x"}))
        self.assertEqual((result.prompt_tokens, result.completion_tokens, result.tokens),
                         (0, 0, 0))

    def test_subscription_usage_reports_no_dollar_cost(self):
        self.assertEqual(self.complete(_HAPPY_STDOUT).cost_usd, 0.0)

    def test_model_label_is_carried_into_the_result(self):
        self.assertEqual(self.complete(_HAPPY_STDOUT).model, "gemini-3-pro")

    def test_unlabeled_provider_reports_its_registry_name(self):
        with mock.patch("kickass_loop_engineer.providers.gemini_cli.subprocess.run",
                        return_value=_completed(_HAPPY_STDOUT)):
            result = GeminiCliProvider().complete("sys", "user")
        self.assertEqual(result.model, "gemini")

    def test_non_json_stdout_falls_back_to_raw_text(self):
        self.assertEqual(self.complete("not json at all").text, "not json at all")

    def test_empty_stdout_raises(self):
        with self.assertRaises(ProviderError) as ctx:
            self.complete("   ")
        self.assertIn("empty response", str(ctx.exception))


class GeminiCliCommandTests(unittest.TestCase):
    """The headless invocation contract and subprocess wiring."""

    def run_call(self, **provider_kwargs):
        """Run one completion and return the mock that captured ``subprocess.run``."""
        with mock.patch("kickass_loop_engineer.providers.gemini_cli.subprocess.run",
                        return_value=_completed(_HAPPY_STDOUT)) as run_mock:
            GeminiCliProvider(**provider_kwargs).complete("sys", "user")
        return run_mock

    def test_command_is_headless_stream_json_with_no_prompt_in_argv(self):
        command = self.run_call().call_args.args[0]
        self.assertEqual(command, ["agy", "-p", "", "--input-format", "stream-json",
                                   "--output-format", "stream-json"])

    def test_model_is_not_passed_as_a_cli_flag(self):
        # agy's headless contract exposes no model-selection flag; the label is
        # for reporting/pricing only and must never leak into the command.
        command = self.run_call(model="gemini-3-pro").call_args.args[0]
        self.assertNotIn("--model", command)
        self.assertNotIn("gemini-3-pro", command)

    def test_custom_binary_is_honored(self):
        command = self.run_call(binary="/opt/bin/agy").call_args.args[0]
        self.assertEqual(command[0], "/opt/bin/agy")

    def test_configured_cwd_is_passed_to_subprocess_run(self):
        self.assertEqual(self.run_call(cwd="/tmp/x").call_args.kwargs["cwd"], "/tmp/x")

    def test_default_cwd_is_none(self):
        self.assertIsNone(self.run_call().call_args.kwargs["cwd"])

    def test_force_subscription_strips_api_keys_from_the_environment(self):
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "k", "GOOGLE_API_KEY": "k"},
                             clear=False):
            env = self.run_call().call_args.kwargs["env"]
        self.assertNotIn("GEMINI_API_KEY", env)
        self.assertNotIn("GOOGLE_API_KEY", env)

    def test_api_keys_survive_when_force_subscription_is_off(self):
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "k"}, clear=False):
            env = self.run_call(force_subscription=False).call_args.kwargs["env"]
        self.assertEqual(env["GEMINI_API_KEY"], "k")


class GeminiCliFailureTests(unittest.TestCase):
    """Error semantics mirror the Claude Code provider's."""

    def complete(self, completed, **provider_kwargs):
        """Run one completion with ``subprocess.run`` stubbed to return *completed*."""
        with mock.patch("kickass_loop_engineer.providers.gemini_cli.subprocess.run",
                        return_value=completed):
            return GeminiCliProvider(**provider_kwargs).complete("sys", "user")

    def complete_raising(self, exc, **provider_kwargs):
        """Run one completion with ``subprocess.run`` stubbed to raise *exc*."""
        with mock.patch("kickass_loop_engineer.providers.gemini_cli.subprocess.run",
                        side_effect=exc):
            return GeminiCliProvider(**provider_kwargs).complete("sys", "user")

    def test_error_status_raises_with_the_reported_response(self):
        stdout = json.dumps({"status": "ERROR", "response": "quota exhausted"})
        with self.assertRaises(ProviderError) as ctx:
            self.complete(_completed(stdout))
        self.assertIn("quota exhausted", str(ctx.exception))

    def test_error_status_without_detail_names_the_status(self):
        with self.assertRaises(ProviderError) as ctx:
            self.complete(_completed(json.dumps({"status": "CANCELLED"})))
        self.assertIn("CANCELLED", str(ctx.exception))

    def test_non_zero_exit_surfaces_stderr(self):
        with self.assertRaises(ProviderError) as ctx:
            self.complete(_completed("", returncode=1, stderr="boom on stderr"))
        self.assertIn("boom on stderr", str(ctx.exception))

    def test_non_zero_exit_without_output_has_a_default_message(self):
        with self.assertRaises(ProviderError) as ctx:
            self.complete(_completed("", returncode=2))
        self.assertIn("non-zero exit", str(ctx.exception))

    def test_auth_failure_on_exit_tells_the_operator_to_run_agy_once(self):
        stderr = "Error: authentication required (cannot prompt: not a TTY)"
        with self.assertRaises(ProviderError) as ctx:
            self.complete(_completed("", returncode=1, stderr=stderr))
        message = str(ctx.exception)
        self.assertIn("run `agy` interactively once", message)
        self.assertIn("authentication required", message)

    def test_auth_failure_in_the_envelope_gets_the_same_hint(self):
        stdout = json.dumps({"status": "AUTH_ERROR", "response": "Not logged in."})
        with self.assertRaises(ProviderError) as ctx:
            self.complete(_completed(stdout))
        self.assertIn("run `agy` interactively once", str(ctx.exception))

    def test_timeout_names_the_configured_budget(self):
        # A timeout is transient, so retrying is disabled here to keep the
        # assertion about the MESSAGE, not about the backoff schedule.
        exc = subprocess.TimeoutExpired(cmd="agy", timeout=30.0)
        with self.assertRaises(ProviderError) as ctx:
            self.complete_raising(exc, timeout_seconds=30.0, retry_attempts=1)
        self.assertIn("timed out after 30s", str(ctx.exception))

    def test_missing_binary_is_attributed_to_the_binary(self):
        exc = FileNotFoundError(2, "No such file or directory")
        exc.filename = "agy"
        with self.assertRaises(ProviderError) as ctx:
            self.complete_raising(exc, cwd="/tmp")
        self.assertIn("agy binary not found", str(ctx.exception))

    def test_missing_cwd_is_attributed_to_the_cwd(self):
        exc = FileNotFoundError(2, "No such file or directory")
        exc.filename = "/gone/workspace"
        with self.assertRaises(ProviderError) as ctx:
            self.complete_raising(exc, cwd="/gone/workspace")
        message = str(ctx.exception)
        self.assertIn("cwd does not exist", message)
        self.assertIn("/gone/workspace", message)
        self.assertNotIn("binary not found", message)

    def test_os_error_is_wrapped(self):
        with self.assertRaises(ProviderError) as ctx:
            self.complete_raising(OSError("exec format error"))
        self.assertIn("subprocess error", str(ctx.exception))


class GeminiCliRegistryTests(unittest.TestCase):
    """Registry wiring and cross-model family classification."""

    def test_registry_builds_the_gemini_provider(self):
        self.assertIn("gemini", PROVIDERS)
        self.assertIsInstance(build_provider("gemini"), GeminiCliProvider)

    def test_default_binary_is_the_antigravity_cli(self):
        self.assertEqual(GeminiCliProvider().binary, "agy")

    def test_gemini_models_form_their_own_review_family(self):
        self.assertEqual(model_family("gemini-3-pro"), "gemini")
        self.assertEqual(model_family("gemini"), "gemini")
        self.assertNotEqual(model_family("gemini-3-pro"), model_family("claude-opus-4-8"))


class GeminiCliEditTests(unittest.TestCase):
    """The agentic in-place path: argv, working directory, and timeout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def edit_call(self, **provider_kwargs):
        """Run one edit and return the mock that captured ``subprocess.run``."""
        with mock.patch("kickass_loop_engineer.providers.gemini_cli.subprocess.run",
                        return_value=_completed(_HAPPY_STDOUT)) as run_mock:
            GeminiCliProvider(**provider_kwargs).edit("sys", "user", cwd=self.tmp.name)
        return run_mock

    def test_provider_declares_in_place_editing(self):
        self.assertTrue(GeminiCliProvider().edits_in_place)

    def test_edit_argv_matches_the_headless_contract(self):
        # Identical to complete's: agy has no tool-permission flags, and the
        # prompt is on stdin for both (see PromptOnStdinTests).
        command = self.edit_call(model="gemini-3-pro").call_args.args[0]
        self.assertEqual(command, ["agy", "-p", "", "--input-format", "stream-json",
                                   "--output-format", "stream-json"])

    def test_edit_passes_the_given_cwd_and_edit_timeout(self):
        kwargs = self.edit_call(edit_timeout_seconds=900.0).call_args.kwargs
        self.assertEqual(kwargs["cwd"], self.tmp.name)
        self.assertEqual(kwargs["timeout"], 900.0)

    def test_edit_default_timeout_is_thirty_minutes(self):
        self.assertEqual(self.edit_call().call_args.kwargs["timeout"], 1800.0)

    def test_edit_ignores_the_constructor_cwd(self):
        self.assertEqual(self.edit_call(cwd="/somewhere/else").call_args.kwargs["cwd"],
                         self.tmp.name)

    def test_edit_returns_the_parsed_prose_summary(self):
        with mock.patch("kickass_loop_engineer.providers.gemini_cli.subprocess.run",
                        return_value=_completed(_HAPPY_STDOUT)):
            result = GeminiCliProvider(model="gemini-3-pro").edit(
                "sys", "user", cwd=self.tmp.name)
        self.assertEqual(result.text, "hello from agy stub")
        self.assertEqual(result.tokens, 13)

    def test_edit_rejects_a_missing_cwd_before_spawning_anything(self):
        with mock.patch("kickass_loop_engineer.providers.gemini_cli.subprocess.run") as run_mock:
            with self.assertRaises(ProviderError) as ctx:
                GeminiCliProvider().edit("sys", "user", cwd="/gone/workspace")
        message = str(ctx.exception)
        self.assertIn("requires an existing cwd", message)
        self.assertIn("/gone/workspace", message)
        run_mock.assert_not_called()


class GeminiCliRetryTests(unittest.TestCase):
    """Transient CLI failures are retried; permanent ones fail on the first call."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.delays = []

    def run_with(self, outcome, method="complete", **provider_kwargs):
        """Drive one call with ``subprocess.run`` stubbed by *outcome*.

        The real retry policy runs; only its wait is replaced by a recorder,
        so a retry costs the suite no wall time.
        """
        def spy(call, **kwargs):
            kwargs["sleep"] = self.delays.append
            return base.retry_transient(call, **kwargs)

        stub = ({"return_value": outcome}
                if isinstance(outcome, subprocess.CompletedProcess)
                else {"side_effect": outcome})
        with mock.patch("kickass_loop_engineer.providers.gemini_cli.retry_transient", spy):
            with mock.patch("kickass_loop_engineer.providers.gemini_cli.subprocess.run",
                            **stub):
                provider = GeminiCliProvider(**provider_kwargs)
                if method == "edit":
                    return provider.edit("sys", "user", cwd=self.tmp.name)
                return provider.complete("sys", "user")

    def test_overloaded_exit_is_retried_then_succeeds(self):
        result = self.run_with([
            _completed("", returncode=1, stderr="503 service temporarily unavailable"),
            _completed(_HAPPY_STDOUT),
        ], method="edit")
        self.assertEqual(result.text, "hello from agy stub")
        self.assertEqual(self.delays, [2.0])

    def test_auth_failure_is_never_retried(self):
        with self.assertRaises(ProviderError) as ctx:
            self.run_with(_completed("", returncode=1, stderr="Not logged in."))
        self.assertIn("run `agy` interactively once", str(ctx.exception))
        self.assertEqual(self.delays, [])


def _stream(result: dict, *, extra_lines: tuple = ()) -> str:
    """Render an ``agy --output-format stream-json`` stdout ending in *result*."""
    lines = [json.dumps({"event": "init", "conversation_id": "conv-1", "init": {}})]
    lines.extend(extra_lines)
    lines.append(json.dumps({"event": "result", "result": result}))
    return "\n".join(lines) + "\n"


_HAPPY_RESULT = json.loads(_HAPPY_STDOUT)


class PromptOnStdinTests(unittest.TestCase):
    """The prompt travels on stdin as one stream-json message, never in argv.

    Windows caps a process command line at 32767 characters, and a review brief
    in ``enhance`` mode carries the repo map and the whole diff. Passing that as
    the ``-p`` value raises WinError 206, which Python surfaces as
    ``FileNotFoundError`` — so the engine reported "agy binary not found" while
    ``agy.exe`` sat on PATH working, and every advisory review was skipped. This
    is the same defect ``claude_code`` had; ``agy`` has no bare-stdin mode, so
    the prompt rides its documented ``--input-format stream-json`` channel.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    @staticmethod
    def _run_mock(stdout: str = ""):
        return mock.patch(
            "kickass_loop_engineer.providers.gemini_cli.subprocess.run",
            return_value=_completed(stdout or _stream(_HAPPY_RESULT)))

    @staticmethod
    def _sent(run_mock) -> dict:
        """Decode the one stream-json message the provider wrote to stdin."""
        payload = run_mock.call_args.kwargs["input"]
        lines = [line for line in payload.splitlines() if line.strip()]
        assert len(lines) == 1, f"expected one stdin message, got {len(lines)}"
        return json.loads(lines[0])

    def test_complete_sends_the_prompt_as_one_user_message_on_stdin(self):
        with self._run_mock() as run_mock:
            GeminiCliProvider().complete("sys", "user")
        sent = self._sent(run_mock)
        self.assertEqual(sent["event"], "user")
        self.assertEqual(sent["message"]["role"], "user")
        # complete() prefixes an answer-in-text directive (EmptyAnswerTests).
        self.assertTrue(sent["message"]["content"].endswith("sys\n\nuser"))

    def test_complete_keeps_the_prompt_out_of_argv(self):
        with self._run_mock() as run_mock:
            GeminiCliProvider().complete("sys", "user")
        command = run_mock.call_args.args[0]
        self.assertNotIn("sys\n\nuser", command)
        self.assertNotIn("user", command)

    def test_edit_sends_the_prompt_on_stdin(self):
        with self._run_mock() as run_mock:
            GeminiCliProvider().edit("sys", "user", cwd=self.tmp.name)
        self.assertEqual(self._sent(run_mock)["message"]["content"], "sys\n\nuser")

    def test_a_prompt_far_over_the_windows_argv_limit_stays_out_of_argv(self):
        """The regression this class exists for: 120k of brief, none of it argv."""
        huge = "x" * 120_000
        with self._run_mock() as run_mock:
            GeminiCliProvider().complete("sys", huge)
        command = run_mock.call_args.args[0]
        self.assertLess(sum(len(part) for part in command), 1000)
        self.assertIn(huge, self._sent(run_mock)["message"]["content"])

    def test_the_stdin_line_is_pure_ascii_whatever_the_prompt_holds(self):
        """A Windows code page cannot mangle what it never has to encode."""
        with self._run_mock() as run_mock:
            GeminiCliProvider().complete("sys", "≥3125 tests — no edge")
        payload = run_mock.call_args.kwargs["input"]
        payload.encode("ascii")  # raises if any non-ASCII byte reached stdin
        self.assertTrue(self._sent(run_mock)["message"]["content"].endswith(
            "sys\n\n≥3125 tests — no edge"))

    def test_stdout_is_decoded_as_utf8_not_the_platform_code_page(self):
        with self._run_mock() as run_mock:
            GeminiCliProvider().complete("sys", "user")
        self.assertEqual(run_mock.call_args.kwargs["encoding"], "utf-8")


class StreamParsingTests(unittest.TestCase):
    """Reading the terminal ``result`` event out of a stream-json run."""

    def complete(self, stdout: str):
        with mock.patch("kickass_loop_engineer.providers.gemini_cli.subprocess.run",
                        return_value=_completed(stdout)):
            return GeminiCliProvider(model="gemini-3-pro").complete("sys", "user")

    def test_the_result_event_is_parsed_like_the_json_envelope(self):
        result = self.complete(_stream(_HAPPY_RESULT))
        self.assertEqual(result.text, "hello from agy stub")
        self.assertEqual(result.tokens, 13)

    def test_step_events_before_the_result_are_ignored(self):
        step = json.dumps({"event": "step_update", "step_type": "text", "text": "thinking"})
        result = self.complete(_stream(_HAPPY_RESULT, extra_lines=(step, step)))
        self.assertEqual(result.text, "hello from agy stub")

    def test_a_non_json_diagnostic_line_does_not_break_the_parse(self):
        """agy prints plain `error: …` lines into the same stream."""
        result = self.complete(_stream(_HAPPY_RESULT, extra_lines=("error: benign note",)))
        self.assertEqual(result.text, "hello from agy stub")

    def test_an_error_result_carries_its_error_field_into_the_message(self):
        failed = {"status": "ERROR", "response": "", "error": "quota exhausted for today"}
        with self.assertRaises(ProviderError) as ctx:
            self.complete(_stream(failed))
        self.assertIn("quota exhausted for today", str(ctx.exception))

    def test_a_stream_with_no_result_event_raises(self):
        init_only = json.dumps({"event": "init", "conversation_id": "c", "init": {}}) + "\n"
        with self.assertRaises(ProviderError) as ctx:
            self.complete(init_only)
        self.assertIn("no result event", str(ctx.exception))


class EmptyAnswerTests(unittest.TestCase):
    """A SUCCESS with no text is not an answer — for the roles that need one.

    Observed on agy 1.1.15: handed a long review brief, the model chose to call
    a file tool instead of answering, the tool failed (agy's file tools are
    sandboxed to its own scratch directory, not the process cwd), and the
    print-mode run ENDED on that tool call — `status: SUCCESS`, `response: ""`,
    after 60k tokens. A reviewer parsing that text finds zero findings and the
    engine promotes the slice: a review that never happened, reported as a
    clean one. :meth:`complete` serves exactly those prose roles, so it must
    fail loudly instead. :meth:`edit` must not: its product is the files the
    engine harvests afterwards, and an empty summary line is legitimate there.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    @staticmethod
    def _silent_success():
        return _stream({**_HAPPY_RESULT, "response": ""})

    def test_complete_refuses_a_success_with_no_answer(self):
        with mock.patch("kickass_loop_engineer.providers.gemini_cli.subprocess.run",
                        return_value=_completed(self._silent_success())):
            with self.assertRaises(ProviderError) as ctx:
                GeminiCliProvider().complete("sys", "user")
        self.assertIn("no answer", str(ctx.exception))

    def test_complete_refuses_a_whitespace_only_answer(self):
        stdout = _stream({**_HAPPY_RESULT, "response": "  \n\t "})
        with mock.patch("kickass_loop_engineer.providers.gemini_cli.subprocess.run",
                        return_value=_completed(stdout)):
            with self.assertRaises(ProviderError):
                GeminiCliProvider().complete("sys", "user")

    def test_an_empty_answer_is_not_retried_as_if_it_were_transient(self):
        """Retrying costs another full brief; the failure is a choice, not a blip."""
        with mock.patch("kickass_loop_engineer.providers.gemini_cli.subprocess.run",
                        return_value=_completed(self._silent_success())) as run_mock:
            with self.assertRaises(ProviderError):
                GeminiCliProvider(retry_attempts=3, retry_base_delay=0.0).complete("sys", "user")
        self.assertEqual(run_mock.call_count, 1)

    def test_edit_accepts_an_empty_summary(self):
        with mock.patch("kickass_loop_engineer.providers.gemini_cli.subprocess.run",
                        return_value=_completed(self._silent_success())):
            result = GeminiCliProvider().edit("sys", "user", cwd=self.tmp.name)
        self.assertEqual(result.text, "")

    @staticmethod
    def _sent_content(run_mock) -> str:
        payload = run_mock.call_args.kwargs["input"]
        return json.loads(payload.strip())["message"]["content"]

    def test_complete_tells_the_model_to_answer_in_text_not_tools(self):
        """The same brief that ended on a tool call answered once told not to."""
        with mock.patch("kickass_loop_engineer.providers.gemini_cli.subprocess.run",
                        return_value=_completed(_stream(_HAPPY_RESULT))) as run_mock:
            GeminiCliProvider().complete("sys", "user")
        content = self._sent_content(run_mock)
        self.assertTrue(content.startswith("Answer directly in text."))
        self.assertTrue(content.endswith("sys\n\nuser"))

    def test_edit_does_not_forbid_tools(self):
        """edit() exists so the model CAN act in the worktree."""
        with mock.patch("kickass_loop_engineer.providers.gemini_cli.subprocess.run",
                        return_value=_completed(_stream(_HAPPY_RESULT))) as run_mock:
            GeminiCliProvider().edit("sys", "user", cwd=self.tmp.name)
        self.assertEqual(self._sent_content(run_mock), "sys\n\nuser")


if __name__ == "__main__":
    unittest.main()
