"""Tests for the Codex CLI provider with a stubbed process runner (the real ``codex`` never runs)."""
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from kickass_loop_engineer.config import build_orchestrator
from kickass_loop_engineer.objective import Objective
from kickass_loop_engineer.providers import PROVIDERS, build_provider
from kickass_loop_engineer.providers.base import ProviderError
from kickass_loop_engineer.providers.codex_cli import CodexCliProvider
from kickass_loop_engineer.providers.gemini_cli import GeminiCliProvider
from kickass_loop_engineer.review import CrossModelReviewError, model_family

RUNNER = "kickass_loop_engineer.providers.codex_cli.run_cli"


class FakeCodex:
    """Writes *answer* to the ``-o`` file and returns *returncode*; records each call."""

    def __init__(self, answer="done", returncode=0, stderr="", stdout=""):
        self.answer, self.returncode, self.stderr, self.stdout = answer, returncode, stderr, stdout
        self.calls = []

    def __call__(self, argv, *, stdin, env, cwd, timeout):
        self.calls.append({"argv": argv, "stdin": stdin, "env": env, "cwd": cwd, "timeout": timeout})
        if self.answer is not None:
            with open(argv[argv.index("-o") + 1], "w", encoding="utf-8") as handle:
                handle.write(self.answer)
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


class CodexCompleteTests(unittest.TestCase):
    def run_complete(self, fake, **kwargs):
        with mock.patch(RUNNER, side_effect=fake):
            return CodexCliProvider(model="gpt-6-astra", retry_attempts=1, **kwargs).complete("sys", "user")

    def test_read_only_argv_and_prompt_on_stdin(self):
        fake = FakeCodex("an answer")
        result = self.run_complete(fake)
        argv = fake.calls[0]["argv"]
        self.assertEqual(argv[:2], ["codex", "exec"])
        for flag in ("--ignore-user-config", "--ephemeral", "--skip-git-repo-check"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6-astra")
        self.assertEqual(argv[-1], "-")
        self.assertEqual(fake.calls[0]["stdin"], "sys\n\nuser")
        self.assertEqual((result.text, result.model), ("an answer", "gpt-6-astra"))

    def test_windows_sandbox_override_is_passed(self):
        fake = FakeCodex()
        self.run_complete(fake)
        self.assertIn('windows.sandbox="unelevated"', fake.calls[0]["argv"])

    def test_falls_back_to_stdout_when_no_last_message_file(self):
        fake = FakeCodex(answer=None, stdout="from stdout")
        self.assertEqual(self.run_complete(fake).text, "from stdout")

    def test_empty_answer_is_an_error(self):
        with self.assertRaises(ProviderError):
            self.run_complete(FakeCodex(answer="  "))

    def test_nonzero_exit_carries_stderr(self):
        with self.assertRaises(ProviderError) as ctx:
            self.run_complete(FakeCodex(returncode=1, stderr="auth required: run codex login"))
        self.assertIn("codex login", str(ctx.exception))

    def test_rate_limit_is_retried(self):
        answers = [FakeCodex(returncode=1, stderr="429 Too Many Requests"), FakeCodex("ok")]

        def flaky(argv, **kw):
            return answers[0](argv, **kw) if not answers[0].calls else answers[1](argv, **kw)
        with mock.patch(RUNNER, side_effect=flaky), mock.patch("time.sleep"):
            result = CodexCliProvider(retry_attempts=2, retry_base_delay=0).complete("s", "u")
        self.assertEqual(result.text, "ok")

    def test_timeout_is_a_provider_error(self):
        def slow(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        with mock.patch(RUNNER, side_effect=slow):
            with self.assertRaises(ProviderError) as ctx:
                CodexCliProvider(retry_attempts=1).complete("s", "u")
        self.assertIn("timed out", str(ctx.exception))

    def test_missing_binary_is_a_provider_error(self):
        with mock.patch(RUNNER, side_effect=FileNotFoundError("codex")):
            with self.assertRaises(ProviderError) as ctx:
                CodexCliProvider(retry_attempts=1).complete("s", "u")
        self.assertIn("not found", str(ctx.exception))

    def test_api_and_decision_keys_are_scrubbed(self):
        fake = FakeCodex()
        keys = {"OPENAI_API_KEY": "x", "TYPESAFE_API_KEY": "y", "CLOUDFLARE_API_TOKEN": "z", "KEEP": "1"}
        with mock.patch.dict(os.environ, keys):
            self.run_complete(fake)
        env = fake.calls[0]["env"]
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("TYPESAFE_API_KEY", env)
        self.assertNotIn("CLOUDFLARE_API_TOKEN", env)
        self.assertEqual(env["KEEP"], "1")


class CodexEditTests(unittest.TestCase):
    def test_edit_writes_inside_the_given_workspace(self):
        fake = FakeCodex("edited two files")
        with tempfile.TemporaryDirectory() as work, mock.patch(RUNNER, side_effect=fake):
            result = CodexCliProvider(retry_attempts=1).edit("sys", "user", cwd=work)
            argv = fake.calls[0]["argv"]
            self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")
            self.assertEqual(argv[argv.index("-C") + 1], work)
            self.assertEqual(fake.calls[0]["cwd"], work)
        self.assertEqual(result.text, "edited two files")

    def test_edit_requires_an_existing_directory(self):
        with self.assertRaises(ProviderError):
            CodexCliProvider().edit("s", "u", cwd="/no/such/dir/xyz")


class RegistryAndFamilyTests(unittest.TestCase):
    def test_registry_has_codex_cli_and_agy_cli(self):
        self.assertIs(PROVIDERS["codex_cli"], CodexCliProvider)
        self.assertIs(PROVIDERS["agy_cli"], GeminiCliProvider)
        self.assertTrue(build_provider("codex_cli", model="gpt-6-astra").edits_in_place)

    def test_gpt_family_detection(self):
        for name in ("gpt-6-astra", "gpt-6.1-sol", "codex_cli", "o3-mini"):
            self.assertEqual(model_family(name), "gpt", name)

    def _orchestrate(self, builder, reviewer):
        # The orchestrator keeps .loop-engineer/memory.db open; Windows cannot delete it mid-test.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work:
            config = {"builder": builder, "reviewer": reviewer}
            return build_orchestrator(config, workspace=work, month="2026-10",
                                      objective=Objective(goal="g", done_when="python -m pytest -q"))

    def test_guard_rejects_a_codex_builder_reviewed_by_codex(self):
        with self.assertRaises(CrossModelReviewError):
            self._orchestrate({"provider": "codex_cli", "model": "gpt-6-astra"},
                              {"provider": "codex_cli", "model": "gpt-6.1-sol"})

    def test_guard_accepts_a_codex_builder_reviewed_by_claude(self):
        self.assertIsNotNone(self._orchestrate({"provider": "codex_cli", "model": "gpt-6-astra"},
                                               {"provider": "claude_code", "model": "claude-opus-5-5"}))


if __name__ == "__main__":
    unittest.main()
