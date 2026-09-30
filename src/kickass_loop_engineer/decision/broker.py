"""The decision broker: one entry point from the engine to the decision layer.

For each batch of questions the broker (1) builds the allowlisted state,
(2) replays a recorded answer on resume, otherwise (3) asks the backend
chain, (4) replaces any answer below ``min_confidence`` with the question's
safe default, (5) meters the call, and (6) journals the decision. It returns
the APPLIED choice per question — floors and caps are the caller's job.
"""

from __future__ import annotations

from typing import Callable, Optional

from .base import DecisionError
from .chain import DecisionChain
from .log import DecisionLog, decision_key
from .redact import build_state
from .rules import RulesBackend

#: Default confidence below which a model answer is not acted on.
DEFAULT_MIN_CONFIDENCE = 0.7


class DecisionBroker:
    """Routes decisions through replay → chain → confidence gate → journal."""

    def __init__(self, chain: DecisionChain, log: DecisionLog, *, run_id: str,
                 min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                 on_cost: Optional[Callable[[str, object], None]] = None,
                 on_event: Optional[Callable[[str, dict], None]] = None) -> None:
        """Wire the broker.

        Args:
            chain: Backend chain (always ends in rules).
            log: Decision journal used for replay and audit.
            run_id: Run the decisions belong to (replay is per run).
            min_confidence: Answers below this confidence use the default.
            on_cost: ``(site, batch)`` ledger callback for paid calls.
            on_event: ``(event, payload)`` journal callback.
        """
        self.chain = chain
        self.log = log
        self.run_id = run_id
        self.min_confidence = float(min_confidence)
        self._on_cost = on_cost or (lambda site, batch: None)
        self._on_event = on_event or (lambda event, payload: None)

    def decide(self, site: str, facts: dict, questions: list) -> dict:
        """Answer *questions* for *site* and return ``{question_id: choice}``.

        Args:
            site: Where in the engine the decision is taken (e.g. ``"staffing"``).
            facts: Candidate state facts (allowlisted by :func:`build_state`).
            questions: The :class:`Question` list to answer.

        Returns:
            The applied option per question id — never missing, never outside
            the question's declared options.
        """
        try:
            state = build_state(facts)
            chain = self.chain
        except DecisionError as exc:
            state, chain = {}, DecisionChain([RulesBackend()])
            self._on_event("decision_state_rejected", {"site": site, "reason": str(exc)})
        key = decision_key(state, questions)
        replay = self.log.lookup(self.run_id, key)
        if replay is not None:
            applied = _replayed_choices(replay, questions)
            self._on_event("decision_replayed", {"site": site, "applied": applied})
            return applied

        before = len(chain.fallbacks)
        batch = chain.decide(state, questions)
        if batch.cost_usd > 0:
            self._on_cost(f"decide:{site}", batch)
        answers, applied = {}, {}
        for q in questions:
            ans = batch.answers[q.id]
            accepted = batch.backend == "rules" or ans.confidence >= self.min_confidence
            applied[q.id] = ans.choice if accepted else q.default
            answers[q.id] = {"choice": ans.choice, "applied": applied[q.id],
                             "accepted": accepted, "confidence": ans.confidence,
                             "probabilities": ans.probabilities}
        self.log.append({
            "kind": "decision", "run_id": self.run_id, "site": site, "key": key,
            "backend": batch.backend, "model": batch.model,
            "latency_ms": round(batch.latency_ms, 1), "cost_usd": batch.cost_usd,
            "fallbacks": chain.fallbacks[before:], "answers": answers,
        })
        self._on_event("decision_made", {"site": site, "backend": batch.backend,
                                         "applied": applied})
        return applied

    def record_outcome(self, outcome: dict) -> None:
        """Journal the run's outcome so decisions can be joined to results."""
        self.log.append({"kind": "outcome", "run_id": self.run_id, **outcome})


def _replayed_choices(record: dict, questions: list) -> dict:
    """Recover applied choices from a journal record (defaults if corrupt)."""
    answers = record.get("answers") if isinstance(record.get("answers"), dict) else {}
    applied = {}
    for q in questions:
        entry = answers.get(q.id) if isinstance(answers.get(q.id), dict) else {}
        choice = entry.get("applied")
        applied[q.id] = choice if choice in q.options else q.default
    return applied
