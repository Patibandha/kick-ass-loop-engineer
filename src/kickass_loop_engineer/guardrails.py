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
import posixpath
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Optional

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
    # 3.0 gate tools (spec §3.1 — curated; NEVER a blanket "npx")
    "npx playwright", "npx playwright-cli", "npx --yes @probelabs/maid",
    "lint-imports",
)

#: Credentials never forwarded to a verification subprocess (or a builder CLI):
#: model keys stay with the engine process that owns them.
SCRUBBED_ENV_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "TYPESAFE_API_KEY",
                     "CLOUDFLARE_API_TOKEN")
#: Any other credential-named variable (``OPENAI_API_KEY``, ``GITHUB_TOKEN``, ``DB_PASSWORD``…) is scrubbed too:
#: a gate's output is journaled, so whatever reaches the gate can reach a log.
_CREDENTIAL_NAME = re.compile(r"(?i)(API_?KEY|TOKEN|SECRET|PASSWORD)$")

#: ``^`` and ``%`` are cmd.exe syntax (escape, variable expansion): Windows runs a ``.cmd``/``.bat`` shim
#: such as ``npm.cmd`` through cmd.exe even without ``shell=True``, so they must never reach one.
#: Names Windows maps to devices in any folder and with any extension (``con.txt`` is the console).
_WINDOWS_DEVICE_NAMES = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)),
                                   *(f"LPT{n}" for n in range(1, 10))})

_SHELL_METACHARACTERS = (";", "|", "&", "`", "$", ">", "<", "\n", "\r", "(", ")", "{", "}", "^", "%")


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
        # Judged the same on every host: a backslash is a separator, so a Windows-only spelling is
        # refused on POSIX too instead of being written as an odd filename.
        posix = rel_path.replace("\\", "/")
        if os.path.isabs(rel_path) or posixpath.isabs(posix):
            return False, "absolute paths are not allowed"
        if ":" in posix:
            return False, "drive letters and alternate data streams (':') are not allowed"
        normalized = posixpath.normpath(posix)
        if normalized.startswith("..") or normalized == ".":
            return False, "path escapes the workspace"
        segments = normalized.split("/")
        # Judged on every host: Win32 strips a trailing dot or space, so ``.git./config`` lands in ``.git``.
        if any(segment.endswith((".", " ")) for segment in segments):
            return False, "a path component ends in a dot or space (trailing characters Windows strips)"
        if any(segment.split(".")[0].rstrip(" ").upper() in _WINDOWS_DEVICE_NAMES for segment in segments):
            return False, "a path component is a reserved Windows device name"
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


#: Files that mark a Python project root; with a ``src/`` folder beside one, gates import that folder first.
_PYTHON_PROJECT_FILES = ("pyproject.toml", "setup.py", "setup.cfg")


def _gate_environment(cwd: str) -> dict:
    """The scrubbed environment a gate runs in.

    A src-layout Python project gets ``<cwd>/src`` first on ``PYTHONPATH`` so the gate tests the
    code in THIS workspace (an attempt worktree), not an editable install that points elsewhere.
    """
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV_KEYS and not _CREDENTIAL_NAME.search(k)}
    src = os.path.join(cwd, "src")
    if os.path.isdir(src) and any(os.path.isfile(os.path.join(cwd, name)) for name in _PYTHON_PROJECT_FILES):
        env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH", "")) if p)
    return env


