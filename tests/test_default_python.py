"""The interpreter name in the engine's own gate commands must run on the host.

On Windows ``python3`` is the Microsoft Store stub (it exits 9009 or is not found), so a default gate or a
reproduce check spelled ``python3 -m pytest`` could never pass there.
"""

import pytest

from kickass_loop_engineer.gates import GateSpec, default_python
from kickass_loop_engineer.guardrails import VerificationPolicy
from kickass_loop_engineer.reproduce import repro_check_command


@pytest.mark.parametrize(("os_name", "expected"), [("nt", "python"), ("posix", "python3")])
def test_the_default_interpreter_is_python_on_windows_and_python3_elsewhere(os_name, expected):
    assert default_python(os_name) == expected


def test_the_default_gate_uses_the_hosts_interpreter():
    assert GateSpec().command == f"{default_python()} -m pytest -q"


def test_the_reproduce_check_uses_the_hosts_interpreter_and_stays_allowlisted():
    command = repro_check_command("tests/test_repro_x.py")
    assert command == f"{default_python()} -m pytest tests/test_repro_x.py -q --noconftest -o addopts="
    ok, reason = VerificationPolicy().validate(command)
    assert ok, reason
