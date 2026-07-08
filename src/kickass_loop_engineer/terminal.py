"""Terminal-state taxonomy for a loop run.

Every loop ends in exactly one ``TerminalState`` with evidence. The states mirror
the Agent SDK ``ResultMessage.subtype`` vocabulary plus ``oscillation`` and
``research_blocked``. A run may never report ``success`` without evidence —
"done" is proven, not asserted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TerminalState(Enum):
    """The exhaustive set of ways a loop run can end."""

    SUCCESS = "success"
    STALLED = "stalled"
    BLOCKED = "blocked"
    BUDGET_EXCEEDED = "budget_exceeded"
    APPROVAL_REQUIRED = "approval_required"
    OSCILLATION = "oscillation"
    RESEARCH_BLOCKED = "research_blocked"


@dataclass
class RunOutcome:
    """The result of a loop run: one state, a reason, and supporting evidence.

    Args:
        state: The terminal state reached.
        reason: Human-readable explanation.
        evidence: List of evidence records. Required non-empty when ``state`` is
            ``SUCCESS`` so success can never be a bare claim.

    Raises:
        ValueError: If ``state`` is ``SUCCESS`` with no evidence.
    """

    state: TerminalState
    reason: str = ""
    evidence: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.state is TerminalState.SUCCESS and not self.evidence:
            raise ValueError("SUCCESS requires evidence; 'done' must be proven")

    def to_dict(self) -> dict:
        """Return a JSON-serializable representation."""
        return {"state": self.state.value, "reason": self.reason, "evidence": self.evidence}
