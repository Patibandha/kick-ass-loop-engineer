"""Provider abstraction for pluggable model backends.

A provider is any service that can turn a (system, user) prompt pair into text:
Claude Code in headless mode, a local Ollama model (Kimi, Llama, Qwen, ...), the
Anthropic API, or any OpenAI-compatible endpoint. The loop engine and agents
depend only on this interface, so the backend is chosen at configuration time
and never leaks into the loop logic.

Two backend shapes exist:

* **Blind chat providers** answer with text and never touch the filesystem; the
  engine parses ``=== FILE: ... ===`` blocks out of that text and writes them.
* **Agentic providers** (``edits_in_place = True``) drive their own tools and
  edit the working directory themselves; the engine hands them a directory via
  :meth:`Provider.edit` and afterwards harvests whatever changed.

This module also owns the shared transient-failure policy. Every backend fails
the same way under load (HTTP 429/5xx/529 "overloaded", rate limits, timeouts),
so the classification (:func:`is_transient_error`) and the exponential-backoff
retry (:func:`retry_transient`) live here instead of being re-implemented — and
re-tuned — in each provider.
"""

from __future__ import annotations

import abc
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger("kickass_loop_engineer.providers")

#: Case-insensitive markers of a failure worth retrying. HTTP status codes are
#: matched on word boundaries so a byte count ("1529 bytes") is never mistaken
#: for a status ("HTTP 529"). Everything absent from this list — auth errors,
#: malformed JSON, a missing binary, a bad request — is permanent by default:
#: retrying it only burns the budget.
_TRANSIENT_PATTERNS = (
    r"\b(?:408|425|429|500|502|503|504|529)\b",
    r"overloaded",
    r"rate[ \-_]?limit",
    r"too many requests",
    r"timed out",
    r"timeout",
    r"temporarily unavailable",
    r"connection reset",
    r"connection refused",
    r"server error",
)

_TRANSIENT_RE = re.compile("|".join(_TRANSIENT_PATTERNS), re.IGNORECASE)


def as_int(value) -> int:
    """Coerce a usage count that may be null/absent/non-numeric to a safe int.

    Backends occasionally report token counts as ``null``, strings, or omit
    them entirely; a bare ``int(...)`` would raise on those. Any value that
    cannot be coerced counts as zero.

    Args:
        value: A raw usage count from a provider response.

    Returns:
        The value as an ``int``, or 0 when it is falsy or non-numeric.
    """
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def is_transient_error(exc: BaseException) -> bool:
    """Return True when *exc* describes a failure that is worth retrying.

    Classification is purely textual (``str(exc)``, case-insensitive) because
    every backend reports overload differently — an HTTP status, a JSON error
    type, a CLI's stderr line — and the engine only ever sees the flattened
    message on a ``ProviderError``.

    Transient: HTTP 408/425/429/500/502/503/504/529 (matched on word
    boundaries), "overloaded", any spelling of "rate limit", "too many
    requests", "timed out"/"timeout", "temporarily unavailable", "connection
    reset", "connection refused", "server error".

    Permanent (never retried): authentication failures, malformed JSON, a
    missing binary, an unknown model — retrying those cannot change the answer.

    Args:
        exc: The exception to classify.

    Returns:
        True when the message matches a transient marker, False otherwise.
    """
    return bool(_TRANSIENT_RE.search(str(exc)))


def retry_transient(
    call: Callable[[], "ProviderResult"],
    *,
    attempts: int = 3,
    base_delay: float = 2.0,
    sleep=time.sleep,
    is_transient=is_transient_error,
    on_retry: Optional[Callable[[int, BaseException], None]] = None,
) -> "ProviderResult":
    """Invoke *call*, retrying transient :class:`ProviderError` failures.

    Backoff is exponential: retry ``i`` (0-based) waits
    ``base_delay * 2 ** i`` seconds, so the default policy sleeps 2s then 4s
    across three total calls. A non-transient error propagates from the first
    call — the loop must not spend three model calls on a missing API key. When
    every attempt is exhausted the LAST error is re-raised unchanged, so the
    caller still sees the real failure (and any ``partial_result`` attached to
    it).

    Args:
        call: Zero-argument callable performing one provider invocation.
        attempts: Total calls allowed, including the first (values below 1 are
            treated as 1).
        base_delay: Seconds to wait before the first retry; doubled each time.
        sleep: Injection point for the delay (tests pass a recorder).
        is_transient: Injection point for the classification predicate.
        on_retry: Optional ``(attempt_index, exc)`` callback invoked before
            each sleep — observational only, it never changes retry behavior.

    Returns:
        Whatever *call* returns on its first successful invocation.

    Raises:
        ProviderError: The last failure, once the attempt budget is spent, or
            immediately when the failure is not transient.
    """
    total = max(1, int(attempts))
    for index in range(total):
        try:
            return call()
        except ProviderError as exc:
            if not is_transient(exc) or index >= total - 1:
                raise
            delay = base_delay * (2 ** index)
            logger.warning(
                "transient provider failure (%s); retry %d/%d in %.1fs",
                exc, index + 1, total - 1, delay)
            if on_retry is not None:
                on_retry(index, exc)
            sleep(delay)
    # Unreachable: the final iteration always returns or re-raises.
    raise ProviderError("retry_transient exhausted without a result")


@dataclass
class ProviderResult:
    """The outcome of a single provider completion.

    Attributes:
        text: The model's textual response.
        tokens: Total tokens consumed, when the backend reports them.
        cost_usd: Dollar cost of the call, when the backend reports it.
        model: Identifier of the model that produced the response.
        prompt_tokens: Input-side tokens, when the backend reports the split.
        completion_tokens: Output-side tokens, when the backend reports the split.
    """

    text: str
    tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


class Provider(abc.ABC):
    """Abstract model backend used by builder and reviewer agents."""

    name: str = "provider"

    #: True when the backend edits the working directory itself (an agentic CLI
    #: driving Read/Edit/Write tools) instead of returning FILE blocks for the
    #: engine to write. The build session branches on this: an in-place builder
    #: is handed the attempt's worktree via :meth:`edit` and the engine harvests
    #: whatever changed; a blind provider keeps the FILE-block path.
    edits_in_place: bool = False

    @abc.abstractmethod
    def complete(self, system: str, user: str) -> ProviderResult:
        """Return a completion for the given system and user prompts.

        Args:
            system: Role/system instructions for the model.
            user: The concrete request or context.

        Returns:
            A ``ProviderResult`` carrying the response text and any usage data.

        Raises:
            ProviderError: When the backend fails to produce a response.
        """
        raise NotImplementedError

    def edit(self, system: str, user: str, *, cwd: str) -> ProviderResult:
        """Edit the workspace at *cwd* in place and return the prose summary.

        Only providers that declare ``edits_in_place = True`` implement this;
        the default refuses loudly rather than silently completing without
        touching a file.

        Args:
            system: Role/system instructions for the model.
            user: The concrete request or context.
            cwd: Existing directory the backend must treat as the workspace.

        Returns:
            A ``ProviderResult`` whose text is the backend's summary of the
            edits it made (the edits themselves are already on disk).

        Raises:
            ProviderError: Always, for backends that cannot edit in place.
        """
        raise ProviderError(f"{self.name} cannot edit a workspace in place")


class ProviderError(RuntimeError):
    """Raised when a provider cannot complete a request."""
