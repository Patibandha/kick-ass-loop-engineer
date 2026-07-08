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
"""

from __future__ import annotations

import fnmatch

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
) -> tuple[bool, str]:
    """Determine whether the proposed operation requires human escalation.

    Checks are applied in the following priority order and the FIRST match is
    returned, so reason strings pinpoint the triggering category:

    1. Risky action keyword detected in *action*.
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

    Returns:
        A ``(escalate: bool, reason: str)`` tuple.  When *escalate* is
        ``True``, *reason* is a non-empty human-readable string naming the
        denylist category and the specific trigger.
    """
    # 1. Risky-action keyword check
    low = action.lower()
    for tok in _RISKY_TOKENS:
        if tok in low:
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
