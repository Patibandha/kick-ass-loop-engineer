"""OpenAI Codex provider running headless through the Codex CLI (``codex exec``).

Like the Claude Code and Antigravity providers this is BOTH a prose provider
and an in-place pipeline builder:

* :meth:`CodexCliProvider.complete` runs in Codex's ``read-only`` sandbox and
  returns the agent's final message — for decompose, architect and review roles.
  A reviewer that can edit the code it reviews is not a reviewer.
* :meth:`CodexCliProvider.edit` (``edits_in_place = True``) runs in the
  ``workspace-write`` sandbox rooted at the attempt's worktree; the engine then
  harvests whatever changed and applies the write guardrails.

Headless contract::

    codex exec --ignore-user-config --ephemeral --skip-git-repo-check \
        -s <sandbox> [-m <model>] -o <last-message file> -C <dir> -

The prompt goes over stdin (``-``), never argv (Windows caps a command line at
32767 characters). ``--ignore-user-config`` keeps desktop-app plugin runtimes in
``~/.codex/config.toml`` from breaking the Windows sandbox; auth still comes
from ``CODEX_HOME`` (the subscription login), and ``OPENAI_API_KEY`` is removed
from the child environment so usage never falls back to metered API billing.
``codex`` is an npm ``.cmd`` shim on Windows, so every call goes through
:func:`~.cli_process.run_cli`, whose timeout kills the whole process tree.
The CLI reports no dollar cost; the engine prices calls from its table.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Optional

from ..guardrails import is_credential_env
from .base import Provider, ProviderError, ProviderResult, retry_transient
from .cli_process import run_cli

#: Environment variables a subscription CLI child must never see.
_SCRUBBED_ENV = ("OPENAI_API_KEY", "TYPESAFE_API_KEY", "CLOUDFLARE_API_TOKEN")
#: Name of the final-message file inside the per-call scratch directory.
_LAST_MESSAGE = "last.txt"
#: Characters of stderr carried into an error message.
_DETAIL_CHARS = 600


class CodexCliProvider(Provider):
    """Provider backed by ``codex exec`` on a ChatGPT subscription login."""

    name = "codex_cli"
    edits_in_place = True

    def __init__(
        self,
        model: Optional[str] = None,
        binary: str = "codex",
        timeout_seconds: float = 600.0,
        edit_timeout_seconds: float = 1800.0,
        cwd: Optional[str] = None,
        retry_attempts: int = 3,
        retry_base_delay: float = 2.0,
        windows_sandbox: str = "unelevated",
    ) -> None:
        """Initialize the Codex CLI provider.

        Args:
            model: Optional ``-m`` model; None uses the CLI default.
            binary: Name or path of the Codex executable.
            timeout_seconds: Wall-clock cap for one :meth:`complete` call.
            edit_timeout_seconds: Wall-clock cap for one agentic :meth:`edit` turn.
            cwd: Root for :meth:`complete`; None gives each call a fresh empty
                scratch directory so a review sees only the brief.
            retry_attempts: Total invocations allowed for a transient failure.
            retry_base_delay: Seconds before the first retry; doubled each time.
            windows_sandbox: ``windows.sandbox`` override on Windows
                (``unelevated`` works without an admin helper); empty skips it.
        """
        self.model = model
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        self.edit_timeout_seconds = edit_timeout_seconds
        self.cwd = cwd
        self.retry_attempts = retry_attempts
        self.retry_base_delay = retry_base_delay
        self.windows_sandbox = windows_sandbox

    def complete(self, system: str, user: str) -> ProviderResult:
        """Run one read-only ``codex exec`` and return its final message.

        Raises:
            ProviderError: On a permanent failure or once transient retries run out.
        """
        if self.cwd:
            return self._call("read-only", system, user, self.cwd, self.timeout_seconds)
        with tempfile.TemporaryDirectory(prefix="le-codex-") as scratch:
            return self._call("read-only", system, user, scratch, self.timeout_seconds)

    def edit(self, system: str, user: str, *, cwd: str) -> ProviderResult:
        """Let Codex edit the workspace at *cwd* and return its summary.

        Raises:
            ProviderError: When *cwd* is not an existing directory, or the CLI fails.
        """
        if not cwd or not os.path.isdir(cwd):
            raise ProviderError(f"codex_cli edit requires an existing cwd: {cwd!r}")
        return self._call("workspace-write", system, user, cwd, self.edit_timeout_seconds)

    def _call(self, sandbox: str, system: str, user: str, cwd: str, timeout: float) -> ProviderResult:
        prompt = f"{system}\n\n{user}" if system else user
        return retry_transient(lambda: self._run(sandbox, prompt, cwd, timeout),
                               attempts=self.retry_attempts, base_delay=self.retry_base_delay)

    def _argv(self, sandbox: str, cwd: str, last_message: str) -> list:
        argv = [self.binary, "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                "-s", sandbox]
        if self.model:
            argv += ["-m", self.model]
        if os.name == "nt" and self.windows_sandbox:
            argv += ["-c", f'windows.sandbox="{self.windows_sandbox}"']
        return argv + ["-o", last_message, "-C", cwd, "-"]

    def _run(self, sandbox: str, prompt: str, cwd: str, timeout: float) -> ProviderResult:
        env = {k: v for k, v in os.environ.items()
               if k not in _SCRUBBED_ENV and not is_credential_env(k)}
        with tempfile.TemporaryDirectory(prefix="le-codex-out-") as out_dir:
            last_message = os.path.join(out_dir, _LAST_MESSAGE)
            try:
                completed = run_cli(self._argv(sandbox, cwd, last_message), stdin=prompt, env=env,
                                    cwd=cwd, timeout=timeout)
            except FileNotFoundError as exc:
                raise ProviderError(f"codex binary not found: {self.binary!r}") from exc
            except subprocess.TimeoutExpired as exc:
                raise ProviderError(f"codex timed out after {timeout:.0f}s") from exc
            except OSError as exc:
                raise ProviderError(f"codex subprocess error: {exc}") from exc
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()[-_DETAIL_CHARS:]
                raise ProviderError(detail or f"codex exited {completed.returncode}")
            text = _read(last_message) or (completed.stdout or "")
        if not text.strip():
            raise ProviderError("empty response from codex")
        return ProviderResult(text=text.strip(), model=self.model or self.name)


def _read(path: str) -> str:
    """Return the file's text, or "" when Codex did not write it."""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""
