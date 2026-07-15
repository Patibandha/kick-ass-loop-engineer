"""Reproduce-first fix stage: pin the bug with an engine-committed RED test.

Fix mode never trusts a bug description — it demands a reproduction: one
minimal failing test, emitted by the builder, verified to FAIL on the
unfixed code (the deterministic RED check), then committed by the ENGINE.

WHY the engine commits the repro before any fix attempt: proof-of-test
(``verifier.prove``) stashes the working tree's uncommitted files to check a
gate goes red without the change. An uncommitted repro would revert WITH the
fix during that stash, so "red" would mean the test file went missing, not
that the bug came back. Committed first, revert→red genuinely proves the fix
kills the bug.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

from .guardrails import VerificationPolicy
from .workspace import Workspace

logger = logging.getLogger("kickass_loop_engineer.reproduce")

_SLUG_MAX_LENGTH = 40

REPRO_SYSTEM = (
    "You are a senior software engineer writing a bug reproduction. Emit "
    "exactly ONE file block — a minimal failing test at the exact "
    "tests/test_repro_<slug>.py path named in the request — in this format:\n"
    "=== FILE: tests/test_repro_<slug>.py ===\n<full test file>\n"
    "=== END FILE ===\n"
    "The test MUST FAIL on the current (buggy) code and pass once the bug is "
    "fixed. The test must be fully self-contained: it runs with pytest "
    "configuration disabled (no conftest.py, no ini addopts), so do not rely "
    "on fixtures or hooks defined elsewhere — put any sys.path setup inline "
    "in the test file. Do NOT fix the bug, do NOT emit any other file, and "
    "do not include commentary outside the file block."
)

# Corrective feedback for the single regeneration when the repro is green.
REPRO_PASSED_FEEDBACK = (
    "YOUR PREVIOUS REPRO PASSED when run against the current (buggy) code, "
    "so it reproduces nothing. The reproduction test must FAIL until the bug "
    "is fixed. Regenerate the full file with a test that exercises the "
    "described bug and fails on the code as it is today."
)

# Appended to every fix-mode builder brief (the enhance-mode
# ``UPDATE_INSTRUCTION`` precedent): the committed repro is engine-pinned —
# restored before every gate run and after every promote — and the proving
# gate runs config-isolated, so tampering (direct or via pytest config) is
# futile.
FIX_MODE_RULES = (
    "FIX MODE RULES:\n"
    "- A committed reproduction test (tests/test_repro_*.py) pins the bug; "
    "your fix must make it pass.\n"
    "- NEVER modify or delete tests/test_repro_*.py — the engine restores "
    "the committed version before every gate run and flags the edit as "
    "tampering. Fix the code under test instead.\n"
    "- NEVER create or modify pytest configuration (conftest.py, pytest.ini, "
    "pyproject.toml [tool.pytest], setup.cfg, tox.ini) to skip, deselect, or "
    "reorder tests — the repro gate runs with configuration disabled, so "
    "such edits cannot pass it and are treated as tampering."
)


@dataclass
class ReproResult:
    """Outcome of the reproduce stage.

    Attributes:
        path: Workspace-relative path of the repro test file.
        committed: True when the RED repro was committed by the engine.
        blocked: True when no failing reproduction could be produced.
        reason: Human-readable explanation when ``blocked`` (always prefixed
            ``cannot reproduce:`` so the terminal outcome reads plainly).
    """

    path: str = ""
    committed: bool = False
    blocked: bool = False
    reason: str = ""


def repro_check_command(rel_path: str) -> str:
    """The config-isolated pytest invocation for a repro test.

    ``--noconftest -o addopts=`` neutralizes repo-level pytest configuration
    (conftest.py hooks, ini addopts), closing the sibling-file gaming channel:
    a builder-written conftest that skip-marks ``test_repro_*`` would make a
    red repro exit 0 (skipped tests pass) at both the RED check and the
    proving gate. The command still starts with the allowlisted
    ``python3 -m pytest`` prefix, so no guardrail changes are needed.

    Args:
        rel_path: Workspace-relative repro test path.

    Returns:
        The command string used by the RED check AND the appended GateSpec.
    """
    return f"python3 -m pytest {rel_path} -q --noconftest -o addopts="


def slugify(text: str, max_length: int = _SLUG_MAX_LENGTH) -> str:
    """Slugify *text* for the repro filename and commit message.

    Lowercase, non-alphanumerics collapsed to single dashes, trimmed, capped
    at *max_length*. Never empty: a goal with no usable characters yields
    ``"bug"``.

    Args:
        text: The objective goal (or any free-form text).
        max_length: Hard cap on the slug length.

    Returns:
        The slug.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    slug = slug[:max_length].rstrip("-")
    return slug or "bug"


def _git(workspace: str, *args: str,
         timeout: float = 60.0) -> subprocess.CompletedProcess:
    """Run a git subcommand in the workspace, raising on failure or timeout."""
    return subprocess.run(["git", *args], cwd=workspace, capture_output=True,
                          text=True, check=True, timeout=timeout)


