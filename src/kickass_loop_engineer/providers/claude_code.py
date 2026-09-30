"""Claude Code provider running in headless (``claude -p``) mode.

This provider wears TWO hats, and the caller picks one per call:

* :meth:`ClaudeCodeProvider.complete` — the prose/dispatcher path. The CLI runs
  with a READ-ONLY tool set (Read/Glob/Grep) and answers in text, which is what
  the decompose, architect, and review roles consume. A reviewer or decomposer
  must never edit files, so the read-only default is a contract, not a
  convenience.
* :meth:`ClaudeCodeProvider.edit` — the agentic pipeline-builder path
  (``edits_in_place = True``). The CLI gets edit-capable tools and a working
  directory, drives its own Read/Edit/Write/Bash loop inside it, and returns a
  prose summary; the engine then harvests whatever files changed in that
  directory and applies the write guardrails to them. Whole-file
  ``=== FILE: ... ===`` blocks are never involved.

Because the subprocess edits files relative to its working directory, ``cwd``
pins the invocation to a target workspace: the constructor's ``cwd`` for
``complete``, the required ``cwd`` argument for ``edit``. When
``force_subscription`` is set, the Anthropic API key is stripped from the
subprocess environment so usage draws from the subscription's Agent SDK credit
instead of metered API billing. The reported ``total_cost_usd`` is surfaced so
the engine can budget against it.

Both paths run through :func:`~.base.retry_transient`, so an overloaded API
(HTTP 529), a rate limit, or a timeout is retried with exponential backoff
instead of failing the round.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Optional, Sequence

from .base import (
    Provider,
    ProviderError,
    ProviderResult,
    as_int,
    retry_transient,
)

#: Prose/dispatcher tools. Deliberately read-only: ``complete`` serves the
#: decompose/architect/review roles, and a reviewer that can edit the code it
#: reviews is not a reviewer.
DEFAULT_PROSE_TOOLS = ("Read", "Glob", "Grep")

#: Agentic builder tools for :meth:`ClaudeCodeProvider.edit`. Bash is included
#: because the brief asks the builder to run the project's test/lint commands
#: before it finishes.
DEFAULT_EDIT_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep", "Bash")


class ClaudeCodeProvider(Provider):
    """Provider backed by the Claude Code CLI in non-interactive mode.

    Both a prose provider and an in-place pipeline builder:

    * ``complete(system, user)`` runs with ``allowed_tools`` — read-only by
      default — and returns text for roles that consume prose.
    * ``edit(system, user, cwd=...)`` runs with ``edit_tools`` inside *cwd*,
      lets the CLI apply its own edits there, and returns its summary. The
      engine harvests the changed files afterwards, so no FILE-block parsing
      is involved and the write guardrails still bound the blast radius.
    """

    name = "claude_code"
    edits_in_place = True

    def __init__(
        self,
        model: Optional[str] = None,
        allowed_tools: Sequence[str] = DEFAULT_PROSE_TOOLS,
        permission_mode: str = "acceptEdits",
        timeout_seconds: float = 600.0,
        force_subscription: bool = True,
        binary: str = "claude",
        cwd: Optional[str] = None,
        edit_tools: Sequence[str] = DEFAULT_EDIT_TOOLS,
        edit_permission_mode: str = "acceptEdits",
        edit_timeout_seconds: float = 1800.0,
        retry_attempts: int = 3,
        retry_base_delay: float = 2.0,
    ) -> None:
        """Initialize the Claude Code provider.

        Args:
            model: Optional model override; None uses the CLI default.
            allowed_tools: Tools permitted without interactive approval on the
                PROSE path (:meth:`complete`). Defaults to the read-only set
                ``("Read", "Glob", "Grep")`` — a deliberate contract: the
                roles that call ``complete`` (decompose, architect, review)
                must never edit files. Pass an edit-capable set explicitly if
                some other role genuinely needs one.
            permission_mode: Value passed to ``--permission-mode`` on the prose path.
            timeout_seconds: Per-invocation wall-clock cap for :meth:`complete`.
            force_subscription: Strip the API key so billing uses subscription credit.
            binary: Name or path of the Claude Code executable.
            cwd: Working directory for :meth:`complete` so any file access
                lands in the intended workspace; None inherits the caller's
                cwd. :meth:`edit` ignores it — it takes its directory as a
                required argument.
            edit_tools: Tools permitted on the AGENTIC path (:meth:`edit`).
            edit_permission_mode: ``--permission-mode`` for the agentic path.
            edit_timeout_seconds: Wall-clock cap for one agentic edit turn.
                Generous by default (30 min): an in-place builder reads the
                repo, edits several files, and runs the test suite in a
                single turn.
            retry_attempts: Total invocations allowed per call when the
                failure is transient (see :func:`~.base.is_transient_error`).
            retry_base_delay: Seconds before the first retry; doubled each time.
        """
        self.model = model
        self.allowed_tools = tuple(allowed_tools)
        self.permission_mode = permission_mode
        self.timeout_seconds = timeout_seconds
        self.force_subscription = force_subscription
        self.binary = binary
        self.cwd = cwd
        self.edit_tools = tuple(edit_tools)
        self.edit_permission_mode = edit_permission_mode
        self.edit_timeout_seconds = edit_timeout_seconds
        self.retry_attempts = retry_attempts
        self.retry_base_delay = retry_base_delay

    def complete(self, system: str, user: str) -> ProviderResult:
        """Run one headless, read-only Claude Code invocation and return the result.

        Args:
            system: Role/system instructions for the model.
            user: The concrete request or context.

        Returns:
            A ``ProviderResult`` with the CLI's prose answer and its usage.

        Raises:
            ProviderError: When the CLI fails and the failure is permanent, or
                when every retry of a transient failure is exhausted.
        """
        command = [self.binary, "-p", "--output-format", "json"]
        if self.allowed_tools:
            command += ["--allowedTools", ",".join(self.allowed_tools)]
        if self.permission_mode:
            command += ["--permission-mode", self.permission_mode]
        if self.model:
            command += ["--model", self.model]
        return self._run_with_retry(command, prompt=self._prompt(system, user),
                                    cwd=self.cwd,
                                    timeout=self.timeout_seconds)

    def edit(self, system: str, user: str, *, cwd: str) -> ProviderResult:
        """Let Claude Code edit the workspace at *cwd* and return its summary.

        The CLI applies its own edits inside *cwd*; nothing in the returned
        text is parsed as files. The engine diffs the directory afterwards
        (``Workspace.harvest``) and enforces the write policy on whatever
        actually changed.

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
            raise ProviderError(
                f"claude_code edit requires an existing cwd: {cwd!r}")
        command = [self.binary, "-p", "--output-format", "json",
                   "--allowedTools", ",".join(self.edit_tools),
                   "--permission-mode", self.edit_permission_mode]
        if self.model:
            command += ["--model", self.model]
        return self._run_with_retry(command, prompt=self._prompt(system, user),
                                    cwd=cwd,
                                    timeout=self.edit_timeout_seconds)

    @staticmethod
    def _prompt(system: str, user: str) -> str:
        """Join the system and user prompts into the text written to stdin."""
        return f"{system}\n\n{user}" if system else user

    def _run_with_retry(self, command: list, *, prompt: str,
                        cwd: Optional[str], timeout: float) -> ProviderResult:
        """Run *command*, retrying transient failures with exponential backoff."""
        return retry_transient(
            lambda: self._run(command, prompt=prompt, cwd=cwd, timeout=timeout),
            attempts=self.retry_attempts,
            base_delay=self.retry_base_delay,
        )

    def _run(self, command: list, *, prompt: str, cwd: Optional[str],
             timeout: float) -> ProviderResult:
        """Execute one CLI invocation and parse its result.

        The single subprocess path shared by :meth:`complete` and
        :meth:`edit`; they differ only in argv, working directory, and
        timeout.

        Args:
            command: Fully built argv for the Claude Code CLI, carrying flags
                only. The prompt is never an argv element: Windows caps a
                command line at 32767 characters and an ``enhance``-mode brief
                carries the repo map, so argv would raise WinError 206 — which
                Python reports as ``FileNotFoundError``, indistinguishable from
                a missing binary.
            prompt: The prompt text, written to the CLI's stdin.
            cwd: Working directory for the subprocess (None inherits).
            timeout: Wall-clock cap in seconds.

        Returns:
            The parsed ``ProviderResult``.

        Raises:
            ProviderError: On a missing binary, a missing working directory, a
                timeout, an OS-level failure, a non-zero exit, or a CLI-
                reported error. Messages carry the CLI's own text so the
                shared transient classifier can see an overload or rate limit.
        """
        # Decision-model keys are never the builder's business.
        env = {k: v for k, v in os.environ.items()
               if k not in ("TYPESAFE_API_KEY", "CLOUDFLARE_API_TOKEN")}
        if self.force_subscription:
            for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
                env.pop(key, None)

        try:
            completed = subprocess.run(
                command,
                input=prompt,
                env=env,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            # A missing WORKING DIRECTORY raises the same exception type as a
            # missing binary; ``exc.filename`` names the true culprit.
            if cwd and exc.filename == cwd:
                raise ProviderError(
                    f"claude_code cwd does not exist: {cwd!r}") from exc
            raise ProviderError(f"claude binary not found: {self.binary!r}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"claude timed out after {timeout:.0f}s") from exc
        except OSError as exc:
            raise ProviderError(f"claude subprocess error: {exc}") from exc

        if completed.returncode != 0:
            detail = (completed.stderr or "").strip() or (completed.stdout or "").strip()
            raise ProviderError(detail or "claude non-zero exit")

        return self._parse(completed.stdout)

    def _parse(self, stdout: str) -> ProviderResult:
        """Parse Claude Code JSON output, falling back to raw text."""
        raw = (stdout or "").strip()
        if not raw:
            raise ProviderError("empty response from claude")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return ProviderResult(text=raw, model=self.model or self.name)

        if data.get("is_error"):
            raise ProviderError(str(data.get("result", "claude reported an error")))

        usage = data.get("usage") or {}
        prompt_tokens = as_int(usage.get("input_tokens"))
        completion_tokens = as_int(usage.get("output_tokens"))
        cost = data.get("total_cost_usd")
        return ProviderResult(
            text=data.get("result", ""),
            tokens=prompt_tokens + completion_tokens,
            cost_usd=float(cost) if isinstance(cost, (int, float)) else 0.0,
            model=self.model or self.name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
