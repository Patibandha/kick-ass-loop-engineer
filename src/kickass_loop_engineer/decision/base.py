"""Decision-layer contracts: questions, answers, and the backend interface.

A decision backend answers *closed* questions: every question declares the
options it may pick from, and every answer names exactly one of them together
with a probability per option and a confidence. Backends choose; the engine
disposes — deterministic floors and caps are applied by the caller, never here.
"""

from __future__ import annotations

import abc
import math
from dataclasses import dataclass, field
from typing import Optional

#: Tolerance for a probability vector that should sum to one.
PROBABILITY_SUM_TOLERANCE = 0.05


class DecisionError(Exception):
    """Raised when a backend cannot produce a valid decision.

    Always recoverable: the decision chain falls through to the next backend,
    and the last backend (rules) cannot raise.
    """


@dataclass(frozen=True)
class Question:
    """One closed question put to a decision backend.

    Attributes:
        id: Stable identifier (``[a-z0-9_]``), unique within a batch.
        instructions: What is being decided, in plain language.
        options: Ordered option ids the backend may choose from.
        default: The option the engine uses when no confident answer exists —
            always today's deterministic behavior, so a fallback never changes
            what a pre-3.2 run would have done.
        criteria: Optional per-option description shown to the backend.
    """

    id: str
    instructions: str
    options: tuple
    default: str
    criteria: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.options:
            raise ValueError(f"question {self.id!r} declares no options")
        if self.default not in self.options:
            raise ValueError(
                f"question {self.id!r} default {self.default!r} is not one of "
                f"its options {list(self.options)}")


@dataclass(frozen=True)
class Answer:
    """A backend's answer to one question.

    Attributes:
        choice: The chosen option (always one of the question's options).
        probabilities: Option -> probability; sums to one.
        confidence: Backend confidence in ``[0, 1]``.
    """

    choice: str
    probabilities: dict
    confidence: float


@dataclass
class DecisionBatch:
    """Every answer from ONE backend call plus its usage accounting.

    The ``model``/``cost_usd``/token attributes let the orchestrator's cost
    funnel price a batch exactly like a provider result.
    """

    answers: dict
    backend: str
    model: str = ""
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tokens: int = 0


class DecisionBackend(abc.ABC):
    """A source of answers to closed questions."""

    name: str = "backend"

    @abc.abstractmethod
    def decide(self, state: dict, questions: list) -> DecisionBatch:
        """Answer every question in *questions* against *state*.

        Args:
            state: Redacted, allowlisted facts about the run.
            questions: The :class:`Question` list to answer.

        Returns:
            A :class:`DecisionBatch` holding an :class:`Answer` per question id.

        Raises:
            DecisionError: When no valid answer can be produced.
        """


def validate_answer(question: Question, choice: object, probabilities: object,
                    confidence: object) -> Answer:
    """Check a raw answer against its question and return a normalized one.

    Args:
        question: The question being answered.
        choice: The raw chosen option.
        probabilities: Raw option -> probability mapping.
        confidence: Raw confidence value.

    Returns:
        A validated :class:`Answer` whose probabilities cover exactly the
        question's options and sum to one.

    Raises:
        DecisionError: On any choice, probability, or confidence that does not
            fit the question — a malformed answer is never acted on.
    """
    if choice not in question.options:
        raise DecisionError(
            f"question {question.id!r}: choice {choice!r} is not a declared option")
    if not isinstance(probabilities, dict):
        raise DecisionError(f"question {question.id!r}: probabilities missing")
    unknown = set(probabilities) - set(question.options)
    if unknown:
        raise DecisionError(
            f"question {question.id!r}: probabilities name undeclared options "
            f"{sorted(unknown)}")
    probs = {opt: _unit_float(probabilities.get(opt, 0.0), question.id)
             for opt in question.options}
    total = sum(probs.values())
    if abs(total - 1.0) > PROBABILITY_SUM_TOLERANCE:
        raise DecisionError(
            f"question {question.id!r}: probabilities sum to {total:.3f}, not 1")
    probs = {opt: p / total for opt, p in probs.items()}
    return Answer(choice=str(choice), probabilities=probs,
                  confidence=_unit_float(confidence, question.id))


def _unit_float(value: object, question_id: str) -> float:
    """Return *value* as a finite float in ``[0, 1]`` or raise DecisionError."""
    if isinstance(value, bool):
        raise DecisionError(f"question {question_id!r}: boolean is not a probability")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise DecisionError(
            f"question {question_id!r}: {value!r} is not a number") from exc
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise DecisionError(
            f"question {question_id!r}: {number!r} is outside [0, 1]")
    return number


def default_answer(question: Question, confidence: Optional[float] = 1.0) -> Answer:
    """Return the deterministic answer: the question's default, fully weighted."""
    probs = {opt: (1.0 if opt == question.default else 0.0) for opt in question.options}
    return Answer(choice=question.default, probabilities=probs,
                  confidence=float(confidence if confidence is not None else 1.0))
