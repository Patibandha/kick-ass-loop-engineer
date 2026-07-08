"""Security guardrails for the loop: write policy and verification allowlist.

Two layers of protection:
    * ``WritePolicy`` constrains what generated files may be written — inside the
      workspace only, never over sensitive paths (.git, .env, keys), with size
      and count caps to bound blast radius.
    * ``VerificationPolicy`` constrains what commands the orchestrator may run to
      verify a round — an allowlist of known test/build/lint tools, no shell
      metacharacters, a timeout, and an environment scrubbed of secrets.

These guardrails are enforced in code (not merely requested in a prompt) so an
untrusted model's output cannot escape the workspace or trigger arbitrary shell
execution.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import shlex
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger("kickass_loop_engineer.guardrails")

DEFAULT_PROTECTED = (
    ".git", ".git/*", ".gitignore",
    ".env", ".env.*", "*.env",
    ".ssh", ".ssh/*", "id_rsa", "id_rsa.*", "*.pem", "*.key",
    ".aws", ".aws/*", ".npmrc", ".pypirc",
    "node_modules", "node_modules/*",
    "*secret*", "*credentials*", "*.crt",
)

DEFAULT_VERIFY_PREFIXES = (
    "pytest", "python -m pytest", "python -m unittest", "python -m mypy",
    # python3 variants — many systems ship only `python3`, no `python`
    "python3 -m pytest", "python3 -m unittest", "python3 -m mypy",
    "python3 -m pytest_benchmark",
    "ruff", "flake8", "black --check", "mypy",
    "npm test", "npm run build", "npm run lint", "yarn test", "pnpm test",
    "eslint", "tsc --noEmit",
    "go test", "go build", "go vet",
    "cargo test", "cargo build", "cargo check",
    "make test", "make build", "make lint",
    # verifier gate tools (M1)
    "bandit", "semgrep", "gitleaks", "detect-secrets", "trufflehog",
    "pytest-benchmark", "python -m pytest_benchmark", "k6 run", "locust",
)

_SHELL_METACHARACTERS = (";", "|", "&", "`", "$", ">", "<", "\n", "\r", "(", ")", "{", "}")


@dataclass
class WritePolicy:
    """Constraints on files written into the workspace.

    Attributes:
        protected_globs: Path patterns that must never be written.
        max_file_bytes: Maximum size of any single generated file.
        max_files_per_round: Maximum files a single build round may write.
        allow_overwrite: Whether existing files may be overwritten.
    """

    protected_globs: tuple = DEFAULT_PROTECTED
    max_file_bytes: int = 1_000_000
    max_files_per_round: int = 50
    allow_overwrite: bool = True

    def check_path(self, rel_path: str) -> tuple[bool, str]:
        """Return ``(ok, reason)`` for a proposed workspace-relative path."""
        if os.path.isabs(rel_path):
            return False, "absolute paths are not allowed"
        normalized = os.path.normpath(rel_path)
        if normalized.startswith("..") or normalized == ".":
            return False, "path escapes the workspace"
        segments = normalized.replace("\\", "/").split("/")
        for pattern in self.protected_globs:
            if fnmatch.fnmatch(normalized, pattern):
                return False, f"path matches protected pattern {pattern!r}"
            if any(fnmatch.fnmatch(segment, pattern) for segment in segments):
                return False, f"path component matches protected pattern {pattern!r}"
        return True, ""

    def check_content(self, rel_path: str, content: str) -> tuple[bool, str]:
        """Return ``(ok, reason)`` for a proposed file's content size."""
        size = len(content.encode("utf-8"))
        if size > self.max_file_bytes:
            return False, f"{rel_path} is {size} bytes, exceeds cap {self.max_file_bytes}"
        return True, ""


@dataclass
class VerificationResult:
    """Outcome of running a verification command.

    Attributes:
        passed: True when the command exited zero within the timeout.
        command: The command that was run.
        returncode: Process exit code, or None if it never ran.
        stdout_tail: Trailing stdout for context.
        stderr_tail: Trailing stderr for context.
        error: Reason the command was rejected or failed to run.
    """

    passed: bool
    command: str
    returncode: int = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    error: str = ""


@dataclass
class VerificationPolicy:
    """Constraints on commands the orchestrator may run to verify a round.

    Attributes:
        allowed_prefixes: Command prefixes permitted to run.
        timeout_seconds: Hard cap on a verification command's runtime.
        tail_chars: How many trailing output characters to capture.
    """

    allowed_prefixes: tuple = DEFAULT_VERIFY_PREFIXES
    timeout_seconds: float = 300.0
    tail_chars: int = 2000

    def validate(self, command: str) -> tuple[bool, str]:
        """Return ``(ok, reason)`` for a proposed verification command."""
        stripped = (command or "").strip()
        if not stripped:
            return False, "empty command"
        if any(meta in stripped for meta in _SHELL_METACHARACTERS):
            return False, "shell metacharacters are not allowed"
        if not any(stripped == p or stripped.startswith(p + " ") for p in self.allowed_prefixes):
            return False, f"command not in verification allowlist: {stripped!r}"
        return True, ""

    def run(self, command: str, cwd: str) -> VerificationResult:
        """Validate and run a verification command in a scrubbed environment.

        Args:
            command: The allowlisted command to execute.
            cwd: Directory to run it in (the workspace).

        Returns:
            A ``VerificationResult`` describing the outcome.
        """
        ok, reason = self.validate(command)
        if not ok:
            logger.warning("verification rejected: %s (%s)", command, reason)
            return VerificationResult(passed=False, command=command, error=reason)

        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        try:
            completed = subprocess.run(
                shlex.split(command),
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError:
            return VerificationResult(passed=False, command=command, error="verification tool not found")
        except subprocess.TimeoutExpired:
            return VerificationResult(passed=False, command=command, error=f"timed out after {self.timeout_seconds:.0f}s")
        except OSError as exc:
            return VerificationResult(passed=False, command=command, error=f"failed to run: {exc}")

        return VerificationResult(
            passed=completed.returncode == 0,
            command=command,
            returncode=completed.returncode,
            stdout_tail=(completed.stdout or "")[-self.tail_chars:],
            stderr_tail=(completed.stderr or "")[-self.tail_chars:],
        )
