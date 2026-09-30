"""Persistent run state and shared context for the loop.

Writes a human- and model-readable ``STATE.md`` plus machine-readable round
records into the workspace's ``.loop-engineer`` directory. This is the shared memory
that keeps context intact across rounds and across different models: whichever
agent picks up next reads ``STATE.md`` and sees the full objective, configuration,
history, decisions, and open questions, so nothing is lost between iterations.

Note: ``events.jsonl`` and ``rounds.jsonl`` grow by one entry per round for the
life of a workspace. Long-lived callers (e.g. a workspace reused across many
objectives) should rotate or archive these files between runs; rotation is out
of scope for this engine and left to the caller.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("kickass_loop_engineer.state")

_DIR = ".loop-engineer"


class RunState:
    """Manages the persistent state artifacts for a single objective."""

    def __init__(self, workspace_root: str, objective_summary: dict[str, str]) -> None:
        """Initialize state storage under the workspace.

        Args:
            workspace_root: Absolute path to the workspace.
            objective_summary: Goal, done_when, and config details to record.
        """
        self.root = os.path.abspath(workspace_root)
        self.dir = os.path.join(self.root, _DIR)
        self.state_md = os.path.join(self.dir, "STATE.md")
        self.events_path = os.path.join(self.dir, "events.jsonl")
        self.rounds_path = os.path.join(self.dir, "rounds.jsonl")
        self.objective = objective_summary
        os.makedirs(self.dir, exist_ok=True)
        self._rounds: list[dict[str, Any]] = self._seed_rounds()

    def _seed_rounds(self) -> list[dict[str, Any]]:
        """Load prior round records for in-memory replay.

        Prefers ``rounds.jsonl`` when it exists (the append-based format that
        ``record_round`` writes). Only when the jsonl file is absent does it
        fall back to a legacy ``rounds.json`` array (pre-2.0 format), which is
        then migrated to ``rounds.jsonl`` and best-effort removed so newly
        recorded rounds are never lost on a later restart.
        """
        if os.path.exists(self.rounds_path):
            return self._replay_jsonl()
        return self._migrate_legacy_rounds()

    def _replay_jsonl(self) -> list[dict[str, Any]]:
        """Replay ``rounds.jsonl`` line-by-line, skipping corrupt lines."""
        rounds: list[dict[str, Any]] = []
        try:
            with open(self.rounds_path, "r", encoding="utf-8") as handle:
                for line_no, line in enumerate(handle, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rounds.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning(
                            "skipping corrupt round record at %s:%d", self.rounds_path, line_no
                        )
        except FileNotFoundError:
            pass
        except OSError:
            logger.exception("could not read rounds record")
        return rounds

    def _migrate_legacy_rounds(self) -> list[dict[str, Any]]:
        """Load legacy ``rounds.json``, migrate it to jsonl, and remove it."""
        legacy_path = os.path.join(self.dir, "rounds.json")
        try:
            with open(legacy_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return []
        except (json.JSONDecodeError, OSError):
            logger.exception("could not read legacy rounds.json")
            return []
        if not isinstance(data, list):
            logger.warning("legacy rounds.json is not a list; ignoring it")
            return []
        try:
            with open(self.rounds_path, "w", encoding="utf-8") as handle:
                for record in data:
                    handle.write(json.dumps(record) + "\n")
        except OSError:
            logger.exception("could not migrate legacy rounds.json to jsonl")
            return data
        try:
            os.remove(legacy_path)
        except OSError:
            # jsonl now exists and wins on the next init, so a lingering
            # legacy file is harmless — keep it and move on.
            logger.warning("could not remove legacy rounds.json after migration")
        return data

    def record_round(self, record: dict[str, Any]) -> None:
        """Append a round record and refresh the STATE.md summary.

        Args:
            record: Structured details of the completed round.
        """
        self._rounds.append(record)
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(self.rounds_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError:
            logger.exception("could not persist round record")
        self._write_state_md(self._rounds)

    def _write_state_md(self, rounds: list[dict[str, Any]]) -> None:
        """Render the shared-context STATE.md from recorded rounds."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        lines = [
            "# kickAssLoopEngineer run state",
            "",
            f"_Last updated: {now}_",
            "",
            "## Objective",
            f"- **Goal:** {self.objective.get('goal', '')}",
            f"- **Done when:** {self.objective.get('done_when', '')}",
            f"- **Builder model:** {self.objective.get('builder', '')}",
            "",
            "## Round history",
            "",
            "| Round | Files written | Verified | Decision |",
            "| ----- | ------------- | -------- | -------- |",
        ]
        for rec in rounds:
            files = len(rec.get("files_written", []))
            verified = rec.get("verified", "—")
            decision = rec.get("decision", "—")
            lines.append(f"| {rec.get('round_no', '?')} | {files} | {verified} | {decision} |")
        lines += ["", "## Notes & decisions", ""]
        for rec in rounds:
            note = rec.get("note")
            if note:
                lines.append(f"- Round {rec.get('round_no', '?')}: {note}")
        lines.append("")
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(self.state_md, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines))
        except OSError:
            logger.exception("could not write STATE.md")
