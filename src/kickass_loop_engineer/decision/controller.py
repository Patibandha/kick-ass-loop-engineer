"""Staffing controller: the orchestrator's single hook into the decision layer.

It turns broker answers into engine settings — review block-on severities,
builder test constraints, architect on/off, attempts and model tier per
slice, one stall-recovery attempt — and persists the applied plan. Every
setting is already clamped by the floors before the engine sees it.
"""

from __future__ import annotations

import re
from typing import Optional

from ..ensemble import default_specs
from .base import Question
from .broker import DecisionBroker
from .floors import detect_domains
from .settings import DecisionSettings
from .staffing import (StaffingPlan, apply_run_choices, run_questions,
                       slice_questions)

#: Outcome records from earlier runs shown to the model as history.
_HISTORY_RUNS = 5


def configured_review(block_on: tuple) -> str:
    """Map configured ``review.block_on`` severities to a review depth."""
    upper = {s.upper() for s in block_on}
    if "HIGH" in upper:
        return "strict"
    if "CRITICAL" in upper:
        return "standard"
    return "light"


class StaffingController:
    """Plans and applies the run's staffing through a :class:`DecisionBroker`."""

    def __init__(self, broker: DecisionBroker, settings: DecisionSettings,
                 workspace: str, run_id: str) -> None:
        """Bind the controller to one run."""
        self.broker = broker
        self.settings = settings
        self.workspace = workspace
        self.plan = StaffingPlan(run_id=run_id)
        self._facts: dict = {}
        self._tier_index: dict = {}  # role -> starting tier index

    def plan_run(self, *, objective, slices: list, mode: str, architect_auto: bool,
                 block_on: tuple) -> StaffingPlan:
        """Decide run-level staffing (risk, security, qa, devops, review, tests)."""
        text = " ".join([objective.goal, objective.done_when, objective.constraints]
                        + [s.objective for s in slices])
        domains = detect_domains(text)
        self._facts = {
            "objective": objective.goal, "done_when": objective.done_when,
            "constraints": objective.constraints, "mode": mode,
            "slice_count": len(slices), "sensitive_domains": domains,
            "slices": [{"role": s.role, "objective": s.objective[:300],
                        "depends_on": list(s.depends_on)} for s in slices],
            "configured": {"max_attempts": self.settings.max_attempts,
                           "tiers": len(self.settings.tiers)},
            "run_history": self._history(),
        }
        questions = run_questions(slice_count=len(slices), architect_auto=architect_auto,
                                  configured_review=configured_review(block_on))
        choices = self.broker.decide("staffing", self._facts, questions)
        apply_run_choices(self.plan, choices, domains=domains,
                          security_min=self.settings.security_min,
                          review_min=self.settings.review_min,
                          signoff_domains=self.settings.signoff_domains)
        self.plan.save(self.workspace)
        return self.plan

    def plan_slices(self, slices: list, default_attempts: int) -> StaffingPlan:
        """Decide attempts and starting model tier for every slice."""
        tier_names = self.settings.tier_names()
        questions, ids = slice_questions(
            slices, max_attempts=self.settings.max_attempts,
            default_attempts=default_attempts, tier_names=tier_names)
        facts = dict(self._facts, slices=[
            {"role": s.role, "objective": s.objective[:300]} for s in slices])
        choices = self.broker.decide("staffing:slices", facts, questions)
        for role, (attempts_id, tier_id) in ids.items():
            tier = tier_names.index(choices[tier_id]) if tier_id else 0
            self._tier_index[role] = tier
            self.plan.slices[role] = {"attempts": int(choices[attempts_id]), "tier": tier}
        self.plan.save(self.workspace)
        return self.plan

    def attempt_specs(self, role: str, base_specs: list) -> tuple:
        """Return ``(specs, cascade)`` for *role*'s ensemble.

        With a tier ladder, attempts climb from the chosen starting tier and
        the ensemble stops at the first pass (cascade: cheapest passing model
        wins). Without one, the configured specs are trimmed or extended to the
        chosen count and selection stays with the gates.
        """
        count = int(self.plan.slices.get(role, {}).get("attempts", len(base_specs) or 1))
        tiers = list(self.settings.tiers)
        if tiers:
            start = self._tier_index.get(role, 0)
            return [tiers[min(start + i, len(tiers) - 1)] for i in range(count)], True
        specs = list(base_specs[:count])
        if len(specs) < count:
            specs += default_specs(count)[len(specs):]
        return specs, False

    def recovery_spec(self, role: str, used: list, failed_gates: list) -> Optional[object]:
        """Ask whether ONE more attempt should follow a stall; return its spec."""
        tiers = list(self.settings.tiers)
        if tiers:
            highest = max((tiers.index(s) for s in used if s in tiers), default=-1)
            if highest >= len(tiers) - 1:
                return None
            candidate = tiers[highest + 1]
        else:
            candidate = default_specs(len(used) + 1)[-1]
        tag = re.sub(r"[^a-z0-9]+", "_", role.lower())[:24]
        question = Question(
            f"recover_{tag}",
            f"Every attempt at slice '{role}' failed its gates. Retry once more "
            "(with the failure observations, on a stronger model if available) "
            "or stop and report the stall?",
            ("retry", "stop"), "stop")
        facts = dict(self._facts, slice={"role": role, "attempts": len(used)},
                     failed_gates=failed_gates)
        choice = self.broker.decide(f"recover:{role}", facts, [question])
        return candidate if choice[question.id] == "retry" else None

    def record_outcome(self, outcome: dict) -> None:
        """Journal the run outcome together with the applied plan."""
        self.broker.record_outcome(dict(outcome, plan={
            "risk": self.plan.risk, "security": self.plan.security,
            "review": self.plan.review, "qa": self.plan.qa,
            "devops": self.plan.devops, "slices": self.plan.slices}))

    def _history(self) -> list:
        """Return the last few run outcomes (economics + plan) as model context."""
        outcomes = [r for r in self.broker.log.records() if r.get("kind") == "outcome"]
        return [{k: r.get(k) for k in ("state", "usd", "attempts", "plan")}
                for r in outcomes[-_HISTORY_RUNS:]]
