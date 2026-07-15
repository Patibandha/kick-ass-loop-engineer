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


#: Auto temperature ladder for default (spec-less) ensembles: the first three
#: attempts probe conservative -> creative, later attempts step up by 0.3 and
#: never exceed the cap (deterministic — the same n always yields the same specs).
_LADDER_HEAD = (0.2, 0.7, 1.0)
_LADDER_STEP = 0.3
_LADDER_MAX = 1.5


@dataclass(frozen=True)
class AttemptSpec:
    """How one ensemble attempt's builder should be constructed.

    Diversity lives entirely in construction: a spec never influences winner
    selection (gates decide). Empty ``model``/``provider`` mean "use the
    configured base builder"; ``temperature=None`` keeps the provider default.

    Args:
        model: Model identifier for this attempt ("" = the base builder's model).
        temperature: Sampling temperature; ``None`` keeps the provider default.
        provider: Provider registry name ("" = the base builder's provider).
        family: Declared model family for cross-model independence checks
            (mirrors the ``builder.family`` knob for third-party models).
    """

    model: str = ""
    temperature: Optional[float] = None
    provider: str = ""
    family: str = ""


def default_specs(n: int) -> list:
    """Return the deterministic default specs for an *n*-attempt ensemble.

    Temperatures follow the ladder ``0.2, 0.7, 1.0`` then ``+0.3`` steps
    capped at ``1.5``; model/provider stay empty (the base builder is reused).

    Args:
        n: Number of attempts.

    Returns:
        ``n`` :class:`AttemptSpec` records varying only in temperature.
    """
    specs = []
    for i in range(n):
        if i < len(_LADDER_HEAD):
            temperature = _LADDER_HEAD[i]
        else:
            steps = i - (len(_LADDER_HEAD) - 1)
            temperature = min(_LADDER_HEAD[-1] + _LADDER_STEP * steps, _LADDER_MAX)
        specs.append(AttemptSpec(temperature=round(temperature, 2)))
    return specs


@dataclass
class Attempt:
    """One ensemble attempt and its gate evidence.

    Args:
        id: Identifier for the attempt (e.g. the round/worktree name).
        workspace: Where the attempt's files live (e.g. an isolated worktree).
        evidence: The gate result for this attempt; only ``.passed`` is required.
        file_count: Number of files the attempt wrote (the simplicity tiebreak).
        spec: The :class:`AttemptSpec` this attempt was built from (``None``
            for spec-less callers — back-compat).
    """

    id: str
    workspace: str
    evidence: object
    file_count: int = 0
    spec: Optional[AttemptSpec] = None


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
    specs: Optional[list] = None,
) -> list:
    """Run ``n`` attempts and return their gate evidence as ``Attempt`` records.

    Args:
        n: Number of attempts to run.
        build_fn: ``i -> (workspace, file_count)`` builds attempt ``i``.
        gate_fn: ``workspace -> evidence`` runs the gate for an attempt.
        specs: Optional per-attempt :class:`AttemptSpec` list; ``specs[i]``
            rides on attempt ``i`` (``None`` leaves every ``Attempt.spec``
            unset — back-compat).

    Returns:
        A list of ``Attempt`` (selection is the caller's job via ``select_winner``).
    """
    attempts = []
    for i in range(n):
        workspace, file_count = build_fn(i)
        evidence = gate_fn(workspace)
        spec = specs[i] if specs and i < len(specs) else None
        attempts.append(Attempt(id=str(i), workspace=workspace,
                                evidence=evidence, file_count=file_count,
                                spec=spec))
    return attempts
