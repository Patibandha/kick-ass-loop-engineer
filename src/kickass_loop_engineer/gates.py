"""The verifier gate registry.

Each gate is an executable, allowlisted check in one of five categories. A gate
returns an evidence record (see ``verifier.py``), never an opinion. Commands here
must all pass ``VerificationPolicy.validate`` — they are templates the orchestrator
fills with concrete targets at run time.
"""
from __future__ import annotations

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
}


def gate_categories() -> set:
    """Return the set of distinct gate categories."""
    return {spec["category"] for spec in GATES.values()}
