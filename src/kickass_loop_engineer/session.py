"""Single-round build session driven by the Claude Code orchestrator.

In the skill workflow, the Claude Code session owns the review/iterate loop and
calls one ``BuildSession`` round at a time. Each round runs the builder (a local
Ollama/Kimi model or any provider), writes guarded files into the workspace,
records progress and shared state, and returns a structured result for the
orchestrator to review and verify.

The round takes one of two shapes, decided by the builder's provider:

* **FILE blocks** — a blind provider answers with fenced blocks the session
  applies through :meth:`Workspace.apply`; a reply with no blocks earns a
  bounded corrective retry.
* **In place** — an agentic provider (``provider.edits_in_place``) is handed the
  workspace directory and edits it itself; the session fingerprints the
  directory first, un-commits anything the builder committed, and harvests the
  difference afterwards, and a turn that changed nothing earns the same bounded
  corrective retry.

Everything downstream — guardrails, state, progress, gates, review, promotion —
is identical for both shapes: the round always ends with the list of files the
policy allowed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from .agents import Builder
from .objective import Objective
from .progress import ProgressReporter
from .providers.base import ProviderError, ProviderResult
from .state import RunState
from .workspace import Workspace

#: Corrective brief for an agentic builder whose turn touched no files.
NO_EDIT_REMINDER = (
    "YOUR PREVIOUS TURN CHANGED NO FILES in the working directory. You must "
    "implement the goal by editing files with your tools, then summarise."
)


@dataclass
class _RoundUsage:
    """Running total of a round's provider usage across every call it makes.

    A round may issue several calls (a corrective retry after a malformed or
    empty-handed first turn). Each one costs real tokens, so the totals are
    accumulated here and reported as one, and the same object supplies the
    ``partial_result`` attached when a later call fails.

    Attributes:
        tokens: Total tokens across every call so far.
        cost_usd: Total dollar cost across every call so far.
        prompt_tokens: Input-side tokens across every call so far.
        completion_tokens: Output-side tokens across every call so far.
    """

    tokens: int = 0
    cost_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def add(self, result: ProviderResult) -> None:
        """Fold one provider result into the running total."""
        self.tokens += result.tokens
        self.cost_usd += result.cost_usd
        self.prompt_tokens += result.prompt_tokens
        self.completion_tokens += result.completion_tokens

    def partial(self, model: str) -> ProviderResult:
        """Return the spend so far as a ``ProviderResult`` for the ledger."""
        return ProviderResult(
            text="", tokens=self.tokens, cost_usd=self.cost_usd, model=model,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens)


@dataclass
class BuildRound:
    """Structured result of one builder round.

    Attributes:
        round_no: The round number just completed.
        files_written: Workspace-relative paths written this round.
        rejected: ``(path, reason)`` pairs blocked by the write policy.
        tokens: Tokens consumed by the builder, summed across every call in
            the round (format retries included).
        cost_usd: Dollar cost reported by the builder, summed across every
            call in the round (zero for local models).
        model: The model that produced the output.
        builder_text: Raw builder output from the LAST call, retained for
            auditing.
        prompt_tokens: Input-side tokens (summed), when the builder reports the split.
        completion_tokens: Output-side tokens (summed), when the builder reports the split.
    """

    round_no: int
    files_written: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    builder_text: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def to_dict(self) -> dict:
        """Return a JSON-serializable view of the round."""
        return asdict(self)


class BuildSession:
    """Runs one guarded builder round and records progress and state."""

    def __init__(
        self,
        builder: Builder,
        workspace: Workspace,
        objective: Objective,
        total_rounds: int = 5,
        progress: Optional[ProgressReporter] = None,
        state: Optional[RunState] = None,
        format_retries: int = 1,
        on_retry: Optional[Callable[[], None]] = None,
    ) -> None:
        """Wire the session components.

        Args:
            builder: The provider-backed builder agent.
            workspace: Guarded workspace to write into.
            objective: The target with completion criteria.
            total_rounds: Maximum rounds, used for progress display.
            progress: Optional progress reporter; created if None.
            state: Optional shared-state recorder; created if None.
            format_retries: Corrective builder calls allowed per round when a
                non-empty response contains no valid FILE blocks (weaker local
                models often reply in prose first); ``0`` disables the retry.
            on_retry: Optional zero-arg callback invoked once per corrective
                format retry, just before the retry call is issued (journal
                hook — purely observational, never changes retry behavior).
        """
        self.builder = builder
        self.workspace = workspace
        self.objective = objective
        self.total_rounds = total_rounds
        self.format_retries = format_retries
        self.on_retry = on_retry
        self.progress = progress or ProgressReporter(
            total_rounds, events_path=f"{workspace.root}/.loop-engineer/events.jsonl"
        )
        self.state = state or RunState(
            workspace.root,
            {
                "goal": objective.goal,
                "done_when": objective.done_when,
                "builder": getattr(builder.provider, "model", "") or builder.provider.name,
            },
        )

    def build_round(self, round_no: int, feedback: str = "") -> BuildRound:
        """Run one builder round and return its structured result.

        Which shape the round takes is decided by the builder's provider:

        * A provider that declares ``edits_in_place`` gets the workspace
          directory and edits it itself; the session fingerprints the
          directory beforehand and harvests the difference afterwards. A turn
          that changed NO files earns up to ``format_retries`` corrective
          calls telling it to edit rather than describe.
        * Any other provider answers with FILE blocks. A non-empty reply
          carrying no valid block earns up to ``format_retries`` corrective
          calls restating the exact fence format.

        In both shapes the LAST response is what gets applied and recorded as
        ``builder_text``, and the token and cost fields on the returned
        ``BuildRound`` are the SUM across every call in the round, so a paid
        retry is never lost to cost accounting. An EMPTY response signals a
        provider failure, not a format problem, and is never retried.

        Args:
            round_no: The current round number (orchestrator-managed).
            feedback: Reviewer feedback from the prior round, if any.

        Returns:
            A ``BuildRound`` describing what was produced and written.

        Raises:
            ProviderError: When a builder call fails. A failure on a
                CORRECTIVE call carries the round's completed spend as
                ``partial_result`` so the caller can still ledger it.
            WorkspaceError: When an in-place round cannot inspect the
                workspace (it is not a git repository, or git failed).
        """
        if getattr(self.builder.provider, "edits_in_place", False):
            result, outcome, usage = self._round_in_place(round_no, feedback)
        else:
            result, outcome, usage = self._round_from_file_blocks(round_no, feedback)

        note = f"wrote {len(outcome.written)} file(s)"
        if outcome.rejected:
            note += f", rejected {len(outcome.rejected)} by guardrails"
        self.state.record_round(
            {
                "round_no": round_no,
                "files_written": outcome.written,
                "rejected": outcome.rejected,
                "verified": "pending",
                "decision": "pending",
                "note": note,
            }
        )
        self.progress.emit(round_no, "built", note)

        return BuildRound(
            round_no=round_no,
            files_written=outcome.written,
            rejected=outcome.rejected,
            tokens=usage.tokens,
            cost_usd=usage.cost_usd,
            model=result.model,
            builder_text=result.text,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
        )

    def _round_from_file_blocks(self, round_no: int, feedback: str):
        """Run a blind builder's round: ask for FILE blocks, then write them.

        Args:
            round_no: The current round number (for progress events).
            feedback: Reviewer feedback from the prior round, if any.

        Returns:
            ``(result, outcome, usage)`` — the last provider result, the write
            outcome, and the round's accumulated usage.

        Raises:
            ProviderError: When a builder call fails; a corrective call's
                failure carries the completed spend as ``partial_result``.
        """
        self.progress.emit(round_no, "building", f"querying {self.objective.goal[:48]}")
        result = self.builder.build(self.objective, feedback)
        usage = _RoundUsage()
        usage.add(result)

        attempts = 0
        while (attempts < self.format_retries
               and (result.text or "").strip()
               and not Workspace.parse(result.text)):
            attempts += 1
            if self.on_retry is not None:
                self.on_retry()
            self.progress.emit(round_no, "format-retry",
                               "no valid FILE blocks; asking the builder to reformat")
            reminder = (
                f"{feedback}\n\nFORMAT ERROR: your previous output ({len(result.text)} "
                "chars) contained no '=== FILE:' fence. Emit ONLY blocks in exactly "
                "this form:\n"
                "=== FILE: relative/path.ext ===\n<full contents>\n=== END FILE ==="
            )
            try:
                result = self.builder.build(self.objective, reminder)
            except ProviderError as exc:
                # The FIRST call completed and may have cost money; attach the
                # accumulated spend so the caller can still ledger it even
                # though no BuildRound will be returned.
                exc.partial_result = usage.partial(result.model)
                raise
            usage.add(result)

        self.progress.emit(round_no, "writing", "applying generated files")
        return result, self.workspace.apply(result.text), usage

    def _round_in_place(self, round_no: int, feedback: str):
        """Run an agentic builder's round: it edits, the engine harvests.

        The workspace is fingerprinted BEFORE the turn so the harvest reports
        exactly what this round changed — files seeded from an earlier slice
        are already in the baseline and are never claimed as this round's
        work. A turn that changes nothing is the in-place equivalent of a
        reply with no FILE blocks, so it earns the same bounded corrective
        retry; every harvest diffs against the ORIGINAL baseline, so edits
        made across turns all count.

        ``HEAD`` is recorded alongside the fingerprint, and EVERY turn — the
        first and each corrective retry — is followed by
        :meth:`Workspace.uncommit_to` before the harvest. A builder that
        committed its work in the worktree would otherwise look like it changed
        nothing (earning a pointless retry) and would leave proof-of-test
        unable to revert a change that already sits in ``HEAD``.

        Args:
            round_no: The current round number (for progress events).
            feedback: Reviewer feedback from the prior round, if any.

        Returns:
            ``(result, outcome, usage)`` — the last provider result, the
            harvest outcome, and the round's accumulated usage.

        Raises:
            ProviderError: When a builder call fails; a corrective call's
                failure carries the completed spend as ``partial_result``.
            WorkspaceError: When the workspace cannot be fingerprinted, or the
                builder left ``HEAD`` somewhere the baseline cannot be
                rewound to.
        """
        self.progress.emit(round_no, "building (in-place)",
                           f"querying {self.objective.goal[:48]}")
        before = self.workspace.fingerprint()
        base = self.workspace.head()
        result = self.builder.build_in_place(self.objective, feedback,
                                             cwd=self.workspace.root)
        usage = _RoundUsage()
        usage.add(result)
        self._uncommit(round_no, base)
        outcome = self.workspace.harvest(before)

        attempts = 0
        while (attempts < self.format_retries
               and not outcome.written
               and (result.text or "").strip()):
            attempts += 1
            if self.on_retry is not None:
                self.on_retry()
            self.progress.emit(round_no, "no-edit-retry",
                               "the turn changed no files; asking the builder to edit")
            try:
                result = self.builder.build_in_place(
                    self.objective, f"{feedback}\n\n{NO_EDIT_REMINDER}",
                    cwd=self.workspace.root)
            except ProviderError as exc:
                # The FIRST call completed and may have cost money; attach the
                # accumulated spend so the caller can still ledger it even
                # though no BuildRound will be returned.
                exc.partial_result = usage.partial(result.model)
                raise
            usage.add(result)
            self._uncommit(round_no, base)
            outcome = self.workspace.harvest(before)

        return result, outcome, usage

    def _uncommit(self, round_no: int, base: str) -> None:
        """Undo any commits the builder made in the worktree since *base*.

        Runs after every in-place turn and before the harvest, so the change
        the builder committed is back in the working tree where the harvest
        can see it and proof-of-test can stash it away.

        Args:
            round_no: The current round number (for the progress event).
            base: The workspace ``HEAD`` recorded before the turn.

        Raises:
            WorkspaceError: When ``HEAD`` cannot be rewound to *base*.
        """
        undone = self.workspace.uncommit_to(base)
        if undone:
            self.progress.emit(round_no, "uncommitted",
                               f"uncommitted {undone} builder commit(s)")
