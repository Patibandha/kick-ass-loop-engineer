"""Ollama provider for local models served via the Ollama runtime.

Targets the Ollama chat endpoint (default http://localhost:11434/api/chat) so
any pulled model tag — for example a Kimi coder model, Qwen, or Llama — can act
as the builder or reviewer. Local inference reports zero dollar cost, which the
engine treats as effectively unbudgeted spend.

Uses only the standard library so the package has no hard HTTP dependency.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .base import Provider, ProviderError, ProviderResult


class OllamaProvider(Provider):
    """Provider backed by a local or remote Ollama server."""

    name = "ollama"

    def __init__(
        self,
        model: str,
        host: str = "http://localhost:11434",
        timeout_seconds: float = 600.0,
        temperature: float = 0.2,
    ) -> None:
        """Initialize the Ollama provider.

        Args:
            model: Pulled Ollama model tag, e.g. "kimi-k2" or "qwen2.5-coder".
            host: Base URL of the Ollama server.
            timeout_seconds: Per-request timeout.
            temperature: Sampling temperature passed to the model.
        """
        self.model = model
        self.host = host.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature

    def complete(self, system: str, user: str) -> ProviderResult:
        """Send a chat completion request to Ollama and return the result."""
        payload = {
            "model": self.model,
            "stream": False,
            "options": {"temperature": self.temperature},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        request = urllib.request.Request(
            f"{self.host}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise ProviderError(f"ollama request failed: {exc}") from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderError(f"ollama returned malformed JSON: {exc}") from exc

        message = body.get("message") or {}
        text = message.get("content", "")
        tokens = int(body.get("prompt_eval_count", 0)) + int(body.get("eval_count", 0))
        return ProviderResult(text=text, tokens=tokens, cost_usd=0.0, model=self.model)
