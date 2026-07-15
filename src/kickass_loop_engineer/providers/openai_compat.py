"""OpenAI-compatible provider: any endpoint speaking /chat/completions.

One adapter covers most web providers (OpenAI, Gemini via its OpenAI layer, Grok,
DeepSeek, Mistral, Qwen, OpenRouter, Groq, Together) and every mainstream local
runtime (Ollama, llama.cpp server, LM Studio, vLLM, Jan) — only base_url, model,
and the API-key env var change. The key is read from an env var NAME so secrets
never live in config files. Uses only the standard library.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Optional, Sequence, Union

from .base import Provider, ProviderError, ProviderResult, as_int

_ERROR_BODY_PREVIEW_CHARS = 500
_IMAGE_MIME_BY_EXTENSION = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
_DEFAULT_IMAGE_MIME = "image/png"


def _image_content_part(path: str) -> dict:
    """Encode one image file as an OpenAI ``image_url`` content part.

    The MIME type is derived from the file extension (png/jpg/jpeg); unknown
    extensions default to ``image/png``.

    Args:
        path: Filesystem path of the image to embed.

    Returns:
        A ``{"type": "image_url", ...}`` content part with a base64 data URL.

    Raises:
        ProviderError: When the image file cannot be read.
    """
    extension = os.path.splitext(path)[1].lower()
    mime = _IMAGE_MIME_BY_EXTENSION.get(extension, _DEFAULT_IMAGE_MIME)
    try:
        with open(path, "rb") as handle:
            encoded = base64.b64encode(handle.read()).decode("ascii")
    except OSError as exc:
        raise ProviderError(f"openai_compat could not read image {path!r}: {exc}") from exc
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


class OpenAICompatProvider(Provider):
    """Provider backed by any OpenAI-compatible chat-completions endpoint."""

    name = "openai_compat"

    def __init__(
        self,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        api_key_env: str = "OPENAI_API_KEY",
        temperature: Optional[float] = None,
        max_tokens: int = 0,
        timeout_seconds: float = 300.0,
    ) -> None:
        """Initialize the OpenAI-compatible provider.

        Args:
            model: Model identifier understood by the endpoint,
                e.g. "gpt-4.1", "deepseek-chat", or a local model tag.
            base_url: API root that exposes ``/chat/completions``,
                e.g. "https://api.openai.com/v1" or "http://localhost:11434/v1".
            api_key_env: NAME of the environment variable holding the API key.
                When the variable is unset or empty no Authorization header is
                sent, which keeps key-less local runtimes working.
            temperature: Sampling temperature; omitted from the request when
                None so the endpoint's default applies.
            max_tokens: Response token cap; omitted from the request when <= 0.
            timeout_seconds: Per-request timeout. The 300s default deliberately
                matches the Anthropic provider — long completions on slow local
                endpoints are normal.
        """
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds

    def complete(self, system: str, user: str, images: Sequence[str] = ()) -> ProviderResult:
        """Send a chat-completions request and return the result.

        Args:
            system: Role/system instructions for the model.
            user: The concrete request or context.
            images: Optional image file paths for vision-capable models. When
                non-empty, the user message content becomes the OpenAI
                content-array shape: one text part followed by one base64
                data-URL ``image_url`` part per image. When empty, the request
                body is byte-identical to a text-only call.

        Returns:
            A ``ProviderResult`` with the response text, the prompt/completion
            token split from ``usage``, and their sum as ``tokens``.

        Raises:
            ProviderError: On an unreadable image path, HTTP or network
                failure, malformed JSON, or a response carrying no usable
                choices.
        """
        user_content: Union[str, list] = user
        if images:
            user_content = [{"type": "text", "text": user}]
            user_content.extend(_image_content_part(path) for path in images)
        payload: dict = {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.max_tokens > 0:
            payload["max_tokens"] = self.max_tokens

        headers = {"Content-Type": "application/json"}
        api_key = os.environ.get(self.api_key_env, "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")[:_ERROR_BODY_PREVIEW_CHARS]
            raise ProviderError(f"openai_compat HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"openai_compat request failed: {exc}") from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderError(f"openai_compat returned malformed JSON: {exc}") from exc

        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderError("openai_compat response carried no choices")

        text = ((choices[0].get("message") or {}).get("content")) or ""
        usage = body.get("usage") or {}
        prompt_tokens = as_int(usage.get("prompt_tokens"))
        completion_tokens = as_int(usage.get("completion_tokens"))
        return ProviderResult(
            text=text,
            tokens=prompt_tokens + completion_tokens,
            cost_usd=0.0,
            model=self.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
