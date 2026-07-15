"""Claude Code provider running in headless (``claude -p``) mode.

This is the frontier/dispatcher provider, not a pipeline builder. Claude Code
drives its own tools (Read/Edit/Bash) inside the subprocess and returns a prose
summary of what it did — it does NOT emit the FILE-block format the engine's
build pipeline parses, so wiring it as the pipeline builder yields rounds that
edit nothing. Use it for dispatcher/arbiter roles that consume prose, and keep
FILE-block-emitting models (e.g. the Ollama provider) as pipeline builders.

Delegates a prompt to Claude Code as a subprocess and parses its JSON result.
Because the subprocess edits files relative to its working directory, ``cwd``
pins the invocation to a target workspace; in M1 it is config-only (auto-
defaulting to the run workspace is deliberately deferred). When
``force_subscription`` is set, the Anthropic API key is stripped from the
subprocess environment so usage draws from the subscription's Agent SDK credit
instead of metered API billing. The reported ``total_cost_usd`` is surfaced so
the engine can budget against it.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Optional, Sequence

from .base import Provider, ProviderError, ProviderResult, as_int


class ClaudeCodeProvider(Provider):
    """Provider backed by the Claude Code CLI in non-interactive mode.

    Frontier/dispatcher provider: the CLI applies edits itself via its own
    tools and replies with a prose summary rather than FILE blocks, so this
    provider is not suitable as a pipeline builder — the build pipeline would
    find no parseable files in its output. It fits roles that consume prose
    (dispatcher, arbiter, review commentary).
    """

    name = "claude_code"

    def __init__(
        self,
        model: Optional[str] = None,
        allowed_tools: Sequence[str] = ("Read", "Edit", "Bash"),
        permission_mode: str = "acceptEdits",
        timeout_seconds: float = 600.0,
        force_subscription: bool = True,
        binary: str = "claude",
        cwd: Optional[str] = None,
    ) -> None:
        """Initialize the Claude Code provider.

        Args:
            model: Optional model override; None uses the CLI default.
            allowed_tools: Tools permitted without interactive approval.
            permission_mode: Value passed to ``--permission-mode``.
            timeout_seconds: Per-invocation wall-clock cap.
            force_subscription: Strip the API key so billing uses subscription credit.
            binary: Name or path of the Claude Code executable.
            cwd: Working directory for the subprocess so its file edits land in
                the intended workspace; None inherits the caller's cwd. M1:
                config-only (auto-defaulting to the run workspace is deferred).
        """
        self.model = model
        self.allowed_tools = tuple(allowed_tools)
        self.permission_mode = permission_mode
        self.timeout_seconds = timeout_seconds
        self.force_subscription = force_subscription
        self.binary = binary
        self.cwd = cwd

    def complete(self, system: str, user: str) -> ProviderResult:
        """Run one headless Claude Code invocation and return the result."""
        prompt = f"{system}\n\n{user}" if system else user
        command = [self.binary, "-p", prompt, "--output-format", "json"]
        if self.allowed_tools:
            command += ["--allowedTools", ",".join(self.allowed_tools)]
        if self.permission_mode:
            command += ["--permission-mode", self.permission_mode]
        if self.model:
            command += ["--model", self.model]

        env = dict(os.environ)
        if self.force_subscription:
            for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
                env.pop(key, None)

        try:
            completed = subprocess.run(
                command,
                env=env,
                cwd=self.cwd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            # A missing WORKING DIRECTORY raises the same exception type as a
            # missing binary; ``exc.filename`` names the true culprit.
            if self.cwd and exc.filename == self.cwd:
                raise ProviderError(
                    f"claude_code cwd does not exist: {self.cwd!r}") from exc
            raise ProviderError(f"claude binary not found: {self.binary!r}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"claude timed out after {self.timeout_seconds:.0f}s") from exc
        except OSError as exc:
            raise ProviderError(f"claude subprocess error: {exc}") from exc

        if completed.returncode != 0:
            raise ProviderError((completed.stderr or "").strip() or "claude non-zero exit")

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
