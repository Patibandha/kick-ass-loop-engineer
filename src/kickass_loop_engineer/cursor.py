"""Resumable pipeline cursor: pipeline.json + events.jsonl per stage transition.

The cursor is the artifact the 2.0-M2 ``next`` protocol will read. Writes are
atomic (tmp file + os.replace) so a crash never leaves a half-written cursor.
Like the other run artifacts, the events log grows per transition; long-lived
callers rotate it between runs.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("kickass_loop_engineer.cursor")

_DIR = ".loop-engineer"


class PipelineCursor:
    """Persists the pipeline's current stage under ``<workspace>/.loop-engineer/``."""

    def __init__(self, workspace_root: str) -> None:
        self.root = os.path.abspath(workspace_root)
        self.dir = os.path.join(self.root, _DIR)
        self.path = os.path.join(self.dir, "pipeline.json")
        self.events_path = os.path.join(self.dir, "events.jsonl")
        self._state: dict[str, Any] = {}

    def start(self, run_id: str, objective_goal: str) -> None:
        """Begin (or restart) a run's cursor at the ``start`` stage."""
        self._state = {"protocol": 1, "run_id": run_id, "objective_goal": objective_goal,
                       "stage": "start", "status": "started", "detail": {},
                       "completed_slices": []}
        self._write()
        self._event("stage", {"stage": "start", "status": "started"})

    def set_stage(self, stage: str, status: str, detail: Optional[dict] = None) -> None:
        """Record a stage transition; one cursor write + one event line."""
        self._state.update(stage=stage, status=status, detail=detail or {})
        self._write()
        self._event("stage", {"stage": stage, "status": status, **(detail or {})})

    def complete_slice(self, role: str) -> None:
        """Mark a slice done so a resumed run skips it."""
        if role not in self._state.setdefault("completed_slices", []):
            self._state["completed_slices"].append(role)
        self._write()
        self._event("slice_completed", {"role": role})

    def load(self) -> Optional[dict]:
        """Return the persisted cursor, or None when absent/corrupt."""
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self._state = data
            return data
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def _write(self) -> None:
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._state, handle, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            logger.exception("could not persist pipeline cursor")

    def _event(self, event: str, payload: dict) -> None:
        line = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "run_id": self._state.get("run_id", ""), "event": event, "payload": payload}
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(self.events_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(line) + "\n")
        except OSError:
            logger.exception("could not append pipeline event")
