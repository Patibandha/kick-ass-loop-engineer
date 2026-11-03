"""``guardrails.gate_timeout_seconds`` sets how long each gate command may run."""

import pytest

from kickass_loop_engineer.config import build_verification_policy


def test_the_default_is_unchanged_when_unset():
    assert build_verification_policy({}).timeout_seconds == 300.0


@pytest.mark.parametrize("value", [900, 60.5, 10, 7200])
def test_a_valid_timeout_is_used(value):
    assert build_verification_policy({"guardrails": {"gate_timeout_seconds": value}}).timeout_seconds == float(value)


@pytest.mark.parametrize("value", ["900", True, 0, 9, 7201, -5])
def test_an_invalid_timeout_is_refused(value):
    with pytest.raises(RuntimeError, match="gate_timeout_seconds"):
        build_verification_policy({"guardrails": {"gate_timeout_seconds": value}})
