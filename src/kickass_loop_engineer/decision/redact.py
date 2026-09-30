"""Build the state sent to a hosted decision model: allowlisted and scrubbed.

Only named, typed facts leave the machine — never raw builder output, gate
logs, file contents, or environment values. A secret-shaped string anywhere in
the state fails CLOSED: the hosted call is skipped and the chain falls back.
"""

from __future__ import annotations

import json
import re

from .base import DecisionError

#: Hard cap on the serialized state (Jev allows ~32k tokens; we need far less).
MAX_STATE_BYTES = 16_000
#: Longest single string value kept in the state.
MAX_TEXT_CHARS = 1_200
#: Longest list kept in the state.
MAX_LIST_ITEMS = 12

#: The only top-level keys a decision state may carry.
ALLOWED_KEYS = frozenset({
    "objective", "done_when", "constraints", "mode", "slices", "slice",
    "slice_count", "repo", "sensitive_domains", "configured", "attempts",
    "failed_gates", "run_history",
})

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*\S{8,}"),
)


def build_state(facts: dict) -> dict:
    """Return the allowlisted, truncated, secret-checked form of *facts*.

    Args:
        facts: Candidate facts keyed by name; keys outside
            :data:`ALLOWED_KEYS` are dropped.

    Returns:
        A JSON-serializable dict safe to send to a hosted decision model.

    Raises:
        DecisionError: When a secret-shaped value is present or the state
            exceeds :data:`MAX_STATE_BYTES` after truncation.
    """
    state = {k: _clip(v) for k, v in facts.items() if k in ALLOWED_KEYS}
    serialized = json.dumps(state, sort_keys=True, default=str)
    for pattern in _SECRET_PATTERNS:
        if pattern.search(serialized):
            raise DecisionError("decision state contains a secret-shaped value; "
                                "hosted decision skipped (fail closed)")
    if len(serialized.encode("utf-8")) > MAX_STATE_BYTES:
        raise DecisionError(f"decision state exceeds {MAX_STATE_BYTES} bytes")
    return state


def _clip(value: object) -> object:
    """Truncate strings and lists recursively; stringify unknown types."""
    if isinstance(value, str):
        return value[:MAX_TEXT_CHARS]
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        return {str(k)[:MAX_TEXT_CHARS]: _clip(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clip(v) for v in list(value)[:MAX_LIST_ITEMS]]
    return str(value)[:MAX_TEXT_CHARS]
