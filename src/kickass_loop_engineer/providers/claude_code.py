"""Claude Code provider running in headless (``claude -p``) mode.

Delegates a prompt to Claude Code as a subprocess and parses its JSON result.
When ``force_subscription`` is set, the Anthropic API key is stripped from the
subprocess environment so usage draws from the subscription's Agent SDK credit
instead of metered API billing. The reported ``total_cost_usd`` is surfaced so
the engine can budget against it.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Optional, Sequence

from .base import Provider, ProviderError, ProviderResult


class ClaudeCodeProvider(Provider):
    """Provider backed by the Claude Code CLI in non-interactive mode."""

    name = "claude_code"

    def __init__(
        self,
        model: Optional[str] = None,
        allowed_tools: Sequence[str] = ("Read", "Edit", "Bash"),
        permission_mode: str = "acceptEdits",
        timeout_seconds: float = 600.0,
        force_subscription: bool = True,
        binary: str = "claude",
    ) -> None:
        """Initialize the Claude Code provider.

        Args:
            model: Optional model override; None uses the CLI default.
            allowed_tools: Tools permitted without interactive approval.
            permission_mode: Value passed to ``--permission-mode``.
            timeout_seconds: Per-invocation wall-clock cap.
            force_subscription: Strip the API key so billing uses subscription credit.
            binary: Name or path of the Claude Code executable.
        """
        self.model = model
        self.allowed_tools = tuple(allowed_tools)
        self.permission_mode = permission_mode
        self.timeout_seconds = timeout_seconds
        self.force_subscription = force_subscription
        self.binary = binary

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
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
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
        tokens = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
        cost = data.get("total_cost_usd")
        return ProviderResult(
            text=data.get("result", ""),
            tokens=tokens,
            cost_usd=float(cost) if isinstance(cost, (int, float)) else 0.0,
            model=self.model or self.name,
        )
