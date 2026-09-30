"""Transient-failure classification and the shared exponential-backoff retry.

Every case is offline and clock-free: the retry helper takes an injected
``sleep`` so the recorded delays are asserted without the suite ever waiting.
"""
import unittest

from kickass_loop_engineer.providers.base import (
    ProviderError,
    ProviderResult,
    is_transient_error,
    retry_transient,
)

#: ``(message, expected)`` pairs pinning the transient/permanent boundary.
_TRUTH_TABLE = (
    # Overload and rate limiting — retrying is exactly the right response.
    ("anthropic HTTP 529: overloaded_error", True),
    ("HTTP 529", True),
    ("overloaded_error", True),
    ("Overloaded", True),
    ("openai_compat HTTP 429: rate limit exceeded", True),
    ("rate-limit hit, slow down", True),
    ("ratelimit reached", True),
    ("Too Many Requests", True),
    ("openai_compat HTTP 500: internal server error", True),
    ("anthropic HTTP 502: bad gateway", True),
    ("HTTP 503 service unavailable", True),
    ("HTTP 504", True),
    ("HTTP 408 request timeout", True),
    ("HTTP 425 too early", True),
    ("claude timed out after 600s", True),
    ("model is temporarily unavailable", True),
    ("ollama request failed: <urlopen error [Errno 104] Connection reset by peer>", True),
    ("ollama request failed: <urlopen error [Errno 111] Connection refused>", True),
    # Permanent failures — a retry cannot change the answer.
    ("read 1529 bytes from the socket", False),
    ("wrote 1500 bytes", False),
    ("ANTHROPIC_API_KEY is not set", False),
    ("claude binary not found: 'claude'", False),
    ("openai_compat returned malformed JSON: Expecting value", False),
    ("openai_compat response carried no choices", False),
    ("agy is not authenticated and cannot run its browser sign-in", False),
    ("claude_code cwd does not exist: '/gone'", False),
    ("HTTP 404: model not found", False),
    ("HTTP 401: invalid api key", False),
)


class IsTransientErrorTests(unittest.TestCase):
    """The textual classification of a provider failure."""

    def test_truth_table(self):
        for message, expected in _TRUTH_TABLE:
            with self.subTest(message=message):
                self.assertEqual(is_transient_error(ProviderError(message)), expected)

    def test_classification_reads_the_message_not_the_exception_type(self):
        self.assertTrue(is_transient_error(RuntimeError("server error")))
        self.assertFalse(is_transient_error(RuntimeError("nope")))


class _Recorder:
    """Stands in for ``time.sleep``, recording the delays it was asked for."""

    def __init__(self):
        self.delays = []

    def __call__(self, seconds):
        self.delays.append(seconds)


class RetryTransientTests(unittest.TestCase):
    """Backoff, exhaustion, and the permanent-failure fast path."""

    def test_two_transient_failures_then_success_sleeps_2_then_4(self):
        sleeper = _Recorder()
        outcomes = [
            ProviderError("HTTP 529 overloaded"),
            ProviderError("HTTP 503 temporarily unavailable"),
            ProviderResult(text="ok"),
        ]
        calls = []

        def call():
            calls.append(1)
            item = outcomes.pop(0)
            if isinstance(item, ProviderError):
                raise item
            return item

        result = retry_transient(call, sleep=sleeper)
        self.assertEqual(result.text, "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeper.delays, [2.0, 4.0])

    def test_exhausted_attempts_reraise_the_last_error(self):
        sleeper = _Recorder()
        errors = [ProviderError(f"HTTP 529 overloaded #{i}") for i in range(3)]

        def call():
            raise errors.pop(0)

        with self.assertRaises(ProviderError) as ctx:
            retry_transient(call, sleep=sleeper)
        self.assertIn("#2", str(ctx.exception))
        self.assertEqual(sleeper.delays, [2.0, 4.0])

    def test_attempts_cap_bounds_the_number_of_calls(self):
        sleeper = _Recorder()
        calls = []

        def call():
            calls.append(1)
            raise ProviderError("HTTP 529 overloaded")

        with self.assertRaises(ProviderError):
            retry_transient(call, attempts=5, sleep=sleeper)
        self.assertEqual(len(calls), 5)
        self.assertEqual(sleeper.delays, [2.0, 4.0, 8.0, 16.0])

    def test_base_delay_scales_every_wait(self):
        sleeper = _Recorder()

        def call():
            raise ProviderError("HTTP 429 rate limit")

        with self.assertRaises(ProviderError):
            retry_transient(call, base_delay=0.5, sleep=sleeper)
        self.assertEqual(sleeper.delays, [0.5, 1.0])

    def test_non_transient_error_raises_immediately_without_sleeping(self):
        sleeper = _Recorder()
        calls = []

        def call():
            calls.append(1)
            raise ProviderError("ANTHROPIC_API_KEY is not set")

        with self.assertRaises(ProviderError) as ctx:
            retry_transient(call, sleep=sleeper)
        self.assertIn("ANTHROPIC_API_KEY", str(ctx.exception))
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeper.delays, [])

    def test_first_call_success_never_sleeps(self):
        sleeper = _Recorder()
        result = retry_transient(lambda: ProviderResult(text="fine"), sleep=sleeper)
        self.assertEqual(result.text, "fine")
        self.assertEqual(sleeper.delays, [])

    def test_attempts_of_one_disables_retrying(self):
        sleeper = _Recorder()
        calls = []

        def call():
            calls.append(1)
            raise ProviderError("HTTP 529 overloaded")

        with self.assertRaises(ProviderError):
            retry_transient(call, attempts=1, sleep=sleeper)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeper.delays, [])

    def test_on_retry_receives_the_zero_based_index_and_the_error(self):
        sleeper = _Recorder()
        seen = []
        outcomes = [ProviderError("HTTP 529 overloaded"), ProviderResult(text="ok")]

        def call():
            item = outcomes.pop(0)
            if isinstance(item, ProviderError):
                raise item
            return item

        retry_transient(call, sleep=sleeper,
                        on_retry=lambda index, exc: seen.append((index, str(exc))))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], 0)
        self.assertIn("overloaded", seen[0][1])

    def test_injected_predicate_overrides_classification(self):
        sleeper = _Recorder()
        calls = []

        def call():
            calls.append(1)
            raise ProviderError("this is normally permanent")

        with self.assertRaises(ProviderError):
            retry_transient(call, sleep=sleeper, is_transient=lambda exc: True)
        self.assertEqual(len(calls), 3)

    def test_non_provider_errors_are_never_retried(self):
        sleeper = _Recorder()
        calls = []

        def call():
            calls.append(1)
            raise ValueError("HTTP 529 overloaded")

        with self.assertRaises(ValueError):
            retry_transient(call, sleep=sleeper)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeper.delays, [])


if __name__ == "__main__":
    unittest.main()
