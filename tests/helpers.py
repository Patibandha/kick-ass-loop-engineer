"""Shared test fakes for the 2.0 pipeline."""
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from kickass_loop_engineer.guardrails import VerificationResult
from kickass_loop_engineer.providers.base import Provider, ProviderResult


class FakeProvider(Provider):
    """Returns queued canned responses; records prompts for assertions."""

    name = "fake"

    def __init__(
        self,
        responses,
        model="fake-model",
        cost_usd=0.0,
        tokens=10,
        prompt_tokens=0,
        completion_tokens=0,
    ):
        self.responses = list(responses)
        self.calls = []
        self.model = model
        self.cost_usd = cost_usd
        self.tokens = tokens
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens

    def complete(self, system, user):
        self.calls.append((system, user))
        text = self.responses.pop(0) if self.responses else ""
        return ProviderResult(
            text=text,
            tokens=self.tokens,
            cost_usd=self.cost_usd,
            model=self.model,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
        )


class FakeVisionProvider(Provider):
    """A vision-capable fake: ``complete`` accepts ``images`` and records calls."""

    name = "fake-vision"

    def __init__(self, text="NO FINDINGS", model="fake-vlm", cost_usd=0.0):
        self.calls = []
        self.text = text
        self.model = model
        self.cost_usd = cost_usd

    def complete(self, system, user, images=()):
        self.calls.append((system, user, tuple(images)))
        return ProviderResult(text=self.text, tokens=5, cost_usd=self.cost_usd,
                              model=self.model)


class FakePolicy:
    """Records every command run; returns a configurable VerificationResult."""

    def __init__(self, passed=True, error=""):
        self.runs = []
        self.passed = passed
        self.error = error

    def run(self, command, cwd):
        self.runs.append((command, cwd))
        return VerificationResult(passed=self.passed, command=command,
                                  error=self.error)


class _CannedJSONHandler(BaseHTTPRequestHandler):
    """Replies to any POST with the server's canned (status, body) pair."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        status, body = self.server.canned
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    log_message = lambda *a: None


class StubServerTestCase(unittest.TestCase):
    """Base for HTTP-provider tests: runs a canned-JSON stub server.

    Subclasses call ``self.set_canned(body)`` and point the provider under
    test at ``self.base_url``.
    """

    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _CannedJSONHandler)
        self.server.canned = (200, "{}")
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def set_canned(self, body: str, status: int = 200) -> None:
        """Set the JSON body (and status) the stub returns to the next request."""
        self.server.canned = (status, body)


class FakeDecisionBackend:
    """Scripted decision backend: answers from a ``{question_id: choice}`` map.

    ``confidence`` applies to every answer; a question absent from ``choices``
    answers with its default. ``error`` (a DecisionError) is raised instead
    when set. Every ``(state, question ids)`` call is recorded.
    """

    name = "fake-decider"

    def __init__(self, choices=None, confidence=0.95, error=None, cost_usd=0.0):
        self.choices = dict(choices or {})
        self.confidence = confidence
        self.error = error
        self.cost_usd = cost_usd
        self.calls = []

    def decide(self, state, questions):
        from kickass_loop_engineer.decision.base import Answer, DecisionBatch
        self.calls.append((state, [q.id for q in questions]))
        if self.error is not None:
            raise self.error
        answers = {}
        for q in questions:
            choice = self.choices.get(q.id, q.default)
            probs = {o: (0.9 if o == choice else 0.1 / max(len(q.options) - 1, 1))
                     for o in q.options}
            if len(q.options) == 1:
                probs = {choice: 1.0}
            answers[q.id] = Answer(choice=choice, probabilities=probs,
                                   confidence=self.confidence)
        return DecisionBatch(answers=answers, backend=self.name, model="fake-jev",
                             cost_usd=self.cost_usd, prompt_tokens=100)
