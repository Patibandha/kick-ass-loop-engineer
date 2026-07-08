"""Single-round build session driven by the Claude Code orchestrator.

In the skill workflow, the Claude Code session owns the review/iterate loop and
calls one ``BuildSession`` round at a time. Each round runs the builder (a local
Ollama/Kimi model or any provider), writes guarded files into the workspace,
records progress and shared state, and returns a structured result for the
orchestrator to review and verify.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from .agents import Builder
from .objective import Objective
from .progress import ProgressReporter
from .state import RunState
from .workspace import Workspace


@dataclass
class BuildRound:
    """Structured result of one builder round.

    Attributes:
        round_no: The round number just completed.
        files_written: Workspace-relative paths written this round.
        rejected: ``(path, reason)`` pairs blocked by the write policy.
        tokens: Tokens consumed by the builder.
        cost_usd: Dollar cost reported by the builder (zero for local models).
        model: The model that produced the output.
        builder_text: Raw builder output, retained for auditing.
    """

    round_no: int
    files_written: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    builder_text: str = ""

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
    ) -> None:
        """Wire the session components.

        Args:
            builder: The provider-backed builder agent.
            workspace: Guarded workspace to write into.
            objective: The target with completion criteria.
            total_rounds: Maximum rounds, used for progress display.
            progress: Optional progress reporter; created if None.
            state: Optional shared-state recorder; created if None.
        """
        self.builder = builder
        self.workspace = workspace
        self.objective = objective
        self.total_rounds = total_rounds
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

        Args:
            round_no: The current round number (orchestrator-managed).
            feedback: Reviewer feedback from the prior round, if any.

        Returns:
            A ``BuildRound`` describing what was produced and written.
        """
        self.progress.emit(round_no, "building", f"querying {self.objective.goal[:48]}")
        result = self.builder.build(self.objective, feedback)

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
            tokens=result.tokens,
            cost_usd=result.cost_usd,
            model=result.model,
            builder_text=result.text,
        )
