"""Gemini provider running headless through the Antigravity CLI (``agy -p``).

Like Claude Code, this provider is BOTH a prose provider and an in-place
pipeline builder. The CLI drives its own tools inside the subprocess and returns
a prose summary of what it did — it never emits FILE blocks — so the engine uses
it two ways: :meth:`GeminiCliProvider.complete` for roles that consume prose
(dispatcher, arbiter, review commentary), and :meth:`GeminiCliProvider.edit`
(``edits_in_place = True``) to let it edit an attempt's worktree directly, after
which the engine harvests whatever files changed and applies the write
guardrails to them.

Unlike Claude Code, ``agy`` exposes no tool or permission flags: the same
invocation serves both paths and only the working directory and the wall-clock
budget differ. Both run through :func:`~.base.retry_transient`, so an overloaded
backend, a rate limit, or a timeout is retried with exponential backoff.

The binary is ``agy`` (Antigravity CLI), the subscription path for Gemini after
the standalone Gemini CLI was retired for AI Ultra plans. Headless contract::

    agy -p "" --input-format stream-json --output-format stream-json

with ONE newline-delimited JSON message on stdin::

    {"event": "user", "message": {"role": "user", "content": "<prompt>"}}

The prompt never goes in argv. Windows caps a process command line at 32767
characters, and a brief in ``enhance`` mode — repo map plus the whole diff —
routinely exceeds it; as a ``-p`` value that raised WinError 206, which Python
surfaces as ``FileNotFoundError``, so the engine reported "agy binary not found"
while ``agy.exe`` sat on PATH working. ``agy`` has no bare-stdin mode (``-p ""``
alone is refused as an empty prompt), so the prompt rides the documented
``--input-format stream-json`` channel instead. The message is serialized with
``ensure_ascii`` so stdin is pure ASCII and no platform code page can mangle it.

stdout is then a stream of events — ``init``, ``step_update``…, and a terminal
``result`` whose ``result`` object carries ``conversation_id``, ``status``
(``"SUCCESS"`` or an error status), ``response`` (the prose), ``error``,
``duration_seconds``, ``num_turns`` and a ``usage`` block (``input_tokens``,
``output_tokens``, ``thinking_tokens``, ``cache_read_tokens``,
``total_tokens``) — the same object ``--output-format json`` prints bare. It is
decoded as UTF-8, which is what ``agy`` writes, rather than the Windows code
page ``text=True`` would otherwise pick. Because the subprocess edits files
relative to its working directory, ``cwd`` pins the invocation to a target
workspace. The CLI reports no dollar cost (usage draws on the subscription), so
spend is priced by the engine's pricing table from the reported token split.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Optional

from .base import (
    Provider,
    ProviderError,
    ProviderResult,
    as_int,
    retry_transient,
)

#: Prefixed to every :meth:`GeminiCliProvider.complete` prompt. Handed a long
#: brief, agy 1.1.15 would call a file tool instead of answering; its file tools
#: are sandboxed to agy's own scratch directory, so the call failed, and print
#: mode ENDED on it with an empty response. The same brief, told this, answered.
#: Not used by :meth:`~GeminiCliProvider.edit`, which exists so the model can act.
ANSWER_IN_TEXT = (
    "Answer directly in text. Do not call tools: they cannot see the caller's "
    "workspace, everything you need is in this message, and a run that ends on "
    "a tool call returns no answer at all."
)

#: Substrings that mark a failure as "the operator never signed in". ``agy``
#: cannot open its browser OAuth flow from a non-TTY (CI, cron, the loop
#: engine), so an unauthenticated run dies here instead of prompting.
_AUTH_MARKERS = (
    "authentication required",
    "authentication failed",
    "not authenticated",
    "unauthenticated",
    "not logged in",
    "please log in",
    "please sign in",
    "unauthorized",
    "no credentials",
    "run `agy`",
)

#: Appended to any authentication failure so the operator knows the one-time fix.
_AUTH_HINT = (
    "agy is not authenticated and cannot run its browser sign-in from a "
    "non-interactive shell; run `agy` interactively once to sign in, then retry"
)


class GeminiCliProvider(Provider):
    """Provider backed by the Antigravity CLI (``agy``) in non-interactive mode.

    Both a prose provider and an in-place pipeline builder:

    * ``complete(system, user)`` returns text for roles that consume prose
      (dispatcher, arbiter, review commentary).
    * ``edit(system, user, cwd=...)`` runs the same invocation inside *cwd*,
      lets the CLI apply its own edits there, and returns its summary. The
      engine harvests the changed files afterwards, so no FILE-block parsing is
      involved and the write guardrails still bound the blast radius.
    """

    name = "gemini"
    edits_in_place = True

    def __init__(
        self,
        model: Optional[str] = None,
        timeout_seconds: float = 600.0,
        force_subscription: bool = True,
        binary: str = "agy",
        cwd: Optional[str] = None,
        edit_timeout_seconds: float = 1800.0,
        retry_attempts: int = 3,
        retry_base_delay: float = 2.0,
    ) -> None:
        """Initialize the Gemini (Antigravity CLI) provider.

        Args:
            model: Label for the model behind the CLI, used for reporting and
                pricing lookups only. Unlike Claude Code's ``--model``, the
                verified ``agy`` headless contract exposes no model-selection
                flag (only ``-p`` and ``--output-format``); model choice lives in
                the CLI's own configuration. The value is therefore accepted and
                passed through to ``ProviderResult.model`` but NOT sent to the
                subprocess — set it to the model you configured in ``agy`` (e.g.
                ``"gemini-3-pro"``) so the pricing table can bind budget caps.
            timeout_seconds: Per-invocation wall-clock cap.
            force_subscription: Strip Gemini/Google API keys from the subprocess
                environment so usage draws on the subscription instead of
                metered API billing.
            binary: Name or path of the Antigravity executable.
            cwd: Working directory for :meth:`complete` so its file edits land
                in the intended workspace; None inherits the caller's cwd.
                :meth:`edit` ignores it — it takes its directory as a required
                argument.
            edit_timeout_seconds: Wall-clock cap for one agentic edit turn.
                Generous by default (30 min): an in-place builder reads the
                repo, edits several files, and runs the test suite in a single
                turn.
            retry_attempts: Total invocations allowed per call when the failure
                is transient (see :func:`~.base.is_transient_error`).
            retry_base_delay: Seconds before the first retry; doubled each time.
        """
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.force_subscription = force_subscription
        self.binary = binary
        self.cwd = cwd
        self.edit_timeout_seconds = edit_timeout_seconds
        self.retry_attempts = retry_attempts
        self.retry_base_delay = retry_base_delay

    def complete(self, system: str, user: str) -> ProviderResult:
        """Run one headless ``agy`` invocation and return the result.

        Args:
            system: Role/system instructions for the model.
            user: The concrete request or context.

        Returns:
            A ``ProviderResult`` with the CLI's prose answer and its usage.

        Raises:
            ProviderError: When the CLI fails permanently, when every retry
                of a transient failure is exhausted, or when the run reports
                SUCCESS with no answer text (see below).
        """
        directed = f"{ANSWER_IN_TEXT}\n\n{system}" if system else ANSWER_IN_TEXT
        result = self._run_with_retry(self._command(), self._stdin(directed, user),
                                      cwd=self.cwd, timeout=self.timeout_seconds)
        if not (result.text or "").strip():
            # Observed on agy 1.1.15: handed a long brief, the model called a
            # file tool instead of answering, the tool failed (agy's file tools
            # are sandboxed to its own scratch directory, not our cwd), and the
            # print-mode run ENDED on that tool call — status SUCCESS, response
            # "". Every caller of complete() consumes the prose itself (review,
            # arbiter, dispatcher); a reviewer handed "" finds zero findings and
            # the slice is promoted. A review that never happened must not read
            # as a clean one. Not transient, so deliberately outside the retry.
            raise ProviderError(
                "agy reported SUCCESS but returned no answer text — the run most "
                "likely ended on a tool call instead of a reply; treating it as a "
                "failure rather than as an empty (clean) answer")
        return result

    def edit(self, system: str, user: str, *, cwd: str) -> ProviderResult:
        """Let ``agy`` edit the workspace at *cwd* and return its summary.

        The CLI applies its own edits inside *cwd*; nothing in the returned
        text is parsed as files. The engine diffs the directory afterwards
        (``Workspace.harvest``) and enforces the write policy on whatever
        actually changed. ``agy`` exposes no tool-permission flags, so the
        argv is identical to :meth:`complete`'s — only the working directory
        and the wall-clock budget differ.

        Args:
            system: Role/system instructions for the model.
            user: The concrete request or context.
            cwd: Existing directory the CLI must treat as the workspace
                (in the pipeline: the attempt's git worktree).

        Returns:
            A ``ProviderResult`` whose text summarises the edits made.

        Raises:
            ProviderError: When *cwd* is missing or not a directory, when the
                CLI fails permanently, or when every retry is exhausted.
        """
        if not cwd or not os.path.isdir(cwd):
            raise ProviderError(f"gemini edit requires an existing cwd: {cwd!r}")
        return self._run_with_retry(self._command(), self._stdin(system, user),
                                    cwd=cwd, timeout=self.edit_timeout_seconds)

    def _command(self) -> list:
        """Build the headless argv for one ``agy`` invocation.

        It carries no prompt: the prompt goes on stdin (:meth:`_stdin`), because
        an argv element is bounded by Windows' 32767-character command line and
        a review brief is not. ``-p ""`` puts ``agy`` in print mode; with
        ``--input-format stream-json`` the empty value is not an empty prompt,
        it hands the turn to the stdin message.
        """
        return [self.binary, "-p", "", "--input-format", "stream-json",
                "--output-format", "stream-json"]

    @staticmethod
    def _stdin(system: str, user: str) -> str:
        """Return the one stream-json user message that carries the prompt.

        ``json.dumps`` escapes every non-ASCII character by default, so the line
        written to the pipe is pure ASCII and no Windows code page has anything
        to mis-encode — the model still receives the original text.
        """
        prompt = f"{system}\n\n{user}" if system else user
        message = {"event": "user", "message": {"role": "user", "content": prompt}}
        return json.dumps(message) + "\n"

    def _run_with_retry(self, command: list, stdin: str, *, cwd: Optional[str],
                        timeout: float) -> ProviderResult:
        """Run *command*, retrying transient failures with exponential backoff."""
        return retry_transient(
            lambda: self._run(command, stdin, cwd=cwd, timeout=timeout),
            attempts=self.retry_attempts,
            base_delay=self.retry_base_delay,
        )

    def _run(self, command: list, stdin: str, *, cwd: Optional[str],
             timeout: float) -> ProviderResult:
        """Execute one CLI invocation and parse its result.

        The single subprocess path shared by :meth:`complete` and :meth:`edit`;
        they differ only in working directory and timeout.

        Args:
            command: Fully built argv for the Antigravity CLI.
            stdin: The stream-json message carrying the prompt.
            cwd: Working directory for the subprocess (None inherits).
            timeout: Wall-clock cap in seconds.

        Returns:
            The parsed ``ProviderResult``.

        Raises:
            ProviderError: On a missing binary, a missing working directory, a
                timeout, an OS-level failure, a non-zero exit, or a CLI-
                reported error status.
        """
        # Decision-model keys are never the builder's business.
        env = {k: v for k, v in os.environ.items()
               if k not in ("TYPESAFE_API_KEY", "CLOUDFLARE_API_TOKEN")}
        if self.force_subscription:
            for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY"):
                env.pop(key, None)

        try:
            completed = subprocess.run(
                command,
                input=stdin,
                env=env,
                cwd=cwd,
                capture_output=True,
                text=True,
                # agy writes UTF-8. Left to `text=True` alone, Windows decodes
                # with its ANSI code page, which mangles or rejects any review
                # that quotes a non-ASCII character.
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            # A missing WORKING DIRECTORY raises the same exception type as a
            # missing binary; ``exc.filename`` names the true culprit.
            if cwd and exc.filename == cwd:
                raise ProviderError(f"gemini cwd does not exist: {cwd!r}") from exc
            raise ProviderError(f"agy binary not found: {self.binary!r}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"agy timed out after {timeout:.0f}s") from exc
        except OSError as exc:
            raise ProviderError(f"agy subprocess error: {exc}") from exc

        if completed.returncode != 0:
            detail = (completed.stderr or "").strip() or (completed.stdout or "").strip()
            raise ProviderError(self._describe_failure(detail) or "agy non-zero exit")

        return self._parse(completed.stdout)

    def _describe_failure(self, detail: str) -> str:
        """Return an operator-facing message for a failed run.

        Authentication failures get the interactive-sign-in hint appended, since
        no amount of retrying fixes them from a non-TTY.

        Args:
            detail: Raw stderr/response text reported by the CLI.

        Returns:
            The message to carry in the ``ProviderError``.
        """
        lowered = detail.lower()
        if any(marker in lowered for marker in _AUTH_MARKERS):
            return f"{_AUTH_HINT} (agy said: {detail})" if detail else _AUTH_HINT
        return detail

    @staticmethod
    def _result_from_stream(raw: str) -> Optional[dict]:
        """Return the terminal ``result`` object of a stream-json run.

        Returns ``None`` when *raw* is not a stream at all (no line carries an
        ``event`` key), so the caller can read it as a bare envelope instead.

        Raises:
            ProviderError: *raw* IS a stream but it ended without a ``result``
                event — the run was cut off, and a partial stream is not an
                answer.
        """
        is_stream = False
        result: Optional[dict] = None
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # agy interleaves plain `error: …` diagnostics with the events.
                continue
            if not isinstance(event, dict) or "event" not in event:
                continue
            is_stream = True
            if event.get("event") == "result" and isinstance(event.get("result"), dict):
                result = event["result"]
        if not is_stream:
            return None
        if result is None:
            raise ProviderError("agy stream ended with no result event")
        return result

    def _parse(self, stdout: str) -> ProviderResult:
        """Parse an ``agy`` run: a stream-json ``result``, a bare envelope, or text.

        The provider asks for a stream, so the ``result`` event is the normal
        case. A bare JSON envelope (``--output-format json``) is read the same
        way, and anything that is not JSON at all falls back to raw text.
        """
        raw = (stdout or "").strip()
        if not raw:
            raise ProviderError("empty response from agy")
        data = self._result_from_stream(raw)
        if data is None:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return ProviderResult(text=raw, model=self.model or self.name)

        status = str(data.get("status", "")).upper()
        if status != "SUCCESS":
            # A stream's result names the failure in `error`; the bare envelope
            # has only ever used `response`. Prefer whichever says something.
            detail = str(data.get("error") or data.get("response") or "").strip()
            message = self._describe_failure(detail)
            raise ProviderError(
                message or f"agy reported status {data.get('status')!r}")

        usage = data.get("usage") or {}
        prompt_tokens = as_int(usage.get("input_tokens"))
        # Thinking tokens are billed at the output rate and reported separately,
        # so they belong on the completion side of the split.
        completion_tokens = as_int(usage.get("output_tokens")) + as_int(
            usage.get("thinking_tokens"))
        # The CLI's own total is authoritative when present (it accounts for
        # cache reads the split does not expose); otherwise sum what we have.
        total = as_int(usage.get("total_tokens")) or prompt_tokens + completion_tokens
        return ProviderResult(
            text=data.get("response", ""),
            tokens=total,
            cost_usd=0.0,  # Subscription usage: agy reports no dollar cost.
            model=self.model or self.name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
