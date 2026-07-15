"""Read-only audit mode: a chunked reviewer sweep that produces findings.md.

Audit is bug FINDING, never bug fixing — and read-only, precisely defined: the
only file this module writes is ``<workspace>/.loop-engineer/findings.md``.
No source-tree writes, no worktrees, no promote. The selected files are
chunked by file up to a per-chunk byte budget, each chunk goes to the reviewer
in ONE call (parsed with the shared ``FINDING:`` parser via
``Reviewer.review``), and every finding carries a file reference. Findings are
ADVISORY: only the read-only gates this module runs (registry categories
``security``/``data_leak`` — never unit/ui/architecture/performance/smoke,
which may build or mutate) make pass/fail claims.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Callable, List, Optional, Tuple

from .gates import GATES, GateSpec
from .objective import Objective
from .repomap import (
    DEFAULT_SELECT_BUDGET_BYTES,
    _read_text,
    tracked_files,
)
from .verifier import GatesResult, run_gates

logger = logging.getLogger("kickass_loop_engineer.audit")

_ARTIFACT_DIR = ".loop-engineer"
_FINDINGS_FILENAME = "findings.md"

#: Gate registry categories that are safe to run during a read-only audit.
READ_ONLY_CATEGORIES = frozenset({"security", "data_leak"})

#: Per-chunk byte budget for grouping selected files into reviewer calls.
DEFAULT_CHUNK_BUDGET_BYTES = 24576

#: File-selection budget for an audit sweep — deliberately larger than the
#: enhance-mode default (``repomap.DEFAULT_SELECT_BUDGET_BYTES``): an audit
#: reads broadly, a build brief must stay small.
AUDIT_SELECT_BUDGET_BYTES = 4 * DEFAULT_SELECT_BUDGET_BYTES

AUDIT_GOAL = (
    "AUDIT (read-only): examine the files below for concrete defects — bugs, "
    "security vulnerabilities, secret/PII leaks, and correctness problems. "
    "Report each defect as its own 'FINDING: <file path>: <defect>' line, "
    "referencing the exact file path. Do not propose or write fixes."
)

AUDIT_DONE_WHEN = (
    "every concrete defect in the audited files is reported as a FINDING "
    "line carrying its file path"
)

_ADVISORY_HEADER = (
    "# Audit findings\n"
    "\n"
    "> ADVISORY: the findings below are reviewer observations, each with a\n"
    "> file reference — they make no pass/fail claim. Only the executable\n"
    "> gate results at the end of this report make pass/fail claims.\n"
)


def chunk_files(
    files: List[Tuple[str, str]],
    chunk_budget_bytes: int = DEFAULT_CHUNK_BUDGET_BYTES,
) -> List[List[Tuple[str, str]]]:
    """Group ``(path, content)`` pairs into per-reviewer-call chunks.

    Chunking is deliberately simple and deterministic: input order is
    preserved, files are packed greedily until the next file would overflow
    the per-chunk byte budget, then a new chunk starts. A single file larger
    than the budget still gets a chunk of its own — it is never split or
    dropped.

    Args:
        files: Selected ``(path, content)`` tuples, in selection order.
        chunk_budget_bytes: UTF-8 byte budget per chunk.

    Returns:
        A list of chunks, each a non-empty list of ``(path, content)`` tuples;
        empty when *files* is empty.
    """
    chunks: List[List[Tuple[str, str]]] = []
    current: List[Tuple[str, str]] = []
    remaining = chunk_budget_bytes
    for path, content in files:
        size = len(content.encode("utf-8"))
        if current and size > remaining:
            chunks.append(current)
            current = []
            remaining = chunk_budget_bytes
        current.append((path, content))
        remaining -= size
    if current:
        chunks.append(current)
    return chunks


def fallback_files(root: str, budget_bytes: int) -> List[Tuple[str, str]]:
    """ALL tracked text files under *root*, greedily packed into the budget.

    The zero-files fallback: when keyword scoring selects nothing (a vague
    objective like "audit this repo"), the audit still sweeps — every tracked
    file, in git's deterministic ``ls-files`` order, up to *budget_bytes*.
    Mirrors ``repomap.select_files`` packing: a file whose content would
    overflow the remaining budget is skipped (never truncated) and packing
    continues; binary and unreadable files are skipped.

    Args:
        root: Git repo root (or any directory inside one).
        budget_bytes: Total UTF-8 byte budget across returned contents.

    Returns:
        ``(path, content)`` tuples; empty for a non-repo.
    """
    selected: List[Tuple[str, str]] = []
    remaining = budget_bytes
    for path in tracked_files(root):
        content = _read_text(root, path)
        if content is None:
            continue
        size = len(content.encode("utf-8"))
        if size > remaining:
            continue
        selected.append((path, content))
        remaining -= size
    return selected


def read_only_gates(gates: List[GateSpec]) -> List[GateSpec]:
    """Filter configured gates down to the read-only categories, neutralized.

    Only gates whose REGISTRY category is ``security`` or ``data_leak`` may
    run during an audit — unit/ui/architecture/performance/smoke gates may
    build or mutate the tree. The kept specs are returned as copies with
    ``prove=False`` (proof-of-test stashes and pops the working tree — a
    mutation, and meaningless on the clean tree an audit runs against) and
    ``observe=""`` (the failure observer runs an arbitrary allowlisted
    command; the read-only guarantee wins over diagnostics here).

    Args:
        gates: The run's configured ``GateSpec`` list.

    Returns:
        Neutralized copies of the read-only gates, in configured order.
    """
    kept: List[GateSpec] = []
    for spec in gates:
        category = GATES.get(spec.name, {}).get("category", "")
        if category in READ_ONLY_CATEGORIES:
            kept.append(GateSpec(name=spec.name, command=spec.command,
                                 prove=False, observe=""))
    return kept


def _chunk_snapshot(repo_map_text: str, chunk: List[Tuple[str, str]]) -> str:
    """Render one chunk's reviewer snapshot: orientation map + full files."""
    parts: List[str] = []
    if repo_map_text:
        parts.append(f"REPO MAP (orientation only):\n{repo_map_text}")
    parts.append("FILES UNDER AUDIT:")
    parts.extend(f"--- {path} ---\n{content}" for path, content in chunk)
    return "\n\n".join(parts)


