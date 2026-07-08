# src/kickass_loop_engineer/ledger.py
"""Cost ledger: a hard per-run cap and an advisory monthly spend ledger.

The per-run cap is enforced (the loop stops at ``budget_exceeded``). The monthly
ledger is advisory in 1.0 — it tracks cumulative subscription spend and warns past a
threshold (default 80% of the cap). State persists as JSON keyed by ``YYYY-MM`` so
spend survives across runs and process restarts.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("kickass_loop_engineer.ledger")


@dataclass
class CostLedger:
    """Tracks per-run and per-month dollar spend.

    Args:
        path: JSON file persisting monthly totals.
        run_cap_usd: Hard cap for a single run.
        month_cap_usd: Advisory monthly cap.
        warn_ratio: Fraction of the monthly cap that triggers a warning.
        month: The ``YYYY-MM`` bucket (caller supplies; runtime has no clock here).
    """

    path: str
    run_cap_usd: float = 10.0
    month_cap_usd: float = 200.0
    warn_ratio: float = 0.8
    month: str = ""

    def __post_init__(self) -> None:
        self._run_spent = 0.0
        self._months = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle).get("months", {})
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump({"months": self._months}, handle, indent=2)
        except OSError as exc:
            logger.warning("could not persist ledger: %s", exc)

    def record(self, usd: float) -> None:
        """Add spend to the current run and month, and persist."""
        self._run_spent += usd
        self._months[self.month] = round(self._months.get(self.month, 0.0) + usd, 6)
        self._save()

    def run_total(self) -> float:
        """Dollars spent in the current run."""
        return round(self._run_spent, 6)

    def month_total(self) -> float:
        """Dollars spent in the current month bucket (persisted)."""
        return round(self._months.get(self.month, 0.0), 6)

    def would_exceed_run(self, usd: float) -> bool:
        """True if adding ``usd`` would breach the hard per-run cap."""
        return (self._run_spent + usd) > self.run_cap_usd

    def month_warning(self) -> bool:
        """True if the month total has crossed the advisory warning threshold."""
        return self.month_total() > (self.month_cap_usd * self.warn_ratio)
