"""The verifier gate registry and the ``GateSpec`` describing one configured gate.

Each gate is an executable, allowlisted check in one of seven categories. A gate
returns an evidence record (see ``verifier.py``), never an opinion. Commands here
must all pass ``VerificationPolicy.validate`` — they are templates the orchestrator
fills with concrete targets at run time.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GateSpec:
    """One executable gate: a registry name plus the concrete command.

    Attributes:
        name: A gate name registered in ``gates.GATES`` (e.g. ``"unit"``).
        command: The allowlisted command the gate runs.
        prove: Whether to run proof-of-test (revert->red->green) for this gate.
        observe: Optional observation command run (under gate authority, with
            observer caps) when the gate fails; its output lands on the
            failing evidence record and feeds the builder's retry context.
    """

    name: str = "unit"
    command: str = "python3 -m pytest -q"
    prove: bool = True
    observe: str = ""


GATES: dict = {
    "unit": {"category": "unit", "example": "pytest -q",
             "desc": "unit tests with a pass/fail exit code"},
    "security": {"category": "security", "example": "bandit -r .",
                 "desc": "SAST scan (bandit/semgrep)"},
    "data_leak": {"category": "data_leak", "example": "gitleaks detect",
                  "desc": "secret/PII scan (gitleaks/detect-secrets)"},
    "performance": {"category": "performance", "example": "pytest-benchmark",
                    "desc": "latency/throughput budget check"},
    "smoke": {"category": "smoke", "example": "make test",
              "desc": "scripted happy-path end-to-end run"},
    "ui": {"category": "ui", "example": "npx playwright test",
           "desc": "browser DOM-assertion tests (Playwright) with a pass/fail exit code"},
    "architecture": {"category": "architecture", "example": "lint-imports",
                     "desc": "declared-architecture conformance (import-linter contracts)"},
}


def gate_categories() -> set:
    """Return the set of distinct gate categories."""
    return {spec["category"] for spec in GATES.values()}
