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


@dataclass
class ProviderResult:
    """The outcome of a single provider completion.

    Attributes:
        text: The model's textual response.
        tokens: Total tokens consumed, when the backend reports them.
        cost_usd: Dollar cost of the call, when the backend reports it.
        model: Identifier of the model that produced the response.
    """

    text: str
    tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""


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
