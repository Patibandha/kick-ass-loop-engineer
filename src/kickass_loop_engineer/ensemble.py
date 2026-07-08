# src/kickass_loop_engineer/ensemble.py
"""Ensemble with verified selection.

For a hard slice, run N independent attempts and keep the one whose gates PASS —
selection, never merging. Among passing attempts, prefer the simplest (fewest
files) as a tiebreak. If no attempt passes its gate, there is no winner: the loop
must not declare success on an unverified attempt.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class Attempt:
    """One ensemble attempt and its gate evidence.

    Args:
        id: Identifier for the attempt (e.g. the round/worktree name).
        workspace: Where the attempt's files live (e.g. an isolated worktree).
        evidence: The gate result for this attempt; only ``.passed`` is required.
        file_count: Number of files the attempt wrote (the simplicity tiebreak).
    """

    id: str
    workspace: str
    evidence: object
    file_count: int = 0


def select_winner(attempts: list) -> Optional[Attempt]:
    """Return the verified winner, or None if no attempt passes its gate.

    The winner is a passing attempt with the fewest files (simplest). Selection,
    not merging — losing attempts are discarded, never stitched together.
    """
    passing = [a for a in attempts if getattr(a.evidence, "passed", False)]
    if not passing:
        return None
    return min(passing, key=lambda a: a.file_count)


def run_ensemble(
    n: int,
    build_fn: Callable[[int], tuple],
    gate_fn: Callable[[str], object],
) -> list:
    """Run ``n`` attempts and return their gate evidence as ``Attempt`` records.

    Args:
        n: Number of attempts to run.
        build_fn: ``i -> (workspace, file_count)`` builds attempt ``i``.
        gate_fn: ``workspace -> evidence`` runs the gate for an attempt.

    Returns:
        A list of ``Attempt`` (selection is the caller's job via ``select_winner``).
    """
    attempts = []
    for i in range(n):
        workspace, file_count = build_fn(i)
        evidence = gate_fn(workspace)
        attempts.append(Attempt(id=str(i), workspace=workspace,
                                evidence=evidence, file_count=file_count))
    return attempts
