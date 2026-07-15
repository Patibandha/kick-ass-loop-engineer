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

    def record_mode(self, mode: str) -> None:
        """Persist the run's RESOLVED task mode as a durable cursor key.

        Like ``architect_done``, the live ``stage``/``detail`` fields are
        overwritten by every later ``set_stage``, so the resolved mode rides
        its own persisted key that resume and ``next`` readers find on the
        loaded cursor for the whole run.
        """
        self._state["mode"] = mode
        self._write()
        self._event("mode_resolved", {"mode": mode})

    def mark_repro_path(self, path: str) -> None:
        """Persist the committed repro test's path as a durable cursor key.

        The ``architect_done`` pattern: the live ``stage``/``detail`` fields
        are overwritten by every later ``set_stage``, so a resumed fix-mode
        run reads this key to skip regeneration/re-commit and just re-arm
        the proving gate.
        """
        self._state["repro_path"] = path
        self._write()
        self._event("repro_committed", {"repro_path": path})

    def mark_architect_done(self) -> None:
        """Persist the durable architect-done flag so a resumed run skips the stage.

        The live ``stage`` field is overwritten by every later ``set_stage``,
        so resume detection reads this key (mirroring ``completed_slices``)
        from the loaded cursor instead.
        """
        self._state["architect_done"] = True
        self._write()
        self._event("architect_done", {})

    def complete_slice(self, role: str) -> None:
        """Mark a slice done so a resumed run skips it."""
        if role not in self._state.setdefault("completed_slices", []):
            self._state["completed_slices"].append(role)
        self._write()
        self._event("slice_completed", {"role": role})

    def emit(self, event: str, payload: dict) -> None:
        """Append one event-ONLY journal line; ``pipeline.json`` is untouched.

        The public emit path for high-frequency journal events (per-attempt
        starts, gate verdicts, observer runs, format retries, cost records).
        These must NOT go through ``set_stage``: the cursor's live ``stage``
        means "where the run is" for resume/``next`` readers, and thrashing it
        per attempt would corrupt that signal. ``events.jsonl`` is the full
        append-only history; ``pipeline.json`` is the current position.

        Replay tolerance (the journal contract): the journal is append-only
        and NEVER deduplicated. A resumed run re-announces durable facts —
        ``mode_resolved`` appears once per (re)start of the same run_id, and
        any event may recur across resumes — so consumers reconstructing a
        timeline must tolerate repeated events; the writer keeps no dedupe
        state by design.

        Args:
            event: Event name (e.g. ``"attempt_started"``).
            payload: JSON-serializable event details.
        """
        self._event(event, payload)

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
