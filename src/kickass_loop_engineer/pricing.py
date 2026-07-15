"""Pricing registry: bundled per-Mtok price table with prefix resolution.

Model rates live in the bundled ``pricing.yaml`` data file (editable at release
time), optionally overlaid by a user file (``ledger.pricing_path``) and then an
inline mapping (a ``pricing:`` map in loop-engineer.yaml). Lookup is
longest-key-prefix so one ``gpt-`` row covers a whole family while a more
specific ``gpt-5.5`` row wins for its models. YAML is imported lazily so
importing the package does not require PyYAML.
"""

from __future__ import annotations

import importlib.resources
from typing import Any, Optional

_TOKENS_PER_UNIT = 1_000_000  # rates are USD per million tokens


def load_pricing(path: str = "", inline: Optional[dict] = None) -> dict[str, Any]:
    """Load the pricing table: bundled defaults, then file and inline overlays.

    Precedence (lowest to highest): the bundled ``pricing.yaml``, the YAML file
    at ``path`` (when given), then ``inline`` (when given). All keys are
    lowercased so lookups are case-insensitive.

    Args:
        path: Optional path to a user pricing YAML that overlays the bundled
            table row-by-row.
        inline: Optional mapping (e.g. a ``pricing:`` section from
            loop-engineer.yaml) that overlays both.

    Returns:
        A mapping of lowercase model prefix -> rate row
        (``{"in": float, "out": float, "context_window": int}``).

    Raises:
        RuntimeError: When PyYAML is unavailable, when the bundled table cannot
            be loaded (a packaging bug — never hidden as an empty table), or
            when ``path`` is unreadable or malformed.
    """
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to load pricing tables (pip install pyyaml)") from exc
    try:
        resource = importlib.resources.files("kickass_loop_engineer").joinpath("pricing.yaml")
        bundled = yaml.safe_load(resource.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"failed to load bundled pricing.yaml (packaging bug): {exc}") from exc
    table = {str(key).lower(): value for key, value in bundled.items()}
    if path:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                overlay = yaml.safe_load(handle) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise RuntimeError(f"failed to load pricing table {path!r}: {exc}") from exc
        if not isinstance(overlay, dict):
            raise RuntimeError(f"pricing table {path!r} must be a mapping of "
                               f"model prefix -> rates (got {type(overlay).__name__})")
        table.update({str(key).lower(): value for key, value in overlay.items()})
    if inline:
        table.update({str(key).lower(): value for key, value in inline.items()})
    return table


def resolve(model: str, table: dict[str, Any]) -> Optional[dict]:
    """Find the pricing row for a model by longest-prefix match.

    Matching is case-insensitive (``model.lower().startswith(key)``) and the
    longest matching key wins, so a specific ``gpt-5.5`` row beats the family
    ``gpt-`` row. Ties are impossible: two distinct prefixes of the same
    length cannot both prefix one model name.

    Args:
        model: Model identifier as configured (any case).
        table: Pricing table from :func:`load_pricing`.

    Returns:
        The matched rate row, or ``None`` when no key prefixes the model.
    """
    name = model.lower()
    match = ""
    for key in table:
        if name.startswith(key) and len(key) > len(match):
            match = key
    return table[match] if match else None


def estimate_cost(*, model: str, prompt_tokens: int, completion_tokens: int,
                  tokens: int, table: dict[str, Any]) -> Optional[float]:
    """Estimate the USD cost of a call from its token usage.

    Prompt tokens are charged at the input rate and completion tokens at the
    output rate. Any residual total — ``max(0, tokens - prompt - completion)``,
    tokens the provider counted but did not attribute to either side — is
    charged at the OUTPUT rate on top of the split, deliberately conservative
    so budget caps err toward tripping early rather than undercounting spend.
    A fully missing split (both zero) is just the residual rule: ALL tokens at
    the output rate. Negative inputs are clamped to zero and the result is
    rounded to 6 decimals.

    Args:
        model: Model identifier (resolved via :func:`resolve`).
        prompt_tokens: Input-side tokens (0 when the split is unknown).
        completion_tokens: Output-side tokens (0 when the split is unknown).
        tokens: Total tokens (any excess over the split is charged at the
            output rate).
        table: Pricing table from :func:`load_pricing`.

    Returns:
        Estimated cost in USD, or ``None`` when the model has no pricing row.
    """
    row = resolve(model, table)
    if row is None:
        return None
    in_rate = float(row.get("in", 0.0))
    out_rate = float(row.get("out", 0.0))
    prompt = max(0, prompt_tokens)
    completion = max(0, completion_tokens)
    residual = max(0, max(0, tokens) - prompt - completion)
    cost = (prompt * in_rate + (completion + residual) * out_rate) / _TOKENS_PER_UNIT
    return round(cost, 6)


def context_window(model: str, table: dict[str, Any]) -> int:
    """Return the model's context window from its pricing row.

    Args:
        model: Model identifier (resolved via :func:`resolve`).
        table: Pricing table from :func:`load_pricing`.

    Returns:
        The row's ``context_window``, or ``0`` when the model is unknown or
        its row carries no window.
    """
    row = resolve(model, table)
    if row is None:
        return 0
    return int(row.get("context_window", 0))
