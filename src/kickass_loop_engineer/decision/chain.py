"""Ordered fallback chain of decision backends with a per-run circuit breaker.

Backends are tried in order; a :class:`DecisionError` falls through to the
next one. A backend that fails ``breaker_threshold`` times in a row is skipped
for the rest of the run, so an outage costs a few timeouts, not one per call.
The chain always ends in :class:`RulesBackend`, which cannot fail.
"""

from __future__ import annotations

import logging

from .base import DecisionBackend, DecisionBatch, DecisionError
from .jev import JevBackend
from .rules import RulesBackend

logger = logging.getLogger(__name__)

DEFAULT_BREAKER_THRESHOLD = 3


class DecisionChain(DecisionBackend):
    """Tries each backend in order and returns the first valid batch."""

    name = "chain"

    def __init__(self, backends: list,
                 breaker_threshold: int = DEFAULT_BREAKER_THRESHOLD) -> None:
        """Build the chain; a trailing :class:`RulesBackend` is guaranteed.

        Args:
            backends: Backends in preference order.
            breaker_threshold: Consecutive failures that open a backend's breaker.
        """
        chain = list(backends)
        if not chain or not isinstance(chain[-1], RulesBackend):
            chain.append(RulesBackend())
        self.backends = chain
        self.breaker_threshold = max(1, int(breaker_threshold))
        self._failures = {id(b): 0 for b in chain}
        self.fallbacks: list = []  # (backend name, reason) per fall-through

    def decide(self, state: dict, questions: list) -> DecisionBatch:
        """Return the first backend's valid answers (rules as last resort)."""
        for backend in self.backends:
            if self._is_open(backend):
                continue
            try:
                batch = backend.decide(state, questions)
            except DecisionError as exc:
                self._failures[id(backend)] += 1
                self.fallbacks.append((backend.name, str(exc)))
                logger.warning("decision backend %r failed (%s); falling back",
                               backend.name, exc)
                continue
            self._failures[id(backend)] = 0
            return batch
        # Unreachable while the rules backend terminates the chain, but a
        # decision must never be missing: answer with the defaults.
        return RulesBackend().decide(state, questions)

    def _is_open(self, backend: DecisionBackend) -> bool:
        """True when *backend*'s breaker is open (rules never opens)."""
        if isinstance(backend, RulesBackend):
            return False
        return self._failures[id(backend)] >= self.breaker_threshold


def build_chain(settings) -> DecisionChain:
    """Return the backend chain for a ``DecisionSettings`` (always ends in rules)."""
    if settings.backend == "rules":
        return DecisionChain([RulesBackend()])
    return DecisionChain([JevBackend(**settings.jev), RulesBackend()])
