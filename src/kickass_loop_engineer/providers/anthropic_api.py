"""Anthropic API provider using the Messages endpoint.

Calls the Anthropic API directly over HTTP (standard library only). Intended for
predictable pay-as-you-go billing rather than subscription credit; the API key
is read from the ``ANTHROPIC_API_KEY`` environment variable unless passed in.

Requests run through :func:`~.base.retry_transient`, so an overloaded API
(HTTP 529), a rate limit (429), a 5xx, or a read timeout is retried with
exponential backoff; a missing key, a 4xx, or malformed JSON fails immediately.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional

from .base import (
    Provider,
    ProviderError,
    ProviderResult,
    as_int,
    retry_transient,
)

_ENDPOINT = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"


class AnthropicProvider(Provider):
    """Provider backed by the Anthropic Messages API."""

    name = "anthropic"

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        api_key: Optional[str] = None,
        max_tokens: int = 4096,
        timeout_seconds: float = 300.0,
        retry_attempts: int = 3,
        retry_base_delay: float = 2.0,
    ) -> None:
        """Initialize the Anthropic API provider.

        Args:
            model: Model identifier to call.
            api_key: API key; falls back to the ANTHROPIC_API_KEY env var.
            max_tokens: Maximum tokens to generate per response.
            timeout_seconds: Per-request timeout.
            retry_attempts: Total requests allowed per call when the failure is
                transient (see :func:`~.base.is_transient_error`).
            retry_base_delay: Seconds before the first retry; doubled each time.
        """
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.retry_attempts = retry_attempts
        self.retry_base_delay = retry_base_delay

    def complete(self, system: str, user: str) -> ProviderResult:
        """Send a Messages API request and return the result.

        Args:
            system: Role/system instructions for the model.
            user: The concrete request or context.

        Returns:
            A ``ProviderResult`` with the reply text and the token split.

        Raises:
            ProviderError: When no API key is configured, on a permanent HTTP
                or parsing failure, or once every retry of a transient one
                (429/5xx/529, connection failure, read timeout) is spent.
        """
        if not self.api_key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")

        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        return retry_transient(
            lambda: self._request(payload),
            attempts=self.retry_attempts,
            base_delay=self.retry_base_delay,
        )

    def _request(self, payload: dict) -> ProviderResult:
        """Perform one Messages request and parse the response body.

        Args:
            payload: The JSON body to POST to the Messages endpoint.

        Returns:
            The parsed ``ProviderResult``.

        Raises:
            ProviderError: On an HTTP error (the status is kept in the message
                so the transient classifier can read it), a network failure, a
                read timeout, or a body that is not valid JSON.
        """
        request = urllib.request.Request(
            _ENDPOINT,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": _API_VERSION,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ProviderError(f"anthropic HTTP {exc.code}: {exc.read().decode('utf-8', 'ignore')}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"anthropic request failed: {exc}") from exc
        except TimeoutError as exc:
            # urlopen wraps a CONNECT timeout in URLError, but a socket read
            # timeout mid-response propagates bare — uncaught it escapes as a
            # non-ProviderError and skips both the retry and the ledger.
            raise ProviderError(
                f"anthropic timed out after {self.timeout_seconds:.0f}s") from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderError(f"anthropic returned malformed JSON: {exc}") from exc

        text = "".join(
            block.get("text", "") for block in body.get("content", []) if block.get("type") == "text"
        )
        usage = body.get("usage") or {}
        prompt_tokens = as_int(usage.get("input_tokens"))
        completion_tokens = as_int(usage.get("output_tokens"))
        return ProviderResult(
            text=text,
            tokens=prompt_tokens + completion_tokens,
            cost_usd=0.0,
            model=self.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