def _mentions(text: str, path: str) -> bool:
    """True iff *text* names *path* as a whole reference, not a substring.

    Boundary-aware on purpose: ``a.py`` must not match inside ``data.py``
    (lookbehind rejects a preceding word/path character) nor inside
    ``sub/a.py`` or ``a.pyx`` (lookbehind/lookahead reject ``/`` and word
    characters). Ordinary punctuation after the path — ``a.py:``, ``a.py,``,
    a sentence-final ``a.py.`` — still matches.
    """
    pattern = r"(?<![\w./-])" + re.escape(path) + r"(?![\w-])"
    return re.search(pattern, text) is not None


def _attribute(text: str, chunk_paths: List[str]) -> dict:
    """Attach a file reference to one parsed finding.

    A finding already naming one of the chunk's paths (a boundary-aware
    match — see :func:`_mentions`) keeps its text and is filed under that
    path; when several genuinely appear, the longest (most specific) path
    wins, ties broken by path for determinism. A finding naming none is
    prefixed with the chunk's file reference (the chunk's paths,
    comma-joined) so every findings.md entry carries a reference.
    """
    for path in sorted(chunk_paths, key=lambda p: (-len(p), p)):
        if _mentions(text, path):
            return {"file": path, "text": text}
    ref = ", ".join(chunk_paths)
    return {"file": ref, "text": f"{ref}: {text}"}


def _always_continue() -> bool:
    """Default ``budget_check``: never stops the sweep (back-compat)."""
    return True


