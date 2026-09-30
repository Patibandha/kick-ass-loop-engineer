"""Validated ``decision:`` config: backends, confidence gate, floors, tiers."""

from __future__ import annotations

from dataclasses import dataclass, field

from .broker import DEFAULT_MIN_CONFIDENCE
from .floors import REVIEW_LEVELS, SECURITY_LEVELS, SENSITIVE_DOMAINS

DEFAULT_MAX_ATTEMPTS = 3
_BACKENDS = ("jev", "rules")


@dataclass(frozen=True)
class DecisionSettings:
    """Operator settings for the decision layer.

    Attributes:
        backend: ``jev`` (hosted, falls back to rules) or ``rules`` (offline).
        min_confidence: Model answers below this use the safe default.
        security_min: Global security floor (``L1``-``L4``).
        review_min: Global review floor (``light``/``standard``/``strict``).
        signoff_domains: Sensitive domains that force human sign-off (L4).
        max_attempts: Ceiling on attempts per slice the model may choose.
        tiers: Ordered cheap -> strong ``AttemptSpec`` ladder (may be empty).
        jev: Raw Jev client options (model, base_url, api_key_env, timeout_s).
    """

    backend: str = "jev"
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    security_min: str = "L1"
    review_min: str = "light"
    signoff_domains: tuple = ()
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    tiers: tuple = ()
    jev: dict = field(default_factory=dict)

    def tier_names(self) -> tuple:
        """Return stable option ids for the tier ladder (``t0``, ``t1``...)."""
        return tuple(f"t{i}" for i in range(len(self.tiers)))


def parse_settings(section: dict, tiers: tuple) -> DecisionSettings:
    """Validate a raw ``decision:`` mapping into :class:`DecisionSettings`.

    Args:
        section: The parsed ``decision`` config mapping.
        tiers: Already-parsed tier ``AttemptSpec`` tuple.

    Raises:
        RuntimeError: On any unknown backend, level, domain, or bad number —
            misconfiguration fails before any spend.
    """
    backend = str(section.get("backend", "jev"))
    if backend not in _BACKENDS:
        raise RuntimeError(f"decision.backend must be one of {_BACKENDS}, got {backend!r}")
    floors = section.get("floors") or {}
    security_min = str(floors.get("security", "L1"))
    review_min = str(floors.get("review", "light"))
    if security_min not in SECURITY_LEVELS:
        raise RuntimeError(f"decision.floors.security must be one of {SECURITY_LEVELS}")
    if review_min not in REVIEW_LEVELS:
        raise RuntimeError(f"decision.floors.review must be one of {REVIEW_LEVELS}")
    signoff = tuple(str(d) for d in (section.get("signoff_domains") or ()))
    unknown = sorted(set(signoff) - set(SENSITIVE_DOMAINS))
    if unknown:
        raise RuntimeError(f"decision.signoff_domains has unknown domains {unknown}; "
                           f"known: {sorted(SENSITIVE_DOMAINS)}")
    try:
        min_confidence = float(section.get("min_confidence", DEFAULT_MIN_CONFIDENCE))
        max_attempts = int(section.get("max_attempts", DEFAULT_MAX_ATTEMPTS))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"decision: bad number ({exc})") from exc
    if not 0.0 <= min_confidence <= 1.0:
        raise RuntimeError("decision.min_confidence must be within [0, 1]")
    if max_attempts < 1:
        raise RuntimeError("decision.max_attempts must be >= 1")
    jev = {k: section[k] for k in ("model", "base_url", "api_key_env", "timeout_s",
                                   "retries", "route", "account_id_env")
           if k in section}
    if jev.get("route", "typesafe") not in ("typesafe", "cloudflare"):
        raise RuntimeError("decision.route must be 'typesafe' or 'cloudflare'")
    return DecisionSettings(backend=backend, min_confidence=min_confidence,
                            security_min=security_min, review_min=review_min,
                            signoff_domains=signoff, max_attempts=max_attempts,
                            tiers=tuple(tiers), jev=jev)
