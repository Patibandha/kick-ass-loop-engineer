"""Deterministic quality floors: the minimum staffing no model may go below.

The decision model optimizes cost ABOVE these floors; the engine clamps every
choice with ``max(floor, choice)``. Floors come from what the objective
touches (sensitive domains), the model's own risk call (which may only RAISE
a floor), and explicit config minimums.
"""

from __future__ import annotations

import re

SECURITY_LEVELS = ("L1", "L2", "L3", "L4")
REVIEW_LEVELS = ("light", "standard", "strict")

#: Domain -> keywords (word-boundary, case-insensitive) that mark it touched.
SENSITIVE_DOMAINS: dict = {
    "auth": ("auth", "authentication", "authorization", "login", "logout",
             "password", "oauth", "jwt", "session", "permission", "rbac", "sso"),
    "payments": ("payment", "payments", "billing", "invoice", "checkout",
                 "stripe", "paypal", "refund", "subscription", "credit card"),
    "secrets": ("secret", "secrets", "credential", "credentials", "api key",
                "private key", "encryption", "crypto", ".env"),
    "data": ("migration", "migrations", "schema", "database", "sql", "delete",
             "drop", "backup", "pii", "gdpr"),
    "infra": ("deploy", "deployment", "docker", "kubernetes", "k8s", "terraform",
              "ci", "pipeline", "nginx", "systemd", "cron", "dns", "firewall"),
}
#: Domains whose presence forces at least a deep (L3) security review.
_DEEP_SECURITY_DOMAINS = ("auth", "payments", "secrets")


def detect_domains(text: str) -> list:
    """Return the sorted sensitive domains whose keywords appear in *text*."""
    lowered = text.lower()
    found = []
    for domain, words in SENSITIVE_DOMAINS.items():
        for word in words:
            if re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", lowered):
                found.append(domain)
                break
    return sorted(found)


def raise_to(level: str, floor: str, scale: tuple) -> str:
    """Return the higher of *level* and *floor* on the ordered *scale*."""
    return scale[max(scale.index(level), scale.index(floor))]


def security_floor(domains: list, risk: str, *, config_min: str = "L1",
                   signoff_domains: tuple = ()) -> tuple:
    """Return ``(minimum security level, reasons)`` for this run.

    Args:
        domains: Sensitive domains the objective touches.
        risk: The (applied) risk tier — ``critical`` raises the floor.
        config_min: Operator-configured global minimum.
        signoff_domains: Domains that require human sign-off (L4).

    Returns:
        The floor level and the human-readable reasons that set it.
    """
    floor, reasons = config_min, [f"config minimum {config_min}"]
    if domains:
        floor = raise_to("L2", floor, SECURITY_LEVELS)
        reasons.append(f"sensitive domains {domains} -> L2")
    deep = [d for d in domains if d in _DEEP_SECURITY_DOMAINS]
    if deep or risk == "critical":
        floor = raise_to("L3", floor, SECURITY_LEVELS)
        reasons.append(f"{deep or 'critical risk'} -> L3")
    signoff = [d for d in domains if d in signoff_domains]
    if signoff:
        floor = "L4"
        reasons.append(f"sign-off domains {signoff} -> L4")
    return floor, reasons


def review_floor(domains: list, risk: str, *, config_min: str = "light") -> str:
    """Return the minimum review depth for this run."""
    floor = config_min
    if domains or risk in ("high", "critical"):
        floor = raise_to("standard", floor, REVIEW_LEVELS)
    if risk == "critical":
        floor = "strict"
    return floor
