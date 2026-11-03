#!/usr/bin/env python3
"""Diagnostic: list audit-log operations that are not in docs/acceptance/operation-inventory.md.

Usage::

    python scripts/compare_audit.py audit.jsonl [docs/acceptance/operation-inventory.md]

The log is the JSON-lines file written by ``tests/audit_monitor.py``. An operation is matched by
(event, file, function); only the table under ``## Expected operations`` counts. Each operation that is not
listed is printed once with how often it happened. Exit status is always 0: this is a diagnostic, never a
gate, and its output is never guardrail evidence. Standard library only.
"""

import json
import os
import sys

DEFAULT_INVENTORY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "docs", "acceptance", "operation-inventory.md")
SECTION = "## Expected operations"


def expected(path):
    """The (event, file, function) keys in the inventory's expected table."""
    keys, inside = set(), False
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("## "):
                inside = line.strip() == SECTION
            elif inside and line.startswith("|"):
                cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
                if len(cells) >= 3 and cells[0] not in ("Event", "") and not set(cells[0]) <= set("-: "):
                    keys.add(tuple(cells[:3]))
    return keys


def unexpected(log, keys):
    """Map each unlisted (event, file, function) in *log* to [count, first line number]."""
    found = {}
    with open(log, encoding="utf-8") as handle:
        for raw in handle:
            try:
                row = json.loads(raw)
                key = (row["event"], row["file"], row["func"])
            except (ValueError, KeyError, TypeError):
                continue
            if key not in keys:
                found.setdefault(key, [0, row.get("line")])[0] += 1
    return found


def main(argv):
    if not 2 <= len(argv) <= 3:
        print(__doc__)
        return 0
    try:
        found = unexpected(argv[1], expected(argv[2] if len(argv) == 3 else DEFAULT_INVENTORY))
    except OSError as exc:
        print(f"compare_audit: cannot read input: {exc}", file=sys.stderr)
        return 0
    for (event, file, func), (count, line) in sorted(found.items()):
        print(f"unexpected: {event} {file}::{func} (line {line}, {count}x)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
