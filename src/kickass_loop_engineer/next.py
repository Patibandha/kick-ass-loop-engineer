"""The ``next`` envelope state machine — skill composition for the session loop.

The engine cannot invoke Claude Code skills; the session can.  ``loop-engineer
next`` reads the run's artifacts on disk and emits exactly ONE JSON envelope
telling the session which skill / agent / engine-command to dispatch next.

Session loop (spec §4)::

    next → dispatch (Skill tool / Agent tool / Bash / one question) → the skill
    writes ``expects.artifact`` → next validates the artifact deterministically
    → repeat.  Validation-failure rule: a missing/invalid artifact re-emits the
    same envelope with ``retry: N`` (max 2 retries), then emits ``ask_user`` with
    the failure context — never a silent skip.

Design properties: the stage is **derived from artifacts**, never from a
transcript or fragile stored state; re-entry is resumable after a crash; routing
is deterministic.  The only persisted state is a tiny retry ledger at
``.loop-engineer/next.json`` (``{"stage", "attempts", "skipped"}``) which the
engine never touches — a stage change resets ``attempts``.

Stage derivation order: ``research → plan → engine → engine-failure-ask_user →
review → qa → security → ship → terminal``.  ``research``, ``plan`` and
``review`` are mandatory; ``research`` is the honesty gate — its substantive
failures (a load-bearing ``[ASSUMED]`` or a fabricated citation) end the run in
a ``RESEARCH_BLOCKED`` terminal, never ``ask_user``.  ``plan``/``review`` with
nothing available → ``ask_user``; ``qa``/``security``/``ship`` are optional
(explicitly skipped and named in the terminal envelope when unresolvable).

ask_user question key
---------------------
The question of an ``ask_user`` envelope is exposed at ``next_action.question``
(canonical) **and** mirrored at the top-level ``question`` key; both carry the
identical string so a consumer may read either.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

from .artifacts import ARTIFACT_SCHEMAS, validate_artifact
from .cursor import PipelineCursor
from .decision.staffing import load_plan
from .research import (check_citations, check_tag_coverage, parse_research,
                       select_citations)
from .router import SkillRouter, StageRoute
from .terminal import TerminalState

logger = logging.getLogger("kickass_loop_engineer.next")

_PROTOCOL = 1
_STATE_MD = ".loop-engineer/STATE.md"
_LEDGER_REL = os.path.join(".loop-engineer", "next.json")

_MANDATORY = ("plan", "review")
_OPTIONAL = ("qa", "security", "ship")
_POST_ENGINE_STAGES = ("review", "qa", "security", "ship")
_STAGE_SCHEMAS = {
    "plan": "plan_v1",
    "review": "review_v1",
    "qa": "qa_v1",
    "security": "security_v1",
    "ship": "ship_v1",
}
# Attempts 1..3 dispatch (retry field on 2 and 3); the 4th arrival asks the user.
_MAX_RETRIES = 2
# Staffing-plan fields surfaced on the terminal envelope.
_PLAN_SUMMARY_KEYS = ("risk", "security", "review", "qa", "devops", "domains")
# Global backstop on the research sub-machine: the per-substage ``attempts``
# budgets reset whenever the ledger stage flips between "research" and
# "research_citations", so a session that alternates an incomplete and a
# mismatched citations.md would never exhaust either budget. This ceiling counts
# EVERY non-terminal research round (dispatch or verify_citations) across both
# substages and blocks once exceeded — an unattended run can't spin forever.
_MAX_RESEARCH_ROUNDS = 6


def emit_next(workspace: str, available: Optional[set] = None, objective: str = "") -> dict:
    """Derive the session stage from artifacts and emit ONE next-action envelope.

    Args:
        workspace: Workspace root; artifacts live under ``<workspace>/.loop-engineer/``.
        available: Set of installed skill/agent leading name segments (e.g.
            ``{"gsd-planner", "superpowers", "gstack"}``).  ``None`` means the
            stage owners are assumed available (absent ``--available`` flag).
        objective: Objective goal string threaded into dispatch / run_engine args.

    Returns:
        A single envelope dict.  ``next_action.type`` is one of ``invoke_skill``,
        ``invoke_agent``, ``run_engine``, ``ask_user`` or ``terminal``.
    """
    router = SkillRouter()
    cursor_data = PipelineCursor(workspace).load()
    ledger = _load_ledger(workspace)

    # 0. research (mandatory, FIRST) — no build proceeds on unverified facts.
    #    Returns an envelope to emit, or None to fall through to the plan check.
    research_env = _research_stage(workspace, router, available, ledger,
                                   cursor_data, objective)
    if research_env is not None:
        return research_env

    # 1. plan (mandatory) — nothing valid on disk yet means the run hasn't started.
    ok, reason = validate_artifact(workspace, "plan_v1")
    if not ok:
        return _dispatch_stage(workspace, "plan", "plan_v1", reason, router,
                               available, ledger, cursor_data, objective,
                               mandatory="plan" in _MANDATORY)

    # 2. engine — build+verify happen inside `loop-engineer run`; a terminal
    #    cursor is the machine's proof the engine finished.
    if cursor_data is None or cursor_data.get("stage") != "terminal":
        return _engine_dispatch(workspace, ledger, cursor_data, objective)

    # 3. engine terminal but not success → the user's call in M2.
    if cursor_data.get("status") != "success":
        outcome = cursor_data.get("detail", {}).get("outcome", {})
        detail_reason = outcome.get("reason", "")
        status = cursor_data.get("status", "ask_user")
        question = (f"The engine run ended in '{status}': {detail_reason}. "
                    "How should I proceed?")
        return _ask_user_envelope("engine", question, cursor_data.get("run_id", ""),
                                  _cursor_evidence(cursor_data), status=status)

    # 4. review → qa → security → ship. A 3.2 staffing plan (written by the
    #    engine, already clamped by quality floors) may skip optional stages
    #    and may require human sign-off before ship.
    plan = load_plan(workspace)
    staffed_out = _staffing_skips(plan)
    for stage in _POST_ENGINE_STAGES:
        if stage == "ship" and _needs_signoff(workspace, plan):
            return _ask_user_envelope(
                "signoff",
                "Security sign-off required (L4) for sensitive domains "
                f"{plan.get('domains', [])}. Review .loop-engineer/security.md "
                "and the change; reply APPROVE to continue (the session then "
                "writes '.loop-engineer/signoff.md' with 'APPROVED: <name>') "
                "or describe what must change.",
                cursor_data.get("run_id", ""), _cursor_evidence(cursor_data),
                status="signoff_required")
        if stage in ledger["skipped"] or stage in staffed_out:
            continue
        schema = _STAGE_SCHEMAS[stage]
        ok, reason = validate_artifact(workspace, schema)
        if ok:
            continue
        if stage in _OPTIONAL and available is not None:
            route = router.resolve(stage, available=available)
            if route.name.split(":")[0] not in available:
                ledger["skipped"].append(stage)
                _save_ledger(workspace, ledger)
                continue
        return _dispatch_stage(workspace, stage, schema, reason, router,
                               available, ledger, cursor_data, objective,
                               mandatory=stage in _MANDATORY)

    # 5. every stage valid or explicitly skipped → terminal.
    skipped = ledger["skipped"] + [s for s in staffed_out if s not in ledger["skipped"]]
    envelope = _terminal_envelope(cursor_data.get("run_id", ""),
                                  _cursor_evidence(cursor_data), skipped,
                                  _failed_verdicts(workspace, skipped))
    if plan is not None:
        envelope["staffing"] = {"skipped_by_plan": sorted(staffed_out),
                                **{k: plan.get(k) for k in _PLAN_SUMMARY_KEYS}}
    return envelope


# --------------------------------------------------------------------------- #
# Research sub-machine
# --------------------------------------------------------------------------- #
def _research_stage(workspace: str, router: SkillRouter, available: Optional[set],
                    ledger: dict, cursor_data: Optional[dict],
                    objective: str) -> Optional[dict]:
    """Derive the research sub-state from artifacts; emit an envelope or ``None``.

    Research is FIRST and mandatory.  The engine is network-free: the research
    sweep is a skill/agent dispatch, and the citation re-fetch is a
    ``verify_citations`` round the session executes.  This function only reads
    disk and compares deterministically.

    Two-part gate:

    * **Shape + tag coverage** — ``research_v1`` (a ``## Decisions`` section and a
      tagged claim) AND :func:`~kickass_loop_engineer.research.check_tag_coverage`
      (no untagged bullet, no load-bearing ``[ASSUMED]``).  Either failing
      re-dispatches the research skill under ledger stage ``"research"``;
      retry-exhaustion → ``RESEARCH_BLOCKED`` (never ``ask_user``).
    * **Citation spot-check** — once coverage passes, the selected citable claims
      are compared against ``citations.md``.  ``incomplete`` emits a
      ``verify_citations`` round (ledger stage ``"research_citations"``);
      ``mismatch`` re-dispatches research to fix the fabricated citation (ledger
      stage ``"research"``).  Both exhaust to ``RESEARCH_BLOCKED``.

    A global ``research_rounds`` counter in the ledger backstops the per-substage
    budgets: because those reset whenever the ledger stage flips between the two
    substages, an oscillating session could evade both — so every non-terminal
    research round is counted and the run blocks once it exceeds
    :data:`_MAX_RESEARCH_ROUNDS`, independent of the per-substage ``attempts``.

    Returns:
        An envelope to emit, or ``None`` when research is complete (no citable
        claims, or every citation verified) so the caller advances to plan.
    """
    run_id = cursor_data.get("run_id", "") if cursor_data else ""
    path = os.path.join(workspace, ".loop-engineer", "RESEARCH.md")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        text = ""

    shape_ok, shape_reason = validate_artifact(workspace, "research_v1")
    coverage_ok, coverage_reason = check_tag_coverage(text)
    if not shape_ok or not coverage_ok:
        reason = shape_reason if not shape_ok else coverage_reason
        blocked = _research_round_guard(workspace, ledger, run_id)
        if blocked is not None:
            return blocked
        return _research_dispatch(workspace, router, available, ledger,
                                  cursor_data, objective, reason)

    # Coverage passes → spot-check the citable claims against the session fetch.
    selected = select_citations(parse_research(text))
    if not selected:
        return None  # nothing citable → research done, advance to plan.

    citations_path = os.path.join(workspace, ".loop-engineer", "citations.md")
    try:
        with open(citations_path, "r", encoding="utf-8", errors="replace") as handle:
            citations_md = handle.read()
    except OSError:
        citations_md = ""

    result = check_citations(selected, citations_md)
    if result.status == "ok":
        return None  # every citation verified → research done.
    if result.status == "incomplete":
        blocked = _research_round_guard(workspace, ledger, run_id)
        if blocked is not None:
            return blocked
        attempts = _register_attempt(workspace, ledger, "research_citations")
        if attempts - 1 > _MAX_RETRIES:
            return _research_blocked_envelope(run_id, result.detail, attempts - 1)
        envelope = _verify_citations_envelope(run_id, selected, workspace)
        return _annotate_retry(envelope, attempts, result.detail)
    # mismatch → a fabricated/stale citation; re-dispatch research to fix it.
    blocked = _research_round_guard(workspace, ledger, run_id)
    if blocked is not None:
        return blocked
    return _research_dispatch(workspace, router, available, ledger,
                              cursor_data, objective, result.detail)


def _research_round_guard(workspace: str, ledger: dict, run_id: str) -> Optional[dict]:
    """Count one non-terminal research round; block past the global ceiling.

    Increments and persists ``ledger["research_rounds"]`` (the global,
    substage-independent counter) and returns a ``RESEARCH_BLOCKED`` terminal
    once it exceeds :data:`_MAX_RESEARCH_ROUNDS`, else ``None``.  Called
    immediately before emitting any non-terminal research envelope so an
    oscillating session — one that flips the ledger substage every round and so
    keeps resetting the per-substage ``attempts`` budgets — still terminates.
    """
    ledger["research_rounds"] = ledger.get("research_rounds", 0) + 1
    _save_ledger(workspace, ledger)
    if ledger["research_rounds"] > _MAX_RESEARCH_ROUNDS:
        return _research_blocked_envelope(
            run_id,
            f"research did not converge after {_MAX_RESEARCH_ROUNDS} rounds",
            attempted=ledger["research_rounds"])
    return None


def _research_dispatch(workspace: str, router: SkillRouter, available: Optional[set],
                       ledger: dict, cursor_data: Optional[dict], objective: str,
                       reason: str) -> dict:
    """Emit a research skill/agent dispatch; exhaustion → ``RESEARCH_BLOCKED``.

    Mirrors :func:`_dispatch_stage` for the ``research`` stage / ``research_v1``
    schema, but research is mandatory and never asks the user: an unresolvable
    owner still dispatches the owner by name (so the session installs/uses it),
    and the retry budget exhausts to a ``RESEARCH_BLOCKED`` terminal instead of
    an ``ask_user``.
    """
    run_id = cursor_data.get("run_id", "") if cursor_data else ""
    evidence = _cursor_evidence(cursor_data)
    owner = router.owner("research")
    resolve_avail = available if available is not None else {owner.name.split(":")[0]}
    route = router.resolve("research", available=resolve_avail)
    # Research is mandatory: an unresolvable route still dispatches the owner so
    # the session installs/uses it — we never ask_user for a missing research skill.
    if available is not None and route.name.split(":")[0] not in available:
        route = owner

    attempts = _register_attempt(workspace, ledger, "research")
    if attempts - 1 > _MAX_RETRIES:
        return _research_blocked_envelope(run_id, reason, attempts - 1)

    spec = ARTIFACT_SCHEMAS["research_v1"]
    fallback = _fallback_route(router, "research")
    action_type = "invoke_skill" if route.kind == "skill" else "invoke_agent"
    envelope = {
        "protocol": _PROTOCOL, "run_id": run_id, "stage": "research", "status": "dispatch",
        "next_action": {
            "type": action_type,
            "name": route.name,
            "kind": route.kind,
            "fallback": fallback.name if fallback else None,
            "args": {"workspace": workspace, "objective": objective},
            "expects": {"artifact": spec["artifact"], "schema": "research_v1"},
        },
        "evidence": evidence,
        "state_md": _STATE_MD,
    }
    return _annotate_retry(envelope, attempts, reason)


def _verify_citations_envelope(run_id: str, selected: list, workspace: str) -> dict:
    """Build the ``verify_citations`` envelope for the session's re-fetch round.

    The session WebFetches each ``url`` and writes a ``## <url>`` section holding
    the fetched text into ``.loop-engineer/citations.md``; ``next`` then
    deterministically compares the claimed ``quote`` against it.
    """
    return {
        "protocol": _PROTOCOL, "run_id": run_id, "stage": "research", "status": "dispatch",
        "next_action": {
            "type": "verify_citations",
            "name": "web-fetch",
            "args": {
                "workspace": workspace,
                "citations": [{"url": c.url, "quote": c.quote} for c in selected],
            },
            "expects": {"artifact": ".loop-engineer/citations.md",
                        "schema": "citations_v1"},
        },
        "evidence": [],
        "state_md": _STATE_MD,
    }


def _research_blocked_envelope(run_id: str, reason: str, attempted: int) -> dict:
    """Build the ``RESEARCH_BLOCKED`` terminal envelope (the honest dead-end)."""
    return {
        "protocol": _PROTOCOL, "run_id": run_id, "stage": "research",
        "status": TerminalState.RESEARCH_BLOCKED.value,
        "next_action": {"type": "terminal"},
        "reason": reason,
        "attempted": attempted,
        "evidence": [],
        "state_md": _STATE_MD,
    }


# --------------------------------------------------------------------------- #
# Envelope builders
# --------------------------------------------------------------------------- #
def _dispatch_stage(workspace: str, stage: str, schema: str, reason: str,
                    router: SkillRouter, available: Optional[set], ledger: dict,
                    cursor_data: Optional[dict], objective: str, *,
                    mandatory: bool) -> dict:
    """Emit a skill/agent dispatch envelope, applying the retry→ask_user rule.

    Args:
        mandatory: True for stages that may never be skipped.  An optional
            stage must be skip-checked by the caller before dispatch; reaching
            here unresolvable with ``mandatory=False`` is a programming error
            and raises rather than nagging the user.

    Raises:
        RuntimeError: If an optional stage arrives with no available route
            (the caller's skip-check was missed).
    """
    run_id = cursor_data.get("run_id", "") if cursor_data else ""
    evidence = _cursor_evidence(cursor_data)
    owner = router.owner(stage)
    resolve_avail = available if available is not None else {owner.name.split(":")[0]}
    route = router.resolve(stage, available=resolve_avail)

    # No installed skill/agent: mandatory → ask the user to install one;
    # optional → the caller should have skipped it, so fail loudly.
    if available is not None and route.name.split(":")[0] not in available:
        if not mandatory:
            raise RuntimeError(
                f"optional stage {stage!r} reached dispatch unresolvable — "
                "the caller must skip-check optional stages first")
        fallback = _fallback_route(router, stage)
        options = owner.name + (f" or {fallback.name}" if fallback else "")
        question = (f"no skill available for mandatory stage '{stage}'; "
                    f"install one of: {options}")
        return _ask_user_envelope(stage, question, run_id, evidence)

    attempts = _register_attempt(workspace, ledger, stage)
    exhausted = _retry_exhausted(attempts, stage,
                                 f"stage '{stage}' artifact is still invalid",
                                 reason, run_id, evidence)
    if exhausted is not None:
        return exhausted

    spec = ARTIFACT_SCHEMAS[schema]
    fallback = _fallback_route(router, stage)
    action_type = "invoke_skill" if route.kind == "skill" else "invoke_agent"
    envelope = {
        "protocol": _PROTOCOL, "run_id": run_id, "stage": stage, "status": "dispatch",
        "next_action": {
            "type": action_type,
            "name": route.name,
            "kind": route.kind,
            "fallback": fallback.name if fallback else None,
            "args": {"workspace": workspace, "objective": objective},
            "expects": {"artifact": spec["artifact"], "schema": schema},
        },
        "evidence": evidence,
        "state_md": _STATE_MD,
    }
    return _annotate_retry(envelope, attempts, reason)


def _engine_dispatch(workspace: str, ledger: dict, cursor_data: Optional[dict],
                     objective: str) -> dict:
    """Emit the ``run_engine`` envelope, applying the retry→ask_user rule.

    An engine dispatch only counts as an ATTEMPT when a cursor exists and is
    non-terminal (a real crashed/stuck run).  A cursor-absent workspace is the
    first dispatch — a session restart before the engine ran never burns a
    retry toward ask_user.
    """
    if cursor_data is None:
        run_id = ""
        reason = "no engine cursor yet — the run has not started"
        attempts = 1
    else:
        run_id = cursor_data.get("run_id", "")
        reason = f"engine cursor stage is {cursor_data.get('stage')!r}, not 'terminal'"
        attempts = _register_attempt(workspace, ledger, "engine")

    exhausted = _retry_exhausted(attempts, "engine",
                                 "the engine has not reached a terminal state",
                                 reason, run_id, [])
    if exhausted is not None:
        return exhausted

    args = {"workspace": workspace}
    if objective:
        args["objective"] = objective
    envelope = {
        "protocol": _PROTOCOL, "run_id": run_id, "stage": "engine", "status": "dispatch",
        "next_action": {
            "type": "run_engine",
            "name": "loop-engineer run",
            "args": args,
            "expects": {"artifact": ".loop-engineer/pipeline.json",
                        "schema": "engine_terminal"},
        },
        "evidence": [],
        "state_md": _STATE_MD,
    }
    return _annotate_retry(envelope, attempts, reason)


def _retry_exhausted(attempts: int, stage: str, verb: str, reason: str,
                     run_id: str, evidence: list) -> Optional[dict]:
    """Return the ask_user envelope when *attempts* exceeds the retry budget.

    Args:
        attempts: The attempt count for this arrival (1 on the first).
        stage: Stage name for the envelope.
        verb: Stage-specific failure phrasing (e.g. "stage 'review' artifact
            is still invalid").
        reason: The validation failure detail.
        run_id: Run identifier for the envelope.
        evidence: Evidence list for the envelope.

    Returns:
        An ``ask_user`` envelope on the arrival past the last retry
        (initial + ``_MAX_RETRIES``), else ``None``.
    """
    if attempts - 1 > _MAX_RETRIES:
        question = (f"{verb} after {_MAX_RETRIES} retries: {reason}. "
                    "How should I proceed?")
        return _ask_user_envelope(stage, question, run_id, evidence)
    return None


def _annotate_retry(envelope: dict, attempts: int, reason: str) -> dict:
    """Stamp ``retry``/``validation_error`` onto a re-emitted dispatch envelope.

    Attempts 2 and 3 carry ``retry: attempts-1`` plus the validation failure;
    the first attempt passes through untouched.
    """
    if attempts >= 2:
        envelope["retry"] = attempts - 1
        envelope["validation_error"] = reason
    return envelope


def _ask_user_envelope(stage: str, question: str, run_id: str, evidence: list,
                       status: str = "ask_user") -> dict:
    """Build an ``ask_user`` envelope (question at both ``next_action`` and top level)."""
    return {
        "protocol": _PROTOCOL, "run_id": run_id, "stage": stage, "status": status,
        "next_action": {"type": "ask_user", "question": question},
        "question": question,
        "evidence": evidence,
        "state_md": _STATE_MD,
    }


def _terminal_envelope(run_id: str, evidence: list, skipped: list,
                       failed_verdicts: list) -> dict:
    """Build the terminal envelope; ``failed_verdicts`` keeps 'complete' honest."""
    return {
        "protocol": _PROTOCOL, "run_id": run_id, "stage": "terminal", "status": "complete",
        "next_action": {"type": "terminal"},
        "evidence": evidence,
        "skipped_stages": list(skipped),
        "failed_verdicts": failed_verdicts,
        "state_md": _STATE_MD,
    }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _staffing_skips(plan: Optional[dict]) -> set:
    """Optional stages the staffing plan decided not to pay for."""
    if not plan:
        return set()
    skips = set()
    if plan.get("qa") is False:
        skips.add("qa")
    if plan.get("security") == "L1":
        skips.add("security")
    if plan.get("devops") is False:
        skips.add("ship")
    return skips


def _needs_signoff(workspace: str, plan: Optional[dict]) -> bool:
    """True when the plan demands L4 sign-off and no valid sign-off exists."""
    if not plan or plan.get("security") != "L4":
        return False
    ok, _reason = validate_artifact(workspace, "signoff_v1")
    return not ok


def _fallback_route(router: SkillRouter, stage: str) -> Optional[StageRoute]:
    """Return the fallback route for *stage*, or ``None`` when it has none.

    Derived through the public router API: resolving against an empty
    availability set yields the fallback when one is defined, else the owner.
    """
    owner = router.owner(stage)
    resolved = router.resolve(stage, available=set())
    return resolved if resolved != owner else None


def _cursor_evidence(cursor_data: Optional[dict]) -> list:
    """Return the terminal run's evidence, or ``[]`` when the cursor isn't terminal."""
    if cursor_data and cursor_data.get("stage") == "terminal":
        return cursor_data.get("detail", {}).get("outcome", {}).get("evidence", [])
    return []


def _failed_verdicts(workspace: str, skipped: list) -> list:
    """List qa/security stages whose (present) artifact text contains ``VERDICT: FAIL``."""
    failed = []
    for stage in ("qa", "security"):
        if stage in skipped:
            continue
        spec = ARTIFACT_SCHEMAS[_STAGE_SCHEMAS[stage]]
        path = os.path.join(workspace, spec["artifact"])
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                if "VERDICT: FAIL" in handle.read():
                    failed.append(stage)
        except OSError:
            continue
    return failed


def _register_attempt(workspace: str, ledger: dict, stage: str) -> int:
    """Increment (or reset on stage change) the retry counter and persist it.

    Returns:
        The attempt count for *stage* after this arrival (1 on the first).
    """
    if ledger.get("stage") == stage:
        ledger["attempts"] = ledger.get("attempts", 0) + 1
    else:
        ledger["stage"] = stage
        ledger["attempts"] = 1
    _save_ledger(workspace, ledger)
    return ledger["attempts"]


def _load_ledger(workspace: str) -> dict:
    """Load the retry ledger, tolerating an absent or corrupt file with a fresh one."""
    path = os.path.join(workspace, _LEDGER_REL)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return {
            "stage": str(data.get("stage", "")),
            "attempts": int(data.get("attempts", 0)),
            "skipped": [str(s) for s in data.get("skipped", [])],
            "research_rounds": int(data.get("research_rounds", 0)),
        }
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError, TypeError):
        return {"stage": "", "attempts": 0, "skipped": [], "research_rounds": 0}


def _save_ledger(workspace: str, ledger: dict) -> None:
    """Atomically persist the retry ledger (engine never touches this file)."""
    directory = os.path.join(workspace, ".loop-engineer")
    path = os.path.join(workspace, _LEDGER_REL)
    payload = {"stage": ledger.get("stage", ""),
               "attempts": ledger.get("attempts", 0),
               "skipped": ledger.get("skipped", []),
               "research_rounds": ledger.get("research_rounds", 0)}
    try:
        os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(tmp, path)
    except OSError:
        logger.exception("could not persist next.json retry ledger")
