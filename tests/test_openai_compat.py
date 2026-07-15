"""Tests for the OpenAI-compatible provider against a local stub server."""
import base64
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from kickass_loop_engineer.providers import PROVIDERS, build_provider
from kickass_loop_engineer.providers.base import ProviderError, ProviderResult, as_int
from kickass_loop_engineer.providers.openai_compat import OpenAICompatProvider

_TEST_KEY_ENV = "LOOP_ENGINEER_TEST_OPENAI_KEY"

_HAPPY_BODY = json.dumps({
    "choices": [{"message": {"role": "assistant", "content": "hello from stub"}}],
    "usage": {"prompt_tokens": 7, "completion_tokens": 5},
})


class _StubHandler(BaseHTTPRequestHandler):
    """Records each request on the server and replies with canned JSON."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        self.server.requests.append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": json.loads(raw.decode("utf-8")),
        })
        status, body = self.server.canned
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    log_message = lambda *a: None


class OpenAICompatStubTests(unittest.TestCase):
    """Provider behaviour verified end to end against a stdlib stub server."""

    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self.server.requests = []
        self.server.canned = (200, _HAPPY_BODY)
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

    def make_provider(self, **overrides):
        kwargs = {
            "model": "test-model",
            "base_url": self.base_url,
            "api_key_env": _TEST_KEY_ENV,
            "timeout_seconds": 10.0,
        }
        kwargs.update(overrides)
        return OpenAICompatProvider(**kwargs)

    def complete_without_key(self, provider, system="sys prompt", user="user prompt"):
        env = {k: v for k, v in os.environ.items() if k != _TEST_KEY_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            return provider.complete(system, user)

    def test_happy_path_result_fields(self):
        result = self.complete_without_key(self.make_provider())
        self.assertEqual(result.text, "hello from stub")
        self.assertEqual(result.prompt_tokens, 7)
        self.assertEqual(result.completion_tokens, 5)
        self.assertEqual(result.tokens, 12)
        self.assertEqual(result.model, "test-model")
        self.assertEqual(result.cost_usd, 0.0)

    def test_happy_path_request_shape(self):
        self.complete_without_key(self.make_provider())
        request = self.server.requests[0]
        self.assertEqual(request["path"], "/chat/completions")
        body = request["body"]
        self.assertIs(body["stream"], False)
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["messages"], [
            {"role": "system", "content": "sys prompt"},
            {"role": "user", "content": "user prompt"},
        ])
        self.assertNotIn("temperature", body)
        self.assertNotIn("max_tokens", body)

    def test_temperature_present_when_configured(self):
        self.complete_without_key(self.make_provider(temperature=0.4))
        self.assertEqual(self.server.requests[0]["body"]["temperature"], 0.4)

    def test_max_tokens_present_when_positive(self):
        self.complete_without_key(self.make_provider(max_tokens=256))
        self.assertEqual(self.server.requests[0]["body"]["max_tokens"], 256)

    def test_auth_header_sent_when_key_env_set(self):
        provider = self.make_provider()
        with mock.patch.dict(os.environ, {_TEST_KEY_ENV: "sk-test-123"}):
            provider.complete("s", "u")
        headers = self.server.requests[0]["headers"]
        self.assertEqual(headers.get("Authorization"), "Bearer sk-test-123")

    def test_no_auth_header_and_success_when_key_env_unset(self):
        result = self.complete_without_key(self.make_provider())
        headers = self.server.requests[0]["headers"]
        self.assertNotIn("Authorization", headers)
        self.assertEqual(result.text, "hello from stub")

    def test_http_500_raises_provider_error_with_code(self):
        self.server.canned = (500, json.dumps({"error": "boom"}))
        with self.assertRaises(ProviderError) as ctx:
            self.complete_without_key(self.make_provider())
        self.assertIn("500", str(ctx.exception))

    def test_malformed_json_raises_provider_error(self):
        self.server.canned = (200, "this is not json {")
        with self.assertRaises(ProviderError):
            self.complete_without_key(self.make_provider())

    def test_empty_choices_raises_provider_error_mentioning_choices(self):
        self.server.canned = (200, json.dumps({"choices": [], "usage": {}}))
        with self.assertRaises(ProviderError) as ctx:
            self.complete_without_key(self.make_provider())
        self.assertIn("choices", str(ctx.exception))

    def test_null_usage_counts_yield_zero_tokens_without_raising(self):
        self.server.canned = (200, json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "x"}}],
            "usage": {"prompt_tokens": None, "completion_tokens": None},
        }))
        result = self.complete_without_key(self.make_provider())
        self.assertEqual(result.prompt_tokens, 0)
        self.assertEqual(result.completion_tokens, 0)
        self.assertEqual(result.tokens, 0)

    def write_image(self, name, payload=b"\x89PNG fake image bytes"):
        path = Path(self.tmp.name) / name
        path.write_bytes(payload)
        return str(path), base64.b64encode(payload).decode("ascii")

    @property
    def tmp(self):
        if not hasattr(self, "_tmp"):
            self._tmp = tempfile.TemporaryDirectory()
            self.addCleanup(self._tmp.cleanup)
        return self._tmp

    def test_images_turn_user_content_into_content_array(self):
        path, encoded = self.write_image("shot.png")
        provider = self.make_provider()
        env = {k: v for k, v in os.environ.items() if k != _TEST_KEY_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            provider.complete("sys prompt", "user prompt", images=(path,))
        messages = self.server.requests[0]["body"]["messages"]
        self.assertEqual(messages[0], {"role": "system", "content": "sys prompt"})
        self.assertEqual(messages[1], {
            "role": "user",
            "content": [
                {"type": "text", "text": "user prompt"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{encoded}"},
                },
            ],
        })

    def test_multiple_images_yield_one_part_each_in_order(self):
        path_a, encoded_a = self.write_image("a.png", b"first")
        path_b, encoded_b = self.write_image("b.jpg", b"second")
        provider = self.make_provider()
        env = {k: v for k, v in os.environ.items() if k != _TEST_KEY_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            provider.complete("s", "u", images=(path_a, path_b))
        content = self.server.requests[0]["body"]["messages"][1]["content"]
        self.assertEqual(len(content), 3)
        self.assertEqual(
            content[1]["image_url"]["url"], f"data:image/png;base64,{encoded_a}"
        )
        self.assertEqual(
            content[2]["image_url"]["url"], f"data:image/jpeg;base64,{encoded_b}"
        )

    def test_mime_type_derived_from_extension(self):
        cases = [
            ("shot.png", "image/png"),
            ("shot.jpg", "image/jpeg"),
            ("shot.jpeg", "image/jpeg"),
            ("shot.PNG", "image/png"),
            ("shot.webp", "image/png"),
            ("shot", "image/png"),
        ]
        provider = self.make_provider()
        env = {k: v for k, v in os.environ.items() if k != _TEST_KEY_ENV}
        for name, mime in cases:
            with self.subTest(name=name):
                path, encoded = self.write_image(name)
                with mock.patch.dict(os.environ, env, clear=True):
                    provider.complete("s", "u", images=(path,))
                content = self.server.requests[-1]["body"]["messages"][1]["content"]
                self.assertEqual(
                    content[1]["image_url"]["url"],
                    f"data:{mime};base64,{encoded}",
                )

    def test_explicit_empty_images_keeps_text_only_body_byte_identical(self):
        provider = self.make_provider()
        env = {k: v for k, v in os.environ.items() if k != _TEST_KEY_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            provider.complete("sys prompt", "user prompt", images=())
            provider.complete("sys prompt", "user prompt")
        self.assertEqual(
            self.server.requests[0]["body"], self.server.requests[1]["body"]
        )
        self.assertEqual(self.server.requests[0]["body"]["messages"], [
            {"role": "system", "content": "sys prompt"},
            {"role": "user", "content": "user prompt"},
        ])

    def test_unreadable_image_path_raises_provider_error_before_any_request(self):
        provider = self.make_provider()
        missing = str(Path(self.tmp.name) / "does-not-exist.png")
        env = {k: v for k, v in os.environ.items() if k != _TEST_KEY_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ProviderError) as ctx:
                provider.complete("s", "u", images=(missing,))
        self.assertIn("does-not-exist.png", str(ctx.exception))
        self.assertEqual(self.server.requests, [])

    def test_registry_builds_openai_compat_provider(self):
        provider = build_provider("openai_compat", model="m", base_url=self.base_url)
        self.assertIsInstance(provider, OpenAICompatProvider)
        self.assertIn("openai_compat", PROVIDERS)


class ProviderResultUsageFieldTests(unittest.TestCase):
    def test_usage_split_fields_default_to_zero(self):
        result = ProviderResult(text="x")
        self.assertEqual(result.prompt_tokens, 0)
        self.assertEqual(result.completion_tokens, 0)


class AsIntTests(unittest.TestCase):
    """The shared usage-count coercion helper never raises."""

    def test_coerces_usage_count_edge_cases_to_safe_ints(self):
        cases = [(7, 7), ("7", 7), (None, 0), ("", 0), ("junk", 0), ({}, 0), (3.9, 3)]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(as_int(value), expected)


if __name__ == "__main__":
    unittest.main()
