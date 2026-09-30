"""Escalation denylist for the loop engineer.

The denylist is the only recurring human touchpoint in L3 (unattended) mode.
It evaluates four escalation categories in priority order and returns the first
match so callers always see exactly what triggered the interrupt:

1. **Risky actions** — keywords in the action string that imply destructive or
   irreversible operations (deploy, delete, drop table, spend, rm -rf, …).
2. **Protected paths** — files that should never be touched autonomously,
   matched with :func:`fnmatch.fnmatch` glob patterns. The globs derive from
   the single write-policy source of truth, :data:`guardrails.DEFAULT_PROTECTED`
   (secrets, keys, VCS/dependency internals), plus escalation-only extras
   (migrations, auth code, payment/billing code).
3. **File-count cap** — too many files touched in a single pass increases blast
   radius; escalate when ``files_touched > max_files``.
4. **Attempt cap** — repeated failures on the same objective signal a loop that
   needs human intervention; escalate when ``attempt >= max_attempts``.

Only the FIRST layer is relaxable. ``should_escalate(allow_tokens=...)`` lets an
operator exempt named risky keywords for one run, because objectives legitimately
contain them: writing a systemd unit file is not a *deploy*, and a "model spend
cap" is not a *spend*. Tokens are compared case-insensitively after ``.strip()``
and must already be members of :data:`_RISKY_TOKENS`, so the knob narrows an
existing check rather than inventing new vocabulary. Protected paths, the
file-count cap and the attempt cap are NEVER relaxable — an allow list cannot
reach layers 2-4.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable

from .guardrails import DEFAULT_PROTECTED

_RISKY_TOKENS: tuple[str, ...] = (
    "deploy",
    "delete",
    "drop table",
    "drop ",
    "spend",
    "rm -rf",
    "force push",
)

# Escalation-only additions on top of the single write-policy source of truth.
_ESCALATION_EXTRA_GLOBS: tuple[str, ...] = (
    "secrets/*", "migrations/*", "*/auth/*", "auth/*", "*payment*", "*billing*",
)

PROTECTED_GLOBS: tuple[str, ...] = tuple(dict.fromkeys(DEFAULT_PROTECTED + _ESCALATION_EXTRA_GLOBS))


def should_escalate(
    *,
    action: str = "",
    paths: list[str] | None = None,
    files_touched: int = 0,
    attempt: int = 1,
    max_files: int = 10,
    max_attempts: int = 3,
    allow_tokens: Iterable[str] | None = None,
) -> tuple[bool, str]:
    """Determine whether the proposed operation requires human escalation.

    Checks are applied in the following priority order and the FIRST match is
    returned, so reason strings pinpoint the triggering category:

    1. Risky action keyword detected in *action* (skipping *allow_tokens*).
    2. One or more *paths* matched a protected-path glob.
    3. *files_touched* exceeds *max_files*.
    4. *attempt* has reached *max_attempts*.

    If none of the above match, returns ``(False, "")`` indicating the
    operation is safe to proceed autonomously.

    Args:
        action: Human-readable description of the planned operation.
        paths: List of file paths the operation will read or write.
        files_touched: Total number of files that will be modified.
        attempt: Current retry attempt number (1-based).
        max_files: Upper bound on files before escalating (inclusive).
        max_attempts: Attempt number at which escalation is triggered
            (inclusive — escalates when ``attempt >= max_attempts``).
        allow_tokens: Risky keywords to SKIP in check 1 for this run, compared
            case-insensitively after ``.strip()`` (so ``["Deploy"]`` exempts
            ``"deploy"``). Set from the ``escalation.allow_tokens`` config key
            when an objective legitimately contains a flagged word — writing a
            systemd unit file, or capping model *spend*. It relaxes the keyword
            layer ONLY: protected paths, the file-count cap and the attempt cap
            still escalate exactly as before, and a token that is not already a
            risky keyword has no effect here (config rejects it at load time).

    Returns:
        A ``(escalate: bool, reason: str)`` tuple.  When *escalate* is
        ``True``, *reason* is a non-empty human-readable string naming the
        denylist category and the specific trigger.
    """
    # 1. Risky-action keyword check. Word-boundary matching: a bare substring
    # test flagged the AppImage tool "linuxdeploy" as "deploy" and blocked a
    # gate-green packaging slice. Tokens with their own trailing space (e.g.
    # "drop ") keep their explicit shape.
    # Tokens named in *allow_tokens* are skipped here (and ONLY here).
    allowed = {str(t).strip().lower() for t in (allow_tokens or ())}
    low = action.lower()
    for tok in _RISKY_TOKENS:
        if tok.strip().lower() in allowed:
            continue
        pattern = (re.escape(tok) if tok != tok.strip()
                   else rf"\b{re.escape(tok)}\b")
        if re.search(pattern, low):
            return (True, f"risky action: {tok.strip()}")

    # 2. Protected-path glob check
    for p in (paths or []):
        for g in PROTECTED_GLOBS:
            if fnmatch.fnmatch(p, g):
                return (True, f"protected path: {p}")

    # 3. File-count cap
    if files_touched > max_files:
        return (True, f"too many files: {files_touched} > {max_files}")

    # 4. Attempt cap
    if attempt >= max_attempts:
        return (True, f"attempt {attempt} reached escalation threshold")

    return (False, "")
