"""The verifier gate registry and the ``GateSpec`` describing one configured gate.

Each gate is an executable, allowlisted check in one of seven categories. A gate
returns an evidence record (see ``verifier.py``), never an opinion. Commands here
must all pass ``VerificationPolicy.validate`` — they are templates the orchestrator
fills with concrete targets at run time.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def default_python(os_name: str = os.name) -> str:
    """The interpreter name the engine's own gate commands use on this host.

    Windows has no real ``python3``: the name is the Microsoft Store stub, which never runs a suite.
    """
    return "python" if os_name == "nt" else "python3"


#: The gate an unconfigured run uses.
DEFAULT_GATE_CMD = f"{default_python()} -m pytest -q"


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
        expect: Optional regex the gate's OUTPUT must match for the gate to
            pass. An exit code alone cannot tell a green suite from one that
            ran nothing: on 2026-09-21 a container built from a cache holding
            zero-byte files collected NO tests, exited 0, and passed the gate,
            while every command in that image exited 0 because every module was
            empty. ``expect`` is how a gate states what its output must prove.
        min_count: Optional floor on the FIRST capture group of *expect*, read
            as an integer — the test count that must not shrink. Requires
            *expect*; a suite that quietly halves is a regression, not a pass.
    """

    name: str = "unit"
    command: str = DEFAULT_GATE_CMD
    prove: bool = True
    observe: str = ""
    expect: str = ""
    min_count: int | None = None


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
