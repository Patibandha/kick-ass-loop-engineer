"""Autonomy-level gating for the loop engineer.

L3 (unattended) is the standing operational default but engages only after the
readiness checklist passes.  When a gate cannot be proven the system auto-drops
to L2 (assisted) rather than charging ahead blind.  L1 and L2 always pass
through unchanged — no checklist required.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AutonomyLevel(Enum):
    """Three-tier autonomy classification.

    L1 — report_only: the loop surfaces findings but takes no action.
    L2 — assisted: the loop acts with human review at each checkpoint.
    L3 — unattended: the loop runs end-to-end without human checkpoints;
         only available once the :class:`ReadinessChecklist` is fully satisfied.
    """

    L1 = "report_only"
    L2 = "assisted"
    L3 = "unattended"


@dataclass
class ReadinessChecklist:
    """Gate conditions that must all be True before L3 autonomy is permitted.

    Fields (in evaluation order):
    - ``verifier_proven``: at least one verifier gate has passed on a prior run.
    - ``budget_set``: a cost/token budget is configured and enforced.
    - ``denylist_active``: the escalation denylist is loaded and operative.
    - ``workspace_isolated``: the build workspace is git-isolated from the host.

    Use :meth:`ready` to check all-clear, :meth:`gaps` to enumerate failures.
    """

    verifier_proven: bool
    budget_set: bool
    denylist_active: bool
    workspace_isolated: bool

    # Canonical field order used by gaps(); must match declaration order above.
    _FIELDS: tuple[str, ...] = (
        "verifier_proven",
        "budget_set",
        "denylist_active",
        "workspace_isolated",
    )

    def ready(self) -> bool:
        """Return True only when every gate field is True."""
        return all(getattr(self, f) for f in self._FIELDS)

    def gaps(self) -> list[str]:
        """Return the names of every gate field that is currently False.

        Results are returned in declaration order so callers can present them
        in a stable, human-readable sequence.
        """
        return [f for f in self._FIELDS if not getattr(self, f)]


def resolve_level(requested: AutonomyLevel, checklist: ReadinessChecklist) -> AutonomyLevel:
    """Resolve the effective autonomy level given the requested level and checklist state.

    L3 is the aspirational default but requires a clean checklist; if any gate
    is unmet the function returns L2 (assisted) instead so a human stays in the
    loop rather than the system charging ahead blind.  L1 and L2 are always
    returned as-is regardless of checklist state.

    Args:
        requested: The autonomy level the caller would like to operate at.
        checklist: Current gate state for this session.

    Returns:
        The effective :class:`AutonomyLevel` to use.
    """
    if requested is AutonomyLevel.L3 and not checklist.ready():
        return AutonomyLevel.L2
    return requested
