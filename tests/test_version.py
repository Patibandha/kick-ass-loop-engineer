"""Version-agreement guard: the package and the distribution must not drift.

``__version__`` and ``pyproject.toml``'s ``version`` are bumped together (the
CHANGELOG documents the rule). This test pins that they AGREE and that they name
the current release, so a half-finished bump fails CI instead of shipping a
mismatched dist.
"""
import os
import re

import kickass_loop_engineer

EXPECTED_VERSION = "3.2.0"

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PYPROJECT = os.path.join(_ROOT, "pyproject.toml")


def _pyproject_version() -> str:
    """Read ``[project].version`` from pyproject.toml without a toml dependency."""
    with open(_PYPROJECT, "r", encoding="utf-8") as handle:
        text = handle.read()
    match = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', text)
    assert match, "pyproject.toml has no [project] version"
    return match.group(1)


def test_package_and_pyproject_versions_agree():
    assert kickass_loop_engineer.__version__ == _pyproject_version()


def test_version_is_current_release():
    assert kickass_loop_engineer.__version__ == EXPECTED_VERSION
    assert _pyproject_version() == EXPECTED_VERSION
