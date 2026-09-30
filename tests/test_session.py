"""Tests for a build round: FILE-block application and in-place harvesting.

The FILE-block cases run against a plain temp directory; the in-place cases
need a real git repository, because an agentic round learns what changed by
diffing the workspace with git.
"""
import json
import os
import subprocess
import tempfile
import unittest

from kickass_loop_engineer.agents import (
    AGENTIC_BUILDER_SYSTEM,
    UI_BUILDER_RULES,
    Builder,
)
from kickass_loop_engineer.objective import Objective
from kickass_loop_engineer.providers.base import (
    Provider,
    ProviderError,
    ProviderResult,
)
from kickass_loop_engineer.session import NO_EDIT_REMINDER, BuildRound, BuildSession
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


class _InPlaceProvider(Provider):
    """Agentic fake: each turn writes its queued files into ``cwd``.

    Stands in for a CLI that drives its own tools — it edits the directory and
    answers in prose, exactly like ``claude_code``/``gemini`` do.
    """

    name = "fake-agentic"
    edits_in_place = True

    def __init__(self, turns, results=None, model="fake-agentic"):
        """Queue one ``{relative path: contents}`` mapping per expected turn."""
        self.turns = list(turns)
        self.results = list(results) if results else None
        self.calls = []
        self.model = model

    def complete(self, system, user):
        raise AssertionError("an in-place builder must never be asked to complete()")

    def edit(self, system, user, *, cwd):
        self.calls.append((system, user, cwd))
        for rel, body in (self.turns.pop(0) if self.turns else {}).items():
            path = os.path.join(cwd, rel)
            os.makedirs(os.path.dirname(path) or cwd, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(body)
        if self.results:
            return self.results.pop(0)
        return ProviderResult(text="I edited some files.", tokens=10,
                              model=self.model)


class _RaisingInPlaceProvider(_InPlaceProvider):
    """Serves queued turns, then fails — to test corrective-call failures."""

    def edit(self, system, user, *, cwd):
        if not self.turns:
            self.calls.append((system, user, cwd))
            raise ProviderError("claude timed out after 1800s")
        return super().edit(system, user, cwd=cwd)


class _InPlaceCase:
    """Shared fixture: a git-repo workspace wired to an in-place builder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.root)
        subprocess.run(["git", "-C", self.root, "init", "-q"], check=True,
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", self.root, "-c", "user.name=t",
                        "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
                        "-m", "init"], check=True, capture_output=True, text=True)

    def session(self, provider, *, system_prompt=None, **kwargs):
        """Build a BuildSession around an in-place provider and the git repo."""
        builder = (Builder(provider, system_prompt=system_prompt)
                   if system_prompt is not None else Builder(provider))
        return BuildSession(
            builder=builder,
            workspace=Workspace(root=self.root),
            objective=Objective(goal="say hi", done_when="hello.txt exists"),
            **kwargs,
        )

    def read(self, rel):
        with open(os.path.join(self.root, rel), encoding="utf-8") as handle:
            return handle.read()


class InPlaceBuildRoundTests(_InPlaceCase, unittest.TestCase):
    """An agentic provider edits the workspace; the round harvests the result."""

    def test_edited_file_is_reported_as_written(self):
        provider = _InPlaceProvider([{"hello.txt": "hi\n"}])
        round_ = self.session(provider).build_round(1)
        self.assertEqual(round_.files_written, ["hello.txt"])
        self.assertEqual(self.read("hello.txt"), "hi\n")
        self.assertEqual(len(provider.calls), 1)

    def test_every_edited_file_is_reported_sorted(self):
        provider = _InPlaceProvider([{"b.py": "B = 1\n", "pkg/a.py": "A = 1\n"}])
        round_ = self.session(provider).build_round(1)
        self.assertEqual(round_.files_written, ["b.py", "pkg/a.py"])

    def test_prose_summary_is_kept_as_the_builder_text(self):
        provider = _InPlaceProvider([{"hello.txt": "hi\n"}])
        round_ = self.session(provider).build_round(1)
        self.assertEqual(round_.builder_text, "I edited some files.")
        self.assertEqual(round_.model, "fake-agentic")

    def test_usage_is_carried_from_the_provider_result(self):
        provider = _InPlaceProvider(
            [{"hello.txt": "hi\n"}],
            results=[ProviderResult(text="done", tokens=25, cost_usd=0.2,
                                    model="fake-agentic", prompt_tokens=15,
                                    completion_tokens=10)])
        round_ = self.session(provider).build_round(1)
        self.assertEqual(round_.tokens, 25)
        self.assertAlmostEqual(round_.cost_usd, 0.2)
        self.assertEqual(round_.prompt_tokens, 15)
        self.assertEqual(round_.completion_tokens, 10)

    def test_the_builder_is_handed_the_workspace_root_as_its_cwd(self):
        provider = _InPlaceProvider([{"hello.txt": "hi\n"}])
        self.session(provider).build_round(1)
        self.assertEqual(provider.calls[0][2], os.path.abspath(self.root))

    def test_the_agentic_system_prompt_is_used(self):
        provider = _InPlaceProvider([{"hello.txt": "hi\n"}])
        self.session(provider).build_round(1)
        self.assertEqual(provider.calls[0][0], AGENTIC_BUILDER_SYSTEM)

    def test_ui_rules_ride_along_when_the_builder_carries_them(self):
        provider = _InPlaceProvider([{"hello.txt": "hi\n"}])
        self.session(provider,
                     system_prompt=f"whatever\n\n{UI_BUILDER_RULES}").build_round(1)
        system = provider.calls[0][0]
        self.assertTrue(system.startswith(AGENTIC_BUILDER_SYSTEM))
        self.assertIn(UI_BUILDER_RULES, system)

    def test_the_brief_carries_the_goal_and_feedback(self):
        provider = _InPlaceProvider([{"hello.txt": "hi\n"}])
        self.session(provider).build_round(1, "fix the greeting")
        brief = provider.calls[0][1]
        self.assertIn("say hi", brief)
        self.assertIn("fix the greeting", brief)

    def test_files_seeded_before_the_round_are_not_claimed_as_its_work(self):
        with open(os.path.join(self.root, "seeded.py"), "w", encoding="utf-8") as handle:
            handle.write("SEEDED = 1\n")
        provider = _InPlaceProvider([{"fresh.py": "FRESH = 1\n"}])
        round_ = self.session(provider).build_round(1)
        self.assertEqual(round_.files_written, ["fresh.py"])

    def test_guardrail_violations_are_reverted_and_reported(self):
        provider = _InPlaceProvider([{".env": "TOKEN=1\n", "ok.py": "OK = 1\n"}])
        round_ = self.session(provider).build_round(1)
        self.assertEqual(round_.files_written, ["ok.py"])
        self.assertEqual([path for path, _reason in round_.rejected], [".env"])
        self.assertFalse(os.path.exists(os.path.join(self.root, ".env")))


class InPlaceNoEditRetryTests(_InPlaceCase, unittest.TestCase):
    """A turn that changed nothing earns a bounded corrective retry."""

    def test_one_no_edit_retry_reports_the_second_turn_s_file(self):
        provider = _InPlaceProvider([{}, {"hello.txt": "hi\n"}])
        calls = []
        round_ = self.session(provider,
                              on_retry=lambda: calls.append(1)).build_round(1)
        self.assertEqual(round_.files_written, ["hello.txt"])
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(len(calls), 1)

    def test_the_corrective_brief_says_the_turn_changed_nothing(self):
        provider = _InPlaceProvider([{}, {"hello.txt": "hi\n"}])
        self.session(provider).build_round(1, "prior feedback")
        brief = provider.calls[1][1]
        self.assertIn(NO_EDIT_REMINDER, brief)
        self.assertIn("prior feedback", brief)

    def test_usage_is_summed_across_the_retry(self):
        provider = _InPlaceProvider(
            [{}, {"hello.txt": "hi\n"}],
            results=[
                ProviderResult(text="I described the plan.", tokens=10,
                               cost_usd=0.1, model="fake-agentic",
                               prompt_tokens=6, completion_tokens=4),
                ProviderResult(text="I edited hello.txt.", tokens=20,
                               cost_usd=0.3, model="fake-agentic",
                               prompt_tokens=12, completion_tokens=8),
            ])
        round_ = self.session(provider).build_round(1)
        self.assertEqual(round_.tokens, 30)
        self.assertAlmostEqual(round_.cost_usd, 0.4)
        self.assertEqual(round_.prompt_tokens, 18)
        self.assertEqual(round_.completion_tokens, 12)
        self.assertEqual(round_.builder_text, "I edited hello.txt.")

    def test_two_empty_handed_turns_stop_after_the_retry_budget(self):
        provider = _InPlaceProvider([{}, {}])
        round_ = self.session(provider).build_round(1)
        self.assertEqual(round_.files_written, [])
        self.assertEqual(len(provider.calls), 2)

    def test_format_retries_zero_makes_a_single_turn(self):
        provider = _InPlaceProvider([{}])
        round_ = self.session(provider, format_retries=0).build_round(1)
        self.assertEqual(round_.files_written, [])
        self.assertEqual(len(provider.calls), 1)

    def test_format_retries_two_allows_at_most_three_turns(self):
        provider = _InPlaceProvider([{}, {}, {}])
        self.session(provider, format_retries=2).build_round(1)
        self.assertEqual(len(provider.calls), 3)

    def test_an_empty_response_is_never_retried(self):
        provider = _InPlaceProvider(
            [{}], results=[ProviderResult(text="", model="fake-agentic")])
        round_ = self.session(provider).build_round(1)
        self.assertEqual(round_.files_written, [])
        self.assertEqual(len(provider.calls), 1)

    def test_a_productive_first_turn_is_never_retried(self):
        provider = _InPlaceProvider([{"hello.txt": "hi\n"}])
        calls = []
        self.session(provider, on_retry=lambda: calls.append(1)).build_round(1)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(calls, [])

    def test_a_failing_corrective_turn_carries_the_first_turn_s_spend(self):
        provider = _RaisingInPlaceProvider(
            [{}],
            results=[ProviderResult(text="I only described things.", tokens=40,
                                    cost_usd=0.25, model="paid-model",
                                    prompt_tokens=30, completion_tokens=10)])
        with self.assertRaises(ProviderError) as ctx:
            self.session(provider).build_round(1)
        partial = ctx.exception.partial_result
        self.assertEqual(partial.tokens, 40)
        self.assertAlmostEqual(partial.cost_usd, 0.25)
        self.assertEqual(partial.prompt_tokens, 30)
        self.assertEqual(partial.completion_tokens, 10)
        self.assertEqual(partial.model, "paid-model")

    def test_a_failing_first_turn_has_no_partial_result(self):
        provider = _RaisingInPlaceProvider([])
        with self.assertRaises(ProviderError) as ctx:
            self.session(provider).build_round(1)
        self.assertIsNone(getattr(ctx.exception, "partial_result", None))


def _git(root, *args):
    """Run one git command inside *root*, failing loudly."""
    return subprocess.run(["git", "-C", root, *args], check=True,
                          capture_output=True, text=True)


class _CommittingInPlaceProvider(_InPlaceProvider):
    """Agentic fake that COMMITS its edits inside the worktree.

    The observed field failure: an in-place builder finishes its turn with
    ``git commit``, which hides the change from a status-diffing harvest and
    from proof-of-test's ``git stash``. A turn that wrote nothing commits
    nothing (git would fail on an empty commit).
    """

    def edit(self, system, user, *, cwd):
        result = super().edit(system, user, cwd=cwd)
        if _git(cwd, "status", "--porcelain").stdout.strip():
            _git(cwd, "add", "-A")
            _git(cwd, "-c", "user.name=t", "-c", "user.email=t@t",
                 "commit", "-q", "-m", "builder commit")
        return result


class InPlaceBuilderCommitTests(_InPlaceCase, unittest.TestCase):
    """A builder that commits its work still gets harvested and verified."""

    def head(self):
        """Return the workspace's current HEAD sha."""
        return _git(self.root, "rev-parse", "HEAD").stdout.strip()

    def status(self):
        """Return ``git status --porcelain`` — what proof-of-test stashes."""
        return _git(self.root, "status", "--porcelain").stdout.strip()

    def events(self):
        """Return the progress events the round recorded."""
        path = os.path.join(self.root, ".loop-engineer", "events.jsonl")
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_session_harvests_files_the_builder_committed(self):
        base = self.head()
        provider = _CommittingInPlaceProvider([{"hello.txt": "hi\n"}])

        round_ = self.session(provider).build_round(1)

        self.assertIn("hello.txt", round_.files_written)
        self.assertEqual(self.head(), base)
        self.assertEqual(self.read("hello.txt"), "hi\n")

    def test_a_committed_turn_is_not_mistaken_for_a_no_edit_turn(self):
        provider = _CommittingInPlaceProvider([{"hello.txt": "hi\n"}])
        retries = []

        self.session(provider, on_retry=lambda: retries.append(1)).build_round(1)

        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(retries, [])

    def test_the_committed_change_is_left_for_proof_of_test_to_stash(self):
        """Proof-of-test needs an uncommitted change; the round leaves one."""
        provider = _CommittingInPlaceProvider([{"hello.txt": "hi\n"}])

        self.session(provider).build_round(1)

        self.assertTrue(self.status())
        self.assertIn("hello.txt", self.status())

    def test_a_commit_on_a_corrective_turn_is_uncommitted_too(self):
        base = self.head()
        provider = _CommittingInPlaceProvider([{}, {"hello.txt": "hi\n"}])

        round_ = self.session(provider).build_round(1)

        self.assertEqual(round_.files_written, ["hello.txt"])
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(self.head(), base)

    def test_the_un_commit_is_announced_as_a_progress_event(self):
        provider = _CommittingInPlaceProvider([{"hello.txt": "hi\n"}])

        self.session(provider).build_round(1)

        messages = [event["message"] for event in self.events()
                    if event["phase"] == "uncommitted"]
        self.assertEqual(messages, ["uncommitted 1 builder commit(s)"])

    def test_a_builder_that_leaves_head_alone_emits_no_event(self):
        provider = _InPlaceProvider([{"hello.txt": "hi\n"}])

        self.session(provider).build_round(1)

        self.assertEqual([event for event in self.events()
                          if event["phase"] == "uncommitted"], [])


if __name__ == "__main__":
    unittest.main()
