#!/usr/bin/env python3
"""Expected-test check: prove every guardrail test actually ran, and passed, in a pytest run.

A guardrail test that is silently deselected, renamed, skipped or never collected leaves a green run that
proves nothing. This script reads the JUnit XML written by ``pytest --junitxml`` and a file of expected
pytest node IDs (one per line; blank lines and ``#`` comments ignored) and requires every expected ID to
appear in the report as a test case that neither was skipped nor failed.

Usage::

    python scripts/check_expected_tests.py junit.xml tests/expected-guardrail-ids.txt

Exit status: 0 when every expected test ran and passed; 1 naming each expected ID that is missing,
skipped or failed; 2 on a usage error (an unreadable file, malformed XML, an empty expected list).
Standard library only. Nothing from the environment is printed.

The expected file is regenerated from the collected tests (sorted, one per line) with::

    python -m pytest --collect-only -q -p no:cacheprovider \\
        -m "(guardrail_gate or guardrail_isolation or guardrail_proof) and not guardrail_isolation_capability"
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree

PASSED = "passed"
SKIPPED = "skipped"
FAILED = "failed"
MISSING = "missing"

#: Worst outcome wins when one test case appears more than once (for example, a rerun).
_RANK = {PASSED: 0, SKIPPED: 1, FAILED: 2}


class ExpectedTestsError(RuntimeError):
    """Raised when an input cannot be used (unreadable, malformed, or an empty expected list)."""


def junit_key(node_id: str) -> tuple[str, str]:
    """Map a pytest node ID to the ``(classname, name)`` pair pytest writes into JUnit XML.

    ``tests/test_x.py::TestA::test_b[p]`` becomes ``("tests.test_x.TestA", "test_b[p]")``. The parameter
    part is split off first, so a ``::`` inside a parameter ID cannot be taken for a class separator.

    Raises:
        ExpectedTestsError: When *node_id* is not of the form ``<file>.py::<test>``.
    """
    bracket = node_id.find("[")
    base, param = (node_id, "") if bracket == -1 else (node_id[:bracket], node_id[bracket:])
    parts = base.split("::")
    if len(parts) < 2 or not parts[0].endswith(".py") or not parts[-1]:
        raise ExpectedTestsError(f"not a pytest node ID: {node_id!r}")
    module = parts[0][: -len(".py")].replace("\\", "/").replace("/", ".")
    classname = ".".join([module, *parts[1:-1]])
    return classname, parts[-1] + param


def read_expected(path: str) -> list[str]:
    """Read the expected node IDs from *path*, skipping blank lines and ``#`` comments.

    Raises:
        ExpectedTestsError: When the file cannot be read, holds no IDs, or holds a malformed ID.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ExpectedTestsError(f"cannot read expected-ID file {path}: {exc}") from exc
    ids = [line.strip() for line in text.splitlines()]
    ids = [line for line in ids if line and not line.startswith("#")]
    if not ids:
        raise ExpectedTestsError(f"expected-ID file {path} lists no tests")
    for node_id in ids:
        junit_key(node_id)
    return ids


def _case_status(case: ElementTree.Element) -> str:
    """The outcome of one ``<testcase>`` element."""
    tags = {child.tag for child in case}
    if tags & {"failure", "error"}:
        return FAILED
    if "skipped" in tags:
        return SKIPPED
    return PASSED


def read_junit(path: str) -> dict[tuple[str, str], str]:
    """Read a JUnit XML report into ``{(classname, name): status}``.

    Raises:
        ExpectedTestsError: When the report cannot be read or parsed.
    """
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError) as exc:
        raise ExpectedTestsError(f"cannot read JUnit report {path}: {exc}") from exc
    results: dict[tuple[str, str], str] = {}
    for case in root.iter("testcase"):
        key = (case.get("classname", ""), case.get("name", ""))
        status = _case_status(case)
        previous = results.get(key)
        if previous is None or _RANK[status] > _RANK[previous]:
            results[key] = status
    return results


def check(expected: Sequence[str], results: dict[tuple[str, str], str]) -> list[tuple[str, str]]:
    """Return ``(node_id, problem)`` for each expected test that is missing, skipped or failed."""
    problems = []
    for node_id in expected:
        status = results.get(junit_key(node_id), MISSING)
        if status != PASSED:
            problems.append((node_id, status))
    return problems


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command-line entry point; returns the process exit status."""
    parser = argparse.ArgumentParser(description="Fail when an expected test did not run and pass.")
    parser.add_argument("junit", help="JUnit XML report written by pytest --junitxml")
    parser.add_argument("expected", help="file of expected pytest node IDs, one per line")
    args = parser.parse_args(argv)
    try:
        expected = read_expected(args.expected)
        results = read_junit(args.junit)
    except ExpectedTestsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    problems = check(expected, results)
    for node_id, problem in problems:
        print(f"{problem}: {node_id}")
    if problems:
        print(f"FAIL: {len(problems)} of {len(expected)} expected tests did not run and pass")
        return 1
    print(f"OK: all {len(expected)} expected tests ran and passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
