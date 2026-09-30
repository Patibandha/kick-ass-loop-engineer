"""Ollama provider for local models served via the Ollama runtime.

Targets the Ollama chat endpoint (default http://localhost:11434/api/chat) so
any pulled model tag — for example a Kimi coder model, Qwen, or Llama — can act
as the builder or reviewer. Local inference reports zero dollar cost, which the
engine treats as effectively unbudgeted spend.

Uses only the standard library so the package has no hard HTTP dependency.
Requests run through :func:`~.base.retry_transient`, so a daemon restart, a
connection reset, or a read timeout is retried with exponential backoff instead
of failing the round; malformed JSON is permanent and fails immediately.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .base import (
    Provider,
    ProviderError,
    ProviderResult,
    as_int,
    retry_transient,
)


class OllamaProvider(Provider):
    """Provider backed by a local or remote Ollama server."""

    name = "ollama"

    def __init__(
        self,
        model: str,
        host: str = "http://localhost:11434",
        timeout_seconds: float = 600.0,
        temperature: float = 0.2,
        retry_attempts: int = 3,
        retry_base_delay: float = 2.0,
    ) -> None:
        """Initialize the Ollama provider.

        Args:
            model: Pulled Ollama model tag, e.g. "kimi-k2" or "qwen2.5-coder".
            host: Base URL of the Ollama server.
            timeout_seconds: Per-request timeout.
            temperature: Sampling temperature passed to the model.
            retry_attempts: Total requests allowed per call when the failure is
                transient (see :func:`~.base.is_transient_error`).
            retry_base_delay: Seconds before the first retry; doubled each time.
        """
        self.model = model
        self.host = host.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        self.retry_attempts = retry_attempts
        self.retry_base_delay = retry_base_delay

    def complete(self, system: str, user: str) -> ProviderResult:
        """Send a chat completion request to Ollama and return the result.

        Args:
            system: Role/system instructions for the model.
            user: The concrete request or context.

        Returns:
            A ``ProviderResult`` with the reply text and the token split.

        Raises:
            ProviderError: On a permanent failure, or once every retry of a
                transient one (connection reset/refused, read timeout) is spent.
        """
        payload = {
            "model": self.model,
            "stream": False,
            "options": {"temperature": self.temperature},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        return retry_transient(
            lambda: self._request(payload),
            attempts=self.retry_attempts,
            base_delay=self.retry_base_delay,
        )

    def _request(self, payload: dict) -> ProviderResult:
        """Perform one chat request and parse the response body.

        Args:
            payload: The JSON body to POST to ``/api/chat``.

        Returns:
            The parsed ``ProviderResult``.

        Raises:
            ProviderError: On a network failure, a read timeout, or a body
                that is not valid JSON.
        """
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
        except TimeoutError as exc:
            # urlopen wraps a CONNECT timeout in URLError, but a socket read
            # timeout mid-response propagates bare — uncaught it crashes the
            # whole run instead of ending it in an honest blocked terminal.
            raise ProviderError(
                f"ollama timed out after {self.timeout_seconds:.0f}s") from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderError(f"ollama returned malformed JSON: {exc}") from exc

        message = body.get("message") or {}
        text = message.get("content", "")
        prompt_tokens = as_int(body.get("prompt_eval_count"))
        completion_tokens = as_int(body.get("eval_count"))
        return ProviderResult(
            text=text,
            tokens=prompt_tokens + completion_tokens,
            cost_usd=0.0,
            model=self.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
