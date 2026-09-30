"""Append-only decision journal with replay: ``.loop-engineer/decisions.jsonl``.

Every decision is recorded with a key derived from its (state, questions)
input. A resumed run looks the key up and REUSES the recorded answers instead
of calling a model again, so resume stays deterministic even though the model
is not. Outcome lines (``kind: "outcome"``) close the loop for calibration.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Optional

_DIR = ".loop-engineer"
_FILE = "decisions.jsonl"


def decision_key(state: dict, questions: list) -> str:
    """Return the stable replay key for one decision input."""
    material = json.dumps({
        "state": state,
        "questions": [[q.id, list(q.options), q.default] for q in questions],
    }, sort_keys=True, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class DecisionLog:
    """Reads and appends the workspace decision journal."""

    def __init__(self, workspace: str, clock=time.time) -> None:
        """Bind to ``<workspace>/.loop-engineer/decisions.jsonl``."""
        self.path = os.path.join(workspace, _DIR, _FILE)
        self._clock = clock

    def append(self, record: dict) -> None:
        """Append one JSON line (the directory is created on demand)."""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        line = dict(record)
        line.setdefault("ts", self._clock())
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, sort_keys=True, default=str) + "\n")

    def records(self) -> list:
        """Return every parseable record in file order (corrupt lines skipped)."""
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
        return out

    def lookup(self, run_id: str, key: str) -> Optional[dict]:
        """Return the latest decision record for (*run_id*, *key*), if any."""
        found = None
        for rec in self.records():
            if (rec.get("kind") == "decision" and rec.get("run_id") == run_id
                    and rec.get("key") == key):
                found = rec
        return found
