"""Provider abstraction for pluggable model backends.

A provider is any service that can turn a (system, user) prompt pair into text:
Claude Code in headless mode, a local Ollama model (Kimi, Llama, Qwen, ...), the
Anthropic API, or any OpenAI-compatible endpoint. The loop engine and agents
depend only on this interface, so the backend is chosen at configuration time
and never leaks into the loop logic.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass


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


class ProviderError(RuntimeError):
    """Raised when a provider cannot complete a request."""
