"""Gate output is journaled and mirrored into a vault that is pushed nightly, so secrets must not reach a
gate's environment, and anything secret-shaped in gate output is redacted before it is written."""

import subprocess

import pytest

from kickass_loop_engineer.decision.redact import redact_text
from kickass_loop_engineer.guardrails import VerificationPolicy

KEY = "sk-" + "a1B2c3D4e5F6g7H8i9J0"


@pytest.mark.parametrize("text", [f"assert '{KEY}' == ''", "token = abcdefgh12345678", "ghp_" + "x" * 36])
def test_secret_shaped_text_is_redacted(text):
    assert redact_text(text) != text and "[REDACTED]" in redact_text(text)


def test_ordinary_gate_output_is_left_alone():
    text = "exit=1; FAILED tests/test_x.py::test_y - assert 1 == 2\n10 failed, 1238 passed"
    assert redact_text(text) == text


@pytest.mark.parametrize("name", ["OPENAI_API_KEY", "GITHUB_TOKEN", "OLLAMA_API_KEY", "MY_SERVICE_SECRET",
                                  "DB_PASSWORD", "TYPESAFE_API_KEY", "AWS_SECRET_ACCESS_KEY"])
def test_a_credential_variable_never_reaches_a_gate(name, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("kickass_loop_engineer.confine.CONFINEMENT_AVAILABLE", False)
    monkeypatch.setenv(name, "value-that-must-not-leak")
    monkeypatch.setattr(subprocess, "run",
                        lambda argv, **kw: calls.append(kw) or subprocess.CompletedProcess(argv, 0, "", ""))
    assert VerificationPolicy().run("pytest -q", str(tmp_path)).passed
    assert name not in calls[0]["env"] and "PATH" in calls[0]["env"]
