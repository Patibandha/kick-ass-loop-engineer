"""Single-round build session driven by the Claude Code orchestrator.

In the skill workflow, the Claude Code session owns the review/iterate loop and
calls one ``BuildSession`` round at a time. Each round runs the builder (a local
Ollama/Kimi model or any provider), writes guarded files into the workspace,
records progress and shared state, and returns a structured result for the
orchestrator to review and verify.
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

        When the builder replies with non-empty text that contains no valid
        FILE blocks, the round issues up to ``format_retries`` corrective
        calls that restate the exact fence format. The LAST response is what
        gets applied and recorded as ``builder_text``; the token and cost
        fields on the returned ``BuildRound`` are the SUM across every call
        in the round, so a paid retry is never lost to cost accounting. An
        EMPTY response signals a provider failure, not a format problem, and
        is never retried.

        Args:
            round_no: The current round number (orchestrator-managed).
            feedback: Reviewer feedback from the prior round, if any.

        Returns:
            A ``BuildRound`` describing what was produced and written.
        """
        self.progress.emit(round_no, "building", f"querying {self.objective.goal[:48]}")
        result = self.builder.build(self.objective, feedback)
        tokens = result.tokens
        cost_usd = result.cost_usd
        prompt_tokens = result.prompt_tokens
        completion_tokens = result.completion_tokens

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
                exc.partial_result = ProviderResult(
                    text="", tokens=tokens, cost_usd=cost_usd,
                    model=result.model, prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens)
                raise
            tokens += result.tokens
            cost_usd += result.cost_usd
            prompt_tokens += result.prompt_tokens
            completion_tokens += result.completion_tokens

        self.progress.emit(round_no, "writing", "applying generated files")
        outcome = self.workspace.apply(result.text)

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
            tokens=tokens,
            cost_usd=cost_usd,
            model=result.model,
            builder_text=result.text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
