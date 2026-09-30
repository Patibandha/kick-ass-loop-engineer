"""Progress reporting for transparent, observable loop runs.

Emits two streams so the orchestrating Claude Code session can surface exactly
what is happening:
    * A human-readable progress bar printed to stderr (round, phase, message).
    * A structured JSONL event log written into the workspace's ``.loop-engineer``
      directory, which the session can tail to narrate progress to the user.

stdout is reserved for the machine-readable result, so progress never corrupts it.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass
from typing import Optional, TextIO

logger = logging.getLogger("kickass_loop_engineer.progress")

_BAR_WIDTH = 20


@dataclass
class ProgressEvent:
    """A single observable step in a run.

    Attributes:
        round_no: Current round number.
        total_rounds: Maximum rounds for the run.
        phase: Short phase label (e.g. "building", "writing", "built").
        message: Human-readable detail.
        ts: Unix timestamp of the event.
    """

    round_no: int
    total_rounds: int
    phase: str
    message: str
    ts: float


class ProgressReporter:
    """Renders a progress bar and records structured progress events."""

    def __init__(
        self,
        total_rounds: int,
        events_path: Optional[str] = None,
        bar_stream: Optional[TextIO] = None,
        enabled: bool = True,
    ) -> None:
        """Initialize the reporter.

        Args:
            total_rounds: Maximum rounds, used to compute the bar fill.
            events_path: Optional path to append JSONL events to.
            bar_stream: Stream for the progress bar; defaults to stderr.
            enabled: When False, all output is suppressed.
        """
        self.total_rounds = max(1, total_rounds)
        self.events_path = events_path
        self.bar_stream = bar_stream or sys.stderr
        self.enabled = enabled
        if events_path:
            try:
                os.makedirs(os.path.dirname(events_path), exist_ok=True)
            except OSError:
                logger.exception("could not create events directory")

    def emit(self, round_no: int, phase: str, message: str = "") -> None:
        """Record an event and render the progress bar.

        Args:
            round_no: Current round number.
            phase: Short phase label.
            message: Optional human-readable detail.
        """
        if not self.enabled:
            return
        event = ProgressEvent(round_no, self.total_rounds, phase, message, time.time())
        self._write_event(event)
        self._render(event)

    def _write_event(self, event: ProgressEvent) -> None:
        """Append a JSONL event to the events log, ignoring write failures."""
        if not self.events_path:
            return
        try:
            os.makedirs(os.path.dirname(self.events_path) or ".", exist_ok=True)
            with open(self.events_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(event)) + "\n")
        except OSError:
            logger.exception("could not append progress event")

    def _render(self, event: ProgressEvent) -> None:
        """Print the progress bar line to the bar stream."""
        fraction = min(1.0, event.round_no / self.total_rounds)
        filled = int(_BAR_WIDTH * fraction)
        bar = "#" * filled + "-" * (_BAR_WIDTH - filled)
        detail = f" | {event.message}" if event.message else ""
        line = (
            f"[{bar}] {int(fraction * 100):3d}% | "
            f"round {event.round_no}/{self.total_rounds} | {event.phase}{detail}"
        )
        try:
            self.bar_stream.write(line + "\n")
            self.bar_stream.flush()
        except (OSError, ValueError):
            pass
