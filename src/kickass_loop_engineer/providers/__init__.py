"""Provider implementations and a name-based registry.

The registry maps configuration strings to provider classes so a backend can be
selected entirely from config, keeping the engine independent of any specific
model vendor.
"""

from __future__ import annotations

from typing import Type

from .anthropic_api import AnthropicProvider
from .base import Provider, ProviderError, ProviderResult
from .claude_code import ClaudeCodeProvider
from .ollama import OllamaProvider

PROVIDERS: dict[str, Type[Provider]] = {
    ClaudeCodeProvider.name: ClaudeCodeProvider,
    OllamaProvider.name: OllamaProvider,
    AnthropicProvider.name: AnthropicProvider,
}


def build_provider(name: str, **kwargs) -> Provider:
    """Instantiate a registered provider by name.

    Args:
        name: Registry key, e.g. "claude_code", "ollama", or "anthropic".
        **kwargs: Constructor arguments for the chosen provider.

    Returns:
        A configured ``Provider`` instance.

    Raises:
        ProviderError: When ``name`` is not a registered provider.
    """
    try:
        return PROVIDERS[name](**kwargs)
    except KeyError as exc:
        known = ", ".join(sorted(PROVIDERS))
        raise ProviderError(f"unknown provider {name!r}; available: {known}") from exc


__all__ = [
    "Provider",
    "ProviderResult",
    "ProviderError",
    "ClaudeCodeProvider",
    "OllamaProvider",
    "AnthropicProvider",
    "PROVIDERS",
    "build_provider",
]
