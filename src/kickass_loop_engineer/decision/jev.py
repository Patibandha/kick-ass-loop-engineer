"""TypeSafe Jev decision backend (hosted System One model, stdlib HTTP only).

Every question is sent as a Jev ``choice`` question in ONE request — Jev
ingests the state once and answers all questions against it — and every answer
is strictly validated before the engine may act on it.

API reference: ``POST https://api.typesafe.ai/v1/systemone`` with
``{model, state, questions: {id: {type: "choice", instructions, criteria}}}``;
the response carries ``answers: {id: {choice, probabilities, confidence}}`` and
``usage: {input_tokens, output_tokens}`` (docs.typesafe.ai/api).

The ``cloudflare`` route reaches the same model through Cloudflare Workers AI
(``typesafe/jev``, zero data retention, same $0.042/M input price):
``POST {base}/accounts/{account_id}/ai/run`` with ``{model, input: {state,
questions}}``; the REST envelope's ``result`` carries the Jev response
(developers.cloudflare.com/ai/models/typesafe/jev).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from ..providers.base import ProviderError, retry_transient
from .base import DecisionBackend, DecisionBatch, DecisionError, validate_answer

#: Pinned model: aliases like ``jev-latest`` move when TypeSafe ships, which
#: would silently change decisions under tuned thresholds.
DEFAULT_MODEL = "jev-1.13.0"
ROUTES = ("typesafe", "cloudflare")
_BASE_URLS = {"typesafe": "https://api.typesafe.ai",
              "cloudflare": "https://api.cloudflare.com/client/v4"}
_KEY_ENVS = {"typesafe": "TYPESAFE_API_KEY", "cloudflare": "CLOUDFLARE_API_TOKEN"}
_CLOUDFLARE_MODEL = "typesafe/jev"
DEFAULT_ACCOUNT_ID_ENV = "CLOUDFLARE_ACCOUNT_ID"
DEFAULT_TIMEOUT_S = 5.0
#: Published input price: $0.042 per million input tokens; output is free.
USD_PER_INPUT_TOKEN = 0.042 / 1_000_000
_ENDPOINT = "/v1/systemone"
_MS_PER_S = 1000.0


class JevBackend(DecisionBackend):
    """Answers closed questions through TypeSafe's hosted Jev model."""

    name = "jev"

    def __init__(self, model: str = DEFAULT_MODEL, base_url: str = "",
                 api_key_env: str = "", route: str = "typesafe",
                 account_id_env: str = DEFAULT_ACCOUNT_ID_ENV,
                 timeout_s: float = DEFAULT_TIMEOUT_S, retries: int = 1,
                 sleep=time.sleep) -> None:
        """Configure the client.

        Args:
            model: Pinned Jev model id (TypeSafe route; Cloudflare serves
                ``typesafe/jev`` and the response names the real version).
            base_url: API root; empty selects the route's default.
            api_key_env: NAME of the env var holding the bearer key — the key
                itself never lives in config. Empty selects the route default
                (``TYPESAFE_API_KEY`` / ``CLOUDFLARE_API_TOKEN``).
            route: ``typesafe`` (direct) or ``cloudflare`` (Workers AI).
            account_id_env: NAME of the env var holding the Cloudflare account id.
            timeout_s: Per-request timeout in seconds.
            retries: Extra attempts on a transient failure (429/5xx/529).
            sleep: Backoff sleep, injectable for tests.

        Raises:
            ValueError: On an unknown route.
        """
        if route not in ROUTES:
            raise ValueError(f"jev route must be one of {ROUTES}, got {route!r}")
        self.route = route
        self.model = model
        self.base_url = (base_url or _BASE_URLS[route]).rstrip("/")
        self.api_key_env = api_key_env or _KEY_ENVS[route]
        self.account_id_env = account_id_env
        self.timeout_s = float(timeout_s)
        self.retries = max(0, int(retries))
        self._sleep = sleep

    def decide(self, state: dict, questions: list) -> DecisionBatch:
        """Send one batched request and return the validated answers.

        Raises:
            DecisionError: Missing key, HTTP/network failure, or any answer
                that fails validation.
        """
        api_key = os.environ.get(self.api_key_env, "")
        if not api_key:
            raise DecisionError(f"{self.api_key_env} is not set")
        jev_input = {"state": state,
                     "questions": {q.id: _to_jev_question(q) for q in questions}}
        if self.route == "cloudflare":
            account_id = os.environ.get(self.account_id_env, "")
            if not account_id:
                raise DecisionError(f"{self.account_id_env} is not set")
            url = f"{self.base_url}/accounts/{account_id}/ai/run"
            payload = {"model": _CLOUDFLARE_MODEL, "input": jev_input}
        else:
            url = self.base_url + _ENDPOINT
            payload = dict(jev_input, model=self.model)
        started = time.monotonic()
        try:
            body = retry_transient(lambda: self._post(url, payload, api_key),
                                   attempts=1 + self.retries, base_delay=0.5,
                                   sleep=self._sleep)
        except ProviderError as exc:
            raise DecisionError(f"jev request failed: {exc}") from exc
        latency_ms = (time.monotonic() - started) * _MS_PER_S
        return self._parse(body, questions, latency_ms)

    def _post(self, url: str, payload: dict, api_key: str) -> dict:
        """POST *payload*; map HTTP failures onto ProviderError for retry."""
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # The status code in the message is what retry_transient classifies
            # (429/5xx/529 retry; 401/422 never do).
            raise ProviderError(f"jev HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(f"jev temporarily unavailable: {exc}") from exc
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderError("jev returned malformed JSON") from exc
        if not isinstance(body, dict):
            raise ProviderError("jev returned malformed JSON (not an object)")
        return body

    def _parse(self, body: dict, questions: list, latency_ms: float) -> DecisionBatch:
        """Validate every answer in *body* against its question."""
        if isinstance(body.get("result"), dict):  # Cloudflare REST envelope
            body = body["result"]
        raw_answers = body.get("answers")
        if not isinstance(raw_answers, dict):
            raise DecisionError("jev response has no answers map")
        answers = {}
        for question in questions:
            raw = raw_answers.get(question.id)
            if not isinstance(raw, dict):
                raise DecisionError(f"jev response has no answer for {question.id!r}")
            answers[question.id] = validate_answer(
                question, raw.get("choice"), raw.get("probabilities"),
                raw.get("confidence"))
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        input_tokens = _count(usage.get("input_tokens"))
        output_tokens = _count(usage.get("output_tokens"))
        return DecisionBatch(
            answers=answers, backend=self.name,
            model=str(body.get("model") or self.model),
            latency_ms=latency_ms,
            cost_usd=input_tokens * USD_PER_INPUT_TOKEN,
            prompt_tokens=input_tokens, completion_tokens=output_tokens,
            tokens=input_tokens + output_tokens,
        )


def _to_jev_question(question) -> dict:
    """Render a :class:`Question` as a Jev ``choice`` question."""
    return {
        "type": "choice",
        "instructions": question.instructions,
        "criteria": {opt: question.criteria.get(opt) for opt in question.options},
    }


def _count(value: object) -> int:
    """Return a non-negative token count, 0 for anything malformed."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
