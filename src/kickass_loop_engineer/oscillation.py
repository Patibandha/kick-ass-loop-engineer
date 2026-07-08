# src/kickass_loop_engineer/oscillation.py
"""Oscillation detection for the kickass loop engineer.

Oscillation occurs when the same findings repeat for ``window`` consecutive rounds
with no new evidence introduced. This signals the loop is stuck in a cycle and
should stop rather than continue spinning.

Key design decisions:
- Empty findings are treated as no-signal and excluded from the window check.
  Two consecutive empty observations do NOT constitute oscillation.
- Findings are normalized (stripped, order-independent) before comparison so
  ``["bug: x", "bug: y"]`` and ``["bug: y", "bug: x"]`` are treated as equal.
"""
from __future__ import annotations

from collections import deque


class OscillationDetector:
    """Detects when the loop is stuck repeating the same findings.

    Args:
        window: Number of consecutive non-empty rounds with identical findings
                required to declare oscillation.

    Attributes:
        window: The configured window size.
        _history: Bounded deque of normalized finding sets, one per observation.
            Capped at ``max(window * 8, 16)`` entries so long-lived processes
            cannot grow this without bound; only the last ``window`` non-empty
            entries are ever needed for detection.
    """

    def __init__(self, window: int = 2) -> None:
        self.window = window
        self._history: deque = deque(maxlen=max(window * 8, 16))

    def observe(self, findings: list) -> bool:
        """Record a new round of findings and return True if oscillating.

        Args:
            findings: The findings from this round (strings). Order does not matter;
                      duplicates and whitespace are normalized away.

        Returns:
            True if the last ``window`` non-empty observations are identical
            (same findings, no new evidence), False otherwise. Empty observations
            are excluded from the window and never trigger oscillation on their own.
        """
        normalized = frozenset(f.strip() for f in findings if f and f.strip())
        self._history.append(normalized)
        non_empty = [s for s in self._history if s]
        if len(non_empty) < self.window:
            return False
        last = non_empty[-self.window:]
        return bool(last[0]) and all(s == last[0] for s in last)