def _program_suffixes(name: str, env: dict) -> list:
    """Suffixes to try: on Windows only PATHEXT ones (an extensionless file there is a Unix script that
    CreateProcess cannot run), unless *name* already carries one; elsewhere just the bare name."""
    if os.name != "nt":
        return [""]
    pathext = [e for e in env.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").lower().split(";") if e]
    return [""] if os.path.splitext(name)[1].lower() in pathext else pathext


def _resolve_program(argv: list, env: dict, cwd: str) -> Optional[list]:
    """Find the program on the gate's PATH, as a shell would, or None when it is not there.

    On Windows a bare name is otherwise looked up in the engine's own folder (and its current
    directory) first, so ``python`` would run the engine's base interpreter instead of the
    project's virtualenv. Only absolute PATH folders OUTSIDE the workspace are searched: a builder
    must not be able to plant its own ``python`` or ``pytest`` and forge a green gate. A name with
    a folder in it is left as written (the allowlist already matched it).
    """
    if not argv or os.path.dirname(argv[0]):
        return argv
    workspace = os.path.normcase(os.path.realpath(cwd))
    exts = _program_suffixes(argv[0], env)
    for folder in env.get("PATH", "").split(os.pathsep):
        if not folder or not os.path.isabs(folder):
            continue
        real = os.path.normcase(os.path.realpath(folder))
        if real == workspace or real.startswith(workspace + os.sep):
            continue
        for ext in exts:
            candidate = os.path.join(folder, argv[0] + ext)
            if os.path.isfile(candidate) and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return [candidate, *argv[1:]]
    return None


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

    def run(self, command: str, cwd: str, *, expect: str = "",
            min_count: Optional[int] = None) -> VerificationResult:
        """Validate and run a verification command in a scrubbed environment.

        Args:
            command: The allowlisted command to execute.
            cwd: Directory to run it in (the workspace).
            expect: Optional regex the command's output (stdout, else stderr)
                must match. An exit code alone cannot distinguish a green suite
                from one that ran nothing — see :class:`~.gates.GateSpec`.
            min_count: Optional floor on the first capture group of *expect*,
                read as an integer. Ignored without *expect*.

        Returns:
            A ``VerificationResult`` describing the outcome. An unmet
            expectation is a FAILURE whose ``error`` names what was expected
            and what the output actually said.
        """
        ok, reason = self.validate(command)
        if not ok:
            logger.warning("verification rejected: %s (%s)", command, reason)
            return VerificationResult(passed=False, command=command, error=reason)

        env = _gate_environment(cwd)
        argv = _resolve_program(shlex.split(command), env, cwd)
        if argv is None:
            return VerificationResult(passed=False, command=command, error="verification tool not found")
        try:
            completed = subprocess.run(
                argv,
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

        result = VerificationResult(
            passed=completed.returncode == 0,
            command=command,
            returncode=completed.returncode,
            stdout_tail=(completed.stdout or "")[-self.tail_chars:],
            stderr_tail=(completed.stderr or "")[-self.tail_chars:],
        )
        if not result.passed or not expect:
            return result
        return self._check_expectation(result, completed.stdout, completed.stderr,
                                       expect, min_count)

    @staticmethod
    def _check_expectation(result: VerificationResult, stdout: str, stderr: str,
                           expect: str, min_count: Optional[int]) -> VerificationResult:
        """Fail *result* unless the command's output proves what the gate expects.

        Both streams are searched: a tool that summarises on stderr is still
        telling the truth about itself.
        """
        try:
            pattern = re.compile(expect)
        except re.error as exc:
            result.passed = False
            result.error = f"gate expectation {expect!r} is not a valid regex: {exc}"
            return result

        match = pattern.search(stdout or "") or pattern.search(stderr or "")
        if match is None:
            tail = ((stdout or "") + (stderr or "")).strip()[-160:]
            result.passed = False
            result.error = (f"expected output matching {expect!r}, but the command "
                            f"exited 0 saying: {tail!r}")
            return result
        if min_count is None:
            return result
        try:
            found = int(match.group(1))
        except (IndexError, ValueError):
            result.passed = False
            result.error = (f"gate expectation {expect!r} must capture a number to "
                            f"compare against min_count={min_count}")
            return result
        if found < min_count:
            result.passed = False
            result.error = (f"expected at least {min_count} from {expect!r}, got "
                            f"{found} — the suite shrank, which is a regression")
        return result
