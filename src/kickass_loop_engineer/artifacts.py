"""Per-stage artifact schemas and deterministic validators for the ``next`` protocol.

A validator behaves like a gate: file exists, is non-empty, and carries the
schema's required marker (plus any extra ``require`` patterns) — same input,
same verdict, no model judgment.
"""
from __future__ import annotations

import os
import re

# schema -> {artifact: workspace-relative path, marker: compiled regex, hint: str}
ARTIFACT_SCHEMAS: dict = {
    "plan_v1": {"artifact": ".loop-engineer/plan.md",
                "marker": re.compile(r"^\s*- \[[ xX]\] ", re.MULTILINE),
                "hint": "at least one '- [ ] …' task checkbox line"},
    "review_v1": {"artifact": ".loop-engineer/review.md",
                  "marker": re.compile(r"^(FINDING:|NO FINDINGS)", re.MULTILINE),
                  "hint": "'FINDING: …' lines or exactly 'NO FINDINGS'"},
    "qa_v1": {"artifact": ".loop-engineer/qa.md",
              "marker": re.compile(r"^VERDICT: (PASS|FAIL)", re.MULTILINE),
              "hint": "a 'VERDICT: PASS|FAIL' line"},
    "security_v1": {"artifact": ".loop-engineer/security.md",
                    "marker": re.compile(r"^VERDICT: (PASS|FAIL)", re.MULTILINE),
                    "hint": "a 'VERDICT: PASS|FAIL' line"},
    "ship_v1": {"artifact": ".loop-engineer/ship.md",
                "marker": re.compile(r"^(SHIPPED:|BLOCKED:)", re.MULTILINE),
                "hint": "a 'SHIPPED: …' or 'BLOCKED: …' line"},
    # research_v1 validates SHAPE only: a '## Decisions' section plus at least
    # one provenance-tagged claim line anywhere in the doc. The SUBSTANTIVE
    # gate — no load-bearing [ASSUMED] under ## Decisions, and the citation
    # spot-check — is research.check_tag_coverage / research.check_citations,
    # run by the `next` machine (2.0-M4 Task 6), not by validate_artifact.
    # The require regex mirrors research.py's `_CLAIM`/`_BULLET` leniency
    # (bullet markers [-*+], optional leading indent) so a `* [VERIFIED]`
    # doc passes this shape gate too.
    "research_v1": {"artifact": ".loop-engineer/RESEARCH.md",
                    "marker": re.compile(r"^##\s+decisions", re.MULTILINE | re.IGNORECASE),
                    "require": (re.compile(r"^\s*[-*+]\s+\[(VERIFIED|CITED|ASSUMED)\]\s",
                                            re.MULTILINE),),
                    "hint": ("a '## Decisions' section and at least one "
                             "[VERIFIED|CITED|ASSUMED] claim")},
    "citations_v1": {"artifact": ".loop-engineer/citations.md",
                     "marker": re.compile(r"^##\s+\S+", re.MULTILINE),
                     "hint": "at least one '## <url>' fetched-source section"},
}


def validate_artifact(workspace: str, schema: str) -> tuple[bool, str]:
    """Deterministically validate a stage artifact against *schema*.

    Args:
        workspace: Workspace root the artifact path is relative to.
        schema: A key of :data:`ARTIFACT_SCHEMAS`.

    Returns:
        ``(ok, reason)`` — ``reason`` is empty on success and names the exact
        failure (unknown schema / missing / empty / marker absent) otherwise.
    """
    spec = ARTIFACT_SCHEMAS.get(schema)
    if spec is None:
        return False, f"unknown schema {schema!r}; known: {sorted(ARTIFACT_SCHEMAS)}"
    path = os.path.join(workspace, spec["artifact"])
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except FileNotFoundError:
        return False, f"artifact missing: {spec['artifact']}"
    except OSError as exc:
        return False, f"artifact unreadable: {spec['artifact']} ({exc})"
    if not text.strip():
        return False, f"artifact empty: {spec['artifact']}"
    if not spec["marker"].search(text):
        return False, (f"artifact invalid: {spec['artifact']} lacks required marker — "
                        f"expected {spec['hint']}")
    # Optional extra AND-ed patterns (no-op for schemas lacking the key).
    for extra in spec.get("require", ()):
        if not extra.search(text):
            return False, (f"artifact invalid: {spec['artifact']} lacks required marker — "
                            f"expected {spec['hint']}")
    return True, ""
