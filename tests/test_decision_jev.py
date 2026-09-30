"""Jev HTTP backend against a scripted local stub server (no network)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from kickass_loop_engineer.decision.base import DecisionError, Question
from kickass_loop_engineer.decision.jev import USD_PER_INPUT_TOKEN, JevBackend

QUESTIONS = [Question("qa", "run qa?", ("run", "skip"), "run"),
             Question("risk", "risk?", ("low", "high"), "high")]
GOOD = {"model": "jev-1.13.0", "usage": {"input_tokens": 1000, "output_tokens": 30},
        "answers": {
            "qa": {"type": "choice", "choice": "skip",
                   "probabilities": {"run": 0.2, "skip": 0.8}, "confidence": 0.7},
            "risk": {"type": "choice", "choice": "low",
                     "probabilities": {"low": 0.9, "high": 0.1}, "confidence": 0.85}}}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", 0))
        self.server.requests.append({
            "path": self.path, "auth": self.headers.get("Authorization"),
            "body": json.loads(self.rfile.read(length) or b"{}")})
        status, body = self.server.script.pop(0) if self.server.script else (200, GOOD)
        payload = body if isinstance(body, str) else json.dumps(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload.encode("utf-8"))

    log_message = lambda *a: None


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.requests, srv.script = [], []
    thread = threading.Thread(target=srv.serve_forever,
                              kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


@pytest.fixture
def backend(server, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    return JevBackend(base_url=f"http://127.0.0.1:{server.server_address[1]}",
                      timeout_s=2, retries=1, sleep=lambda s: None)


def test_should_send_one_batched_choice_request(backend, server):
    backend.decide({"objective": "docs"}, QUESTIONS)
    (req,) = server.requests
    assert req["path"] == "/v1/systemone" and req["auth"] == "Bearer test-key"
    assert req["body"]["model"] == "jev-1.13.0"
    assert req["body"]["questions"]["qa"] == {
        "type": "choice", "instructions": "run qa?",
        "criteria": {"run": None, "skip": None}}


def test_should_parse_answers_and_price_input_tokens(backend):
    batch = backend.decide({}, QUESTIONS)
    assert batch.answers["qa"].choice == "skip"
    assert batch.answers["risk"].confidence == 0.85
    assert batch.cost_usd == pytest.approx(1000 * USD_PER_INPUT_TOKEN)
    assert batch.backend == "jev" and batch.model == "jev-1.13.0"


def test_should_fail_without_api_key(backend, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY")
    with pytest.raises(DecisionError, match="TYPESAFE_API_KEY"):
        backend.decide({}, QUESTIONS)


def test_should_retry_overload_then_succeed(backend, server):
    server.script = [(529, {"error": "overloaded"}), (200, GOOD)]
    assert backend.decide({}, QUESTIONS).answers["qa"].choice == "skip"
    assert len(server.requests) == 2


def test_should_not_retry_auth_failure(backend, server):
    server.script = [(401, {"error": "bad key"})]
    with pytest.raises(DecisionError):
        backend.decide({}, QUESTIONS)
    assert len(server.requests) == 1


@pytest.mark.parametrize("body", [
    "not json",
    {"answers": {}},
    {"answers": {"qa": {"choice": "maybe", "probabilities": {"run": 1.0},
                        "confidence": 0.9}, "risk": GOOD["answers"]["risk"]}},
])
def test_should_reject_malformed_responses(backend, server, body):
    server.script = [(200, body)]
    with pytest.raises(DecisionError):
        backend.decide({}, QUESTIONS)


def test_should_fail_when_unreachable(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    jev = JevBackend(base_url="http://127.0.0.1:9", timeout_s=0.5, retries=0)
    with pytest.raises(DecisionError):
        jev.decide({}, QUESTIONS)


@pytest.fixture
def cloudflare(server, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct123")
    return JevBackend(route="cloudflare",
                      base_url=f"http://127.0.0.1:{server.server_address[1]}",
                      timeout_s=2, retries=0)


def test_cloudflare_route_wraps_input_and_unwraps_result(cloudflare, server):
    server.script = [(200, {"success": True, "result": GOOD})]
    batch = cloudflare.decide({"objective": "docs"}, QUESTIONS)
    (req,) = server.requests
    assert req["path"] == "/accounts/acct123/ai/run"
    assert req["auth"] == "Bearer cf-token"
    assert req["body"]["model"] == "typesafe/jev"
    assert set(req["body"]["input"]) == {"state", "questions"}
    assert batch.answers["qa"].choice == "skip"


def test_cloudflare_route_needs_an_account_id(cloudflare, monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID")
    with pytest.raises(DecisionError, match="CLOUDFLARE_ACCOUNT_ID"):
        cloudflare.decide({}, QUESTIONS)


def test_unknown_route_is_rejected():
    with pytest.raises(ValueError):
        JevBackend(route="carrier-pigeon")