def _write_repro(text: str, rel_path: str, workspace_root: str) -> bool:
    """Write ONLY the repro file block from builder output, guarded.

    The builder is instructed to emit one block at *rel_path*; anything else
    it emits is ignored — a repro generation may never write other files.
    The matching block goes through the guarded ``Workspace`` (path/content
    policy) like every other generated file.

    Args:
        text: Raw builder output.
        rel_path: The engine-chosen ``tests/test_repro_<slug>.py`` path.
        workspace_root: The main workspace root.

    Returns:
        True iff the repro file was written at *rel_path*.
    """
    for block in Workspace.parse(text):
        if block.path != rel_path:
            logger.warning(
                "repro generation emitted %s (only %s is accepted); ignored",
                block.path, rel_path)
            continue
        content = block.content
        if not content.endswith("\n"):
            content += "\n"
        outcome = Workspace(root=workspace_root).apply(
            f"=== FILE: {rel_path} ===\n{content}\n=== END FILE ===")
        return rel_path in outcome.written
    return False


def generate_repro(builder, objective, workspace: str,
                   policy: Optional[VerificationPolicy] = None,
                   on_usage: Optional[Callable] = None) -> ReproResult:
    """Produce, RED-verify, and engine-commit a reproduction test for a bug.

    Flow: one builder call emits the repro file (written via the guarded
    ``Workspace``), then the deterministic RED check runs the allowlisted,
    config-isolated :func:`repro_check_command` through *policy* — the repro
    MUST fail (exit != 0) on the unfixed code. A repro that PASSES (or never
    arrives)
    earns exactly ONE corrective regeneration; a second miss blocks the
    stage. A confirmed-RED repro is committed by the ENGINE, staging only
    that file, so proof-of-test can later revert the fix and watch the still
    committed repro go genuinely red (see the module docstring for why the
    commit must precede any attempt).

    Args:
        builder: An ``agents.Builder``; its provider does the completions.
        objective: The run objective; ``goal`` describes the bug and seeds
            the slug, ``done_when`` rides along for context.
        workspace: The main git workspace root the repro is written and
            committed in.
        policy: ``VerificationPolicy`` for the RED check; a default is used
            when omitted.
        on_usage: Optional callback receiving the raw ``ProviderResult`` of
            every COMPLETED provider call (both generation calls are
            ledgered — tokens are spent either way).

    Returns:
        A ``ReproResult`` — ``(path, committed=True)`` on success, or
        ``blocked=True`` with a ``cannot reproduce: ...`` reason.
    """
    policy = policy or VerificationPolicy()
    base_slug = slugify(objective.goal)
    slug, rel_path = base_slug, f"tests/test_repro_{base_slug}.py"
    # Never overwrite a pre-existing file at the repro path (a tracked test
    # by that name would be silently replaced and committed over): suffix
    # the slug until the path is free.
    suffix = 2
    while os.path.exists(os.path.join(workspace, rel_path)):
        slug = f"{base_slug}-{suffix}"
        rel_path = f"tests/test_repro_{slug}.py"
        suffix += 1
    check_cmd = repro_check_command(rel_path)
    base_prompt = (
        f"BUG:\n{objective.goal}\n\nDONE WHEN:\n{objective.done_when}\n\n"
        f"Write the reproduction test at EXACTLY this path: {rel_path}"
    )

    feedback = ""
    last_reason = ""
    for _attempt in range(2):  # one generation + exactly ONE regeneration
        prompt = f"{base_prompt}\n\n{feedback}" if feedback else base_prompt
        result = builder.provider.complete(REPRO_SYSTEM, prompt)
        if on_usage is not None:
            on_usage(result)

        if not _write_repro(result.text or "", rel_path, workspace):
            last_reason = (f"cannot reproduce: builder emitted no repro test "
                           f"at {rel_path}")
            feedback = (
                f"FORMAT ERROR: your previous reply contained no FILE block "
                f"at {rel_path}. Emit exactly ONE block:\n"
                f"=== FILE: {rel_path} ===\n<full test file>\n=== END FILE ==="
            )
            continue

        check = policy.run(check_cmd, cwd=workspace)
        if check.error:
            return ReproResult(path=rel_path, blocked=True, reason=(
                f"cannot reproduce: RED check could not run: {check.error}"))
        if check.passed:
            last_reason = ("cannot reproduce: the repro test passes on the "
                           "unfixed code")
            feedback = REPRO_PASSED_FEEDBACK
            continue

        # RED confirmed on the unfixed code: the engine pins the repro.
        # `git add <path>` + a pathspec'd commit stage ONLY that file, even
        # if the workspace index already held unrelated staged content.
        try:
            _git(workspace, "add", rel_path)
            _git(workspace, "commit",
                 "-m", f"test: reproduction for {slug} (engine-pinned)",
                 "--", rel_path)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip() or str(exc)
            return ReproResult(path=rel_path, blocked=True, reason=(
                f"cannot reproduce: could not commit the repro: {detail}"))
        except (subprocess.TimeoutExpired, OSError) as exc:
            return ReproResult(path=rel_path, blocked=True, reason=(
                f"cannot reproduce: could not commit the repro: {exc}"))
        logger.info("reproduction committed: %s (RED on the unfixed code)",
                    rel_path)
        return ReproResult(path=rel_path, committed=True)

    return ReproResult(path=rel_path, blocked=True, reason=last_reason)
