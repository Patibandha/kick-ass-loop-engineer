"""Shared test setup."""

import os
import sys

import pytest


def pytest_configure(config):
    """Register the labelled markers (capability job kept out of the main isolation run; proof negatives; CI)."""
    config.addinivalue_line(
        "markers",
        "guardrail_proof: proof-of-test negative tests (missing, forged, stale and cancelled proofs)",
    )
    config.addinivalue_line(
        "markers",
        "guardrail_proof_transition: a verdict changed between proof and use, torn run-state writes, and "
        "gate launch failures after a valid proof",
    )
    config.addinivalue_line(
        "markers",
        "guardrail_proof_authority: attacks by code running as the gate on the proof and run state "
        "(docs/acceptance/proof-authority.md)",
    )
    config.addinivalue_line(
        "markers",
        "guardrail_gate_confinement: gate processes write only inside their working folder and a private "
        "temp folder (Windows); unconfined runs say so in their evidence",
    )
    config.addinivalue_line(
        "markers",
        "mutation_check:tests for scripts/check_mutations.py and the patches under mutations/",
    )
    config.addinivalue_line(
        "markers",
        "ci_workflow: tests for .github/workflows/ci.yml, the expected guardrail IDs and "
        "scripts/check_expected_tests.py",
    )
    config.addinivalue_line(
        "markers",
        "proof_binding: tests for docs/acceptance/proof-binding.md, authority-map.md and decisions.md",
    )
    config.addinivalue_line(
        "markers",
        "audit_monitor: tests for the diagnostic audit monitor (tests/audit_monitor.py, "
        "scripts/compare_audit.py); never guardrail evidence",
    )
    config.addinivalue_line(
        "markers",
        "guardrail_isolation_capability: isolation cases needing a host capability (symlinks); "
        "capability missing is reported only under this marker",
    )


@pytest.fixture(autouse=True)
def _interpreter_folder_first_on_path(monkeypatch):
    """Resolve bare ``pytest``/``python`` gates to this interpreter, as in an activated venv.

    Gates look programs up on PATH; without this, running the suite as ``.venv/Scripts/python -m pytest``
    from an un-activated shell finds another Python (or the Windows Store stub) and real gates fail.
    """
    folder = os.path.dirname(sys.executable)
    monkeypatch.setenv("PATH", os.pathsep.join([folder, os.environ.get("PATH", "")]))
