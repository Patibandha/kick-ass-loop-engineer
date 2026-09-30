"""Deterministic decision backend: every question answers with its default.

The defaults encode the engine's pre-3.2 behavior, so a run whose model
backends are all unavailable behaves exactly like a run without the decision
layer. This backend cannot fail — it is always the last link of a chain.
"""

from __future__ import annotations

from .base import DecisionBackend, DecisionBatch, default_answer


class RulesBackend(DecisionBackend):
    """Answers every question with its declared default."""

    name = "rules"

    def decide(self, state: dict, questions: list) -> DecisionBatch:
        """Return each question's default answer (never raises)."""
        return DecisionBatch(
            answers={q.id: default_answer(q) for q in questions},
            backend=self.name, model="rules")
