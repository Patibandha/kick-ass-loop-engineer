"""Staffing plan: the decision model sizes the team; floors keep quality.

Two batched decisions per run:

* ``staffing`` (after the first decompose): risk tier, architect on/off,
  security depth (L1-L4), QA on/off, DevOps/ship on/off, review depth, and
  whether builders must write tests.
* ``staffing:slices`` (after the final decompose): attempts per slice and the
  starting model tier per slice (cheap first when a tier ladder exists).

Every choice is clamped by :mod:`.floors` and persisted to
``.loop-engineer/staffing.json`` — the artifact the session ``next`` protocol
reads to skip or require QA / security / ship / sign-off.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Optional

from .base import Question
from .floors import REVIEW_LEVELS, SECURITY_LEVELS, raise_to, review_floor, security_floor

STAFFING_FILE = os.path.join(".loop-engineer", "staffing.json")
RISK_LEVELS = ("low", "medium", "high", "critical")
_BLOCK_ON = {"light": (), "standard": ("CRITICAL",), "strict": ("HIGH", "CRITICAL")}
TESTS_CONSTRAINT = ("Write or update automated tests that cover every new or "
                    "changed behaviour in this slice.")


@dataclass
class StaffingPlan:
    """The applied staffing for one run (after floors)."""

    run_id: str
    risk: str = "medium"
    architect: Optional[bool] = None
    security: str = "L2"
    qa: bool = True
    devops: bool = True
    review: str = "light"
    write_tests: bool = False
    domains: list = field(default_factory=list)
    slices: dict = field(default_factory=dict)  # role -> {"attempts", "tier"}
    floors: list = field(default_factory=list)  # why each floor applied

    def block_on(self, configured: tuple) -> tuple:
        """Review severities that block promotion: config ∪ review depth."""
        return tuple(dict.fromkeys(tuple(configured) + _BLOCK_ON[self.review]))

    def save(self, workspace: str) -> None:
        """Persist the plan atomically as the ``staffing.json`` artifact."""
        path = os.path.join(workspace, STAFFING_FILE)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2, sort_keys=True)
        os.replace(tmp, path)


def load_plan(workspace: str) -> Optional[dict]:
    """Return the persisted plan dict, or ``None`` when absent/corrupt."""
    path = os.path.join(workspace, STAFFING_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def run_questions(*, slice_count: int, architect_auto: bool,
                  configured_review: str) -> list:
    """Return the run-level staffing questions (defaults = pre-3.2 behavior)."""
    questions = [
        Question("risk", "How risky is this change if it ships with a defect?",
                 RISK_LEVELS, "medium",
                 {"low": "docs, tests, isolated internal tooling",
                  "medium": "ordinary feature or refactor",
                  "high": "user-facing, data-affecting, or broad refactor",
                  "critical": "security, money, auth, irreversible data or infra"}),
        Question("security", "How deep must the security review be?",
                 SECURITY_LEVELS, "L2",
                 {"L1": "automated security gates only",
                  "L2": "plus a dedicated AI security review stage",
                  "L3": "plus strict blocking review of HIGH/CRITICAL findings",
                  "L4": "plus human sign-off before ship"}),
        Question("qa", "Is a separate QA pass worth its cost here?", ("run", "skip"),
                 "run", {"run": "behavior/UX needs checking beyond unit gates",
                         "skip": "unit gates fully cover the change"}),
        Question("devops", "Does this change need a DevOps/ship stage?",
                 ("run", "skip"), "run",
                 {"run": "release, packaging, CI or deploy work is needed",
                  "skip": "local change, nothing to release"}),
        Question("review", "How strict should code review be?", REVIEW_LEVELS,
                 configured_review,
                 {"light": "findings are advisory", "standard": "CRITICAL blocks",
                  "strict": "HIGH and CRITICAL block"}),
        Question("tests", "Must the builders write new tests?",
                 ("gates_only", "write_tests"), "gates_only",
                 {"gates_only": "existing tests already cover it",
                  "write_tests": "new behavior needs new tests"}),
    ]
    if architect_auto:
        questions.append(Question(
            "architect", "Does this objective need a design-first architect stage?",
            ("run", "skip"), "run" if slice_count >= 2 else "skip",
            {"run": "several interacting parts or unclear structure",
             "skip": "a single well-scoped change"}))
    return questions


def slice_questions(slices: list, *, max_attempts: int, default_attempts: int,
                    tier_names: tuple) -> tuple:
    """Return ``(questions, ids)`` sizing each slice; ids maps role -> (a, t)."""
    questions, ids = [], {}
    attempt_opts = tuple(str(n) for n in range(1, max_attempts + 1))
    default = str(min(max(default_attempts, 1), max_attempts))
    for index, slice_ in enumerate(slices):
        tag = f"{index}_{re.sub(r'[^a-z0-9]+', '_', slice_.role.lower())[:24]}"
        attempts_id = f"attempts_{tag}"
        questions.append(Question(
            attempts_id,
            f"How many independent build attempts does slice '{slice_.role}' "
            f"need? Objective: {slice_.objective[:300]}",
            attempt_opts, default,
            {"1": "routine, one attempt will pass",
             str(max_attempts): "hard or ambiguous, competing attempts pay off"}))
        tier_id = ""
        if tier_names:
            tier_id = f"tier_{tag}"
            questions.append(Question(
                tier_id,
                f"Cheapest model tier likely to pass slice '{slice_.role}' "
                "(failures escalate to stronger tiers automatically)?",
                tier_names, tier_names[0]))
        ids[slice_.role] = (attempts_id, tier_id)
    return questions, ids


def apply_run_choices(plan: StaffingPlan, choices: dict, *, domains: list,
                      security_min: str, review_min: str,
                      signoff_domains: tuple) -> StaffingPlan:
    """Clamp the model's run-level choices by the floors into *plan*."""
    plan.domains = list(domains)
    plan.risk = choices.get("risk", "medium")
    if domains and plan.risk == "low":
        plan.risk = "medium"
        plan.floors.append(f"risk raised to medium: touches {domains}")
    sec_floor, reasons = security_floor(domains, plan.risk, config_min=security_min,
                                        signoff_domains=signoff_domains)
    plan.security = raise_to(choices.get("security", "L2"), sec_floor, SECURITY_LEVELS)
    plan.floors.extend(reasons)
    plan.review = raise_to(choices.get("review", "light"),
                           review_floor(domains, plan.risk, config_min=review_min),
                           REVIEW_LEVELS)
    if plan.security in ("L3", "L4"):
        plan.review = raise_to(plan.review, "strict", REVIEW_LEVELS)
    plan.qa = choices.get("qa", "run") == "run" or bool(domains)
    plan.devops = choices.get("devops", "run") == "run"
    plan.write_tests = choices.get("tests") == "write_tests" or plan.risk in ("high", "critical")
    if "architect" in choices:
        plan.architect = choices["architect"] == "run"
    return plan
