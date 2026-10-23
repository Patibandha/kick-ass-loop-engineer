"""Shared test setup."""

import os
import sys

import pytest


def pytest_configure(config):
    """Register the labelled capability-job marker (kept out of the main isolation run)."""
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