def _render(findings: List[dict], gates_result: Optional[GatesResult],
            file_count: int, chunk_count: int, truncated: bool = False) -> str:
    """Render the deterministic findings.md text.

    Structure: advisory header, run context line, an optional truncation note
    (when the budget stopped the sweep mid-way), per-file findings sections
    (first-appearance order — chunk order, then finding order), and the gate
    results section listing only the read-only gates that actually ran.
    """
    lines: List[str] = [_ADVISORY_HEADER]
    lines.append(f"Files audited: {file_count} "
                 f"(in {chunk_count} reviewer chunk"
                 f"{'' if chunk_count == 1 else 's'}).\n")
    if truncated:
        lines.append(
            "> TRUNCATED: the run budget was reached mid-sweep — not every "
            "reviewer chunk ran, so the findings below are PARTIAL.\n")

    lines.append("## Findings (advisory)\n")
    if findings:
        by_file: dict = {}
        for finding in findings:
            by_file.setdefault(finding["file"], []).append(finding["text"])
        for ref, texts in by_file.items():
            lines.append(f"### {ref}\n")
            lines.extend(f"- {text}" for text in texts)
            lines.append("")
    else:
        lines.append("No findings.\n")

    lines.append("## Gate results (read-only: security/data_leak)\n")
    if gates_result is None:
        lines.append("No read-only gates configured — this audit makes no "
                     "pass/fail claims.")
    else:
        for record in gates_result.records:
            verdict = "PASS" if record.passed else "FAIL"
            lines.append(f"- {verdict} {record.gate} — `{record.command}` "
                         f"({record.evidence})")
    lines.append("")
    return "\n".join(lines)


def run_audit(
    reviewer,
    repo_map_text: str,
    files: List[Tuple[str, str]],
    gates: List[GateSpec],
    workspace: str,
    policy=None,
    on_usage: Optional[Callable] = None,
    budget_check: Callable[[], bool] = _always_continue,
) -> Tuple[str, List[dict]]:
    """Run the read-only audit sweep and render the findings report.

    One reviewer call per chunk (see :func:`chunk_files`): each call sees the
    repo map for orientation plus the chunk's FULL file contents, and its
    reply is parsed with the shared ``FINDING:`` parser (through the
    reviewer's own ``review`` path). Every completed reviewer call is
    reported to *on_usage* for cost accounting. Before EACH chunk's reviewer
    call, *budget_check* is consulted: it returns ``True`` to continue or
    ``False`` to stop the sweep cleanly — no exception crosses this boundary
    (the orchestrator's private budget-cap exception stays private). On a stop
    the findings gathered so far are still rendered (with a truncation note)
    and returned. After the sweep, only the read-only configured gates run
    (see :func:`read_only_gates`), in the main workspace — the tree is never
    mutated.

    Args:
        reviewer: A ``Reviewer``/``CrossModelReviewer`` exposing
            ``review(objective, snapshot) -> Review``.
        repo_map_text: Rendered ``repomap.repo_map`` orientation brief
            (may be empty).
        files: Pre-selected ``(path, content)`` tuples to audit.
        gates: The run's configured ``GateSpec`` list (filtered here).
        workspace: The main workspace root; the report lands under its
            ``.loop-engineer/`` directory.
        policy: Optional ``VerificationPolicy`` threaded to the gate runs.
        on_usage: Optional callback receiving the ``ProviderResult`` of every
            completed reviewer call (the M1 money invariant).
        budget_check: Zero-arg predicate consulted before each chunk's
            reviewer call; a ``False`` return stops the sweep and renders the
            partial findings with a truncation note. Defaults to always
            continuing (back-compat).

    Returns:
        ``(findings_md_path, findings)`` — the absolute report path and the
        advisory findings, each a ``{"file": ref, "text": text}`` dict.

    Raises:
        ProviderError: Propagated from a failed reviewer call (the caller
            maps it to a terminal outcome).
    """
    objective = Objective(goal=AUDIT_GOAL, done_when=AUDIT_DONE_WHEN)
    chunks = chunk_files(files)
    findings: List[dict] = []
    truncated = False
    for chunk in chunks:
        if not budget_check():
            truncated = True
            break
        review = reviewer.review(objective, _chunk_snapshot(repo_map_text, chunk))
        if on_usage is not None:
            on_usage(review.result)
        chunk_paths = [path for path, _content in chunk]
        findings.extend(_attribute(text, chunk_paths)
                        for text in review.findings)

    ro_specs = read_only_gates(gates)
    gates_result = (run_gates(ro_specs, workspace, policy=policy)
                    if ro_specs else None)

    report = _render(findings, gates_result,
                     file_count=len(files), chunk_count=len(chunks),
                     truncated=truncated)
    out_dir = os.path.join(workspace, _ARTIFACT_DIR)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, _FINDINGS_FILENAME)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(report)
    logger.info("audit report written: %s (%d findings)", path, len(findings))
    return path, findings
