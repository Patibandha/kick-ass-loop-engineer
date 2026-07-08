"""Unit tests for the ``next`` envelope state machine (Task 4)."""
import json
import os

from kickass_loop_engineer.cursor import PipelineCursor
from kickass_loop_engineer.next import emit_next


def _write(ws, rel, text):
    path = os.path.join(ws, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


# A citation-free, coverage-passing RESEARCH.md: a [VERIFIED] Decisions claim
# with no (source: …) clause → select_citations returns empty → the research
# sub-machine auto-advances with no verify_citations round.
_RESEARCH_DONE = "# RESEARCH\n## Decisions\n- [VERIFIED] no external deps\n"


def _seed_research(ws):
    _write(ws, ".loop-engineer/RESEARCH.md", _RESEARCH_DONE)


def _seed_plan(ws):
    _seed_research(ws)
    _write(ws, ".loop-engineer/plan.md", "# Plan\n- [ ] Task 1: do the thing\n")


def _seed_engine_terminal(ws, status="success", reason="ok",
                          evidence=None, run_id="run-42"):
    if evidence is None:
        evidence = [{"gate": "unit", "passed": True}]
    cursor = PipelineCursor(ws)
    cursor.start(run_id=run_id, objective_goal="g")
    cursor.set_stage("terminal", status,
                     {"outcome": {"state": status, "reason": reason,
                                  "evidence": evidence}})


def _ledger(ws):
    with open(os.path.join(ws, ".loop-engineer", "next.json"), encoding="utf-8") as handle:
        return json.load(handle)


def test_first_call_dispatches_plan(tmp_path):
    ws = str(tmp_path)
    _seed_research(ws)  # research done → the machine reaches the plan check
    env = emit_next(ws, available={"gsd-planner"})
    assert env["stage"] == "plan"
    assert env["next_action"]["type"] == "invoke_agent"
    assert env["next_action"]["name"] == "gsd-planner"
    assert env["next_action"]["expects"]["schema"] == "plan_v1"
    # Only superpowers installed → resolve falls back to the writing-plans skill.
    _seed_research(str(tmp_path / "b"))
    env2 = emit_next(str(tmp_path / "b"), available={"superpowers"})
    assert env2["next_action"]["type"] == "invoke_skill"
    assert env2["next_action"]["name"] == "superpowers:writing-plans"


def test_plan_valid_dispatches_engine(tmp_path):
    ws = str(tmp_path)
    _seed_plan(ws)
    env = emit_next(ws, available={"gsd-planner"})
    assert env["stage"] == "engine"
    assert env["next_action"]["type"] == "run_engine"
    assert env["next_action"]["name"] == "loop-engineer run"
    assert env["next_action"]["expects"] == {
        "artifact": ".loop-engineer/pipeline.json", "schema": "engine_terminal"}
    assert env["run_id"] == ""


def test_objective_threads_into_run_engine_args(tmp_path):
    ws = str(tmp_path)
    _seed_plan(ws)
    env = emit_next(ws, available={"gsd-planner"}, objective="build X")
    assert env["next_action"]["type"] == "run_engine"
    assert env["next_action"]["args"]["objective"] == "build X"


def test_engine_success_dispatches_review(tmp_path):
    ws = str(tmp_path)
    _seed_plan(ws)
    _seed_engine_terminal(ws, status="success", run_id="run-7")
    env = emit_next(ws, available={"gsd-code-reviewer"})
    assert env["stage"] == "review"
    assert env["next_action"]["type"] == "invoke_agent"
    assert env["next_action"]["name"] == "gsd-code-reviewer"
    assert env["run_id"] == "run-7"
    assert env["evidence"] == [{"gate": "unit", "passed": True}]


def test_engine_failure_asks_user(tmp_path):
    ws = str(tmp_path)
    _seed_plan(ws)
    _seed_engine_terminal(ws, status="stalled", reason="hit budget cap",
                          evidence=[{"gate": "unit", "passed": False}])
    env = emit_next(ws, available={"gsd-code-reviewer"})
    assert env["next_action"]["type"] == "ask_user"
    assert env["status"] == "stalled"
    assert "hit budget cap" in env["next_action"]["question"]
    assert "hit budget cap" in env["question"]
    assert env["evidence"] == [{"gate": "unit", "passed": False}]


def test_invalid_artifact_retries_then_asks_user(tmp_path):
    ws = str(tmp_path)
    _seed_plan(ws)
    _seed_engine_terminal(ws)
    _write(ws, ".loop-engineer/review.md", "looks good to me\n")  # invalid marker
    avail = {"gsd-code-reviewer"}
    e1 = emit_next(ws, available=avail)
    assert e1["stage"] == "review" and "retry" not in e1
    e2 = emit_next(ws, available=avail)
    assert e2["retry"] == 1 and "marker" in e2["validation_error"]
    e3 = emit_next(ws, available=avail)
    assert e3["retry"] == 2
    e4 = emit_next(ws, available=avail)
    assert e4["next_action"]["type"] == "ask_user"
    assert e4["stage"] == "review"


def test_stage_change_resets_attempts(tmp_path):
    ws = str(tmp_path)
    _seed_research(ws)  # research done → derivation reaches the plan check
    _write(ws, ".loop-engineer/plan.md", "prose only, no checkbox\n")  # invalid
    avail = {"gsd-planner", "gsd-code-reviewer"}
    emit_next(ws, available=avail)
    emit_next(ws, available=avail)
    assert _ledger(ws) == {"stage": "plan", "attempts": 2, "skipped": [],
                           "research_rounds": 0}
    _seed_plan(ws)  # now valid → derivation advances past plan
    _seed_engine_terminal(ws)
    env = emit_next(ws, available=avail)
    assert env["stage"] == "review"
    assert "retry" not in env
    # Attempts RESTARTED at 1 for the new stage, not 0. (The engine stage is
    # exempt: a cursor-absent engine dispatch never registers an attempt.)
    assert _ledger(ws) == {"stage": "review", "attempts": 1, "skipped": [],
                           "research_rounds": 0}


def test_engine_attempts_only_counted_with_stale_cursor(tmp_path):
    ws = str(tmp_path)
    _seed_plan(ws)
    # Cursor absent: every call is a first dispatch — never burns a retry.
    for _ in range(3):
        env = emit_next(ws, available={"gsd-planner"})
        assert env["next_action"]["type"] == "run_engine"
        assert "retry" not in env
    # Cursor exists mid-run (non-terminal): a real crashed/stuck run counts.
    cursor = PipelineCursor(ws)
    cursor.start(run_id="run-9", objective_goal="g")
    cursor.set_stage("decompose", "running")
    retries = []
    for _ in range(3):
        env = emit_next(ws, available={"gsd-planner"})
        assert env["next_action"]["type"] == "run_engine"
        retries.append(env.get("retry"))
    assert retries == [None, 1, 2]
    env = emit_next(ws, available={"gsd-planner"})
    assert env["next_action"]["type"] == "ask_user"
    assert env["stage"] == "engine"


def test_review_stage_unavailable_asks_user(tmp_path):
    ws = str(tmp_path)
    _seed_plan(ws)
    _seed_engine_terminal(ws)
    env = emit_next(ws, available={"gsd-planner"})  # no gsd-code-reviewer, no gstack
    assert env["next_action"]["type"] == "ask_user"
    assert env["stage"] == "review"
    assert "review" in env["question"]


def test_skipped_stages_persist_across_calls(tmp_path):
    ws = str(tmp_path)
    _seed_plan(ws)
    _seed_engine_terminal(ws)
    minimal = {"gsd-planner", "gsd-code-reviewer"}  # no gstack, no security-auditor
    first = emit_next(ws, available=minimal)
    assert first["stage"] == "review"
    _write(ws, ".loop-engineer/review.md", "NO FINDINGS\n")
    second = emit_next(ws, available=minimal)
    assert second["next_action"]["type"] == "terminal"
    assert set(second["skipped_stages"]) == {"qa", "security", "ship"}
    assert set(_ledger(ws)["skipped"]) == {"qa", "security", "ship"}
    # available=None would dispatch qa if skips were not re-read from the ledger.
    third = emit_next(ws)
    assert third["next_action"]["type"] == "terminal"
    assert set(third["skipped_stages"]) == {"qa", "security", "ship"}


def test_corrupt_ledger_starts_fresh(tmp_path):
    ws = str(tmp_path)
    _seed_research(ws)  # research done → derivation reaches the plan check
    _write(ws, ".loop-engineer/next.json", "{not valid json!!\n")
    env = emit_next(ws, available={"gsd-planner"})
    assert env["protocol"] == 1
    assert env["stage"] == "plan"
    assert env["next_action"]["type"] == "invoke_agent"
    assert _ledger(ws) == {"stage": "plan", "attempts": 1, "skipped": [],
                           "research_rounds": 0}


def test_terminal_lists_failed_verdicts(tmp_path):
    ws = str(tmp_path)
    _seed_plan(ws)
    _seed_engine_terminal(ws)
    _write(ws, ".loop-engineer/review.md", "NO FINDINGS\n")
    _write(ws, ".loop-engineer/qa.md", "VERDICT: FAIL\n")  # valid shape, failing verdict
    _write(ws, ".loop-engineer/security.md", "VERDICT: PASS\n")
    _write(ws, ".loop-engineer/ship.md", "SHIPPED: done\n")
    env = emit_next(ws)
    assert env["next_action"]["type"] == "terminal"
    assert env["status"] == "complete"
    assert env["failed_verdicts"] == ["qa"]


def test_optional_stage_skipped_when_unavailable(tmp_path):
    ws = str(tmp_path)
    _seed_plan(ws)
    _seed_engine_terminal(ws)
    _write(ws, ".loop-engineer/review.md", "NO FINDINGS\n")
    env = emit_next(ws, available={"gsd-planner", "gsd-code-reviewer"})
    assert env["next_action"]["type"] == "terminal"
    assert set(env["skipped_stages"]) == {"qa", "security", "ship"}


def test_all_artifacts_valid_is_terminal(tmp_path):
    ws = str(tmp_path)
    _seed_plan(ws)
    _seed_engine_terminal(ws)
    _write(ws, ".loop-engineer/review.md", "NO FINDINGS\n")
    _write(ws, ".loop-engineer/qa.md", "VERDICT: PASS\n")
    _write(ws, ".loop-engineer/security.md", "VERDICT: PASS\n")
    _write(ws, ".loop-engineer/ship.md", "SHIPPED: done\n")
    env = emit_next(ws)
    assert env["next_action"]["type"] == "terminal"
    assert env["status"] == "complete"
    assert env["skipped_stages"] == []
    assert env["failed_verdicts"] == []
    assert env["evidence"] == [{"gate": "unit", "passed": True}]


def test_mandatory_stage_unavailable_asks_user(tmp_path):
    ws = str(tmp_path)
    _seed_research(ws)  # research done → the plan check runs and finds no skill
    env = emit_next(ws, available=set())
    assert env["next_action"]["type"] == "ask_user"
    assert env["stage"] == "plan"
    assert "plan" in env["question"]


def test_owner_assumed_available_when_available_none(tmp_path):
    ws = str(tmp_path)
    _seed_research(ws)  # research done → the plan owner is dispatched next
    env = emit_next(ws, available=None)
    assert env["stage"] == "plan"
    assert env["next_action"]["name"] == "gsd-planner"
    assert env["next_action"]["type"] == "invoke_agent"


# --------------------------------------------------------------------------- #
# Research sub-machine (2.0-M4 Task 6a) — research runs FIRST, ahead of plan.
# --------------------------------------------------------------------------- #
_ASSUMED_DECISION = "# RESEARCH\n## Decisions\n- [ASSUMED] Use Postgres 16\n"
_CITED_DECISION = (
    "# RESEARCH\n## Decisions\n"
    '- [CITED] Piper runs on CPU (source: https://p.example "does not require a GPU")\n'
)


def test_research_dispatched_first_when_research_missing(tmp_path):
    ws = str(tmp_path)
    # Plan already present, but research is missing → research still goes first.
    _write(ws, ".loop-engineer/plan.md", "# Plan\n- [ ] Task 1: do the thing\n")
    env = emit_next(ws, available={"deep-research", "gsd-planner"})
    assert env["stage"] == "research"
    assert env["next_action"]["type"] == "invoke_skill"
    assert env["next_action"]["name"] == "deep-research"
    assert env["next_action"]["expects"]["schema"] == "research_v1"
    assert env["next_action"]["expects"]["artifact"] == ".loop-engineer/RESEARCH.md"
    assert "retry" not in env


def test_research_load_bearing_assumed_blocks_after_retries(tmp_path):
    ws = str(tmp_path)
    _write(ws, ".loop-engineer/RESEARCH.md", _ASSUMED_DECISION)
    avail = {"deep-research"}
    e1 = emit_next(ws, available=avail)
    assert e1["stage"] == "research"
    assert e1["next_action"]["type"] == "invoke_skill"
    assert "retry" not in e1
    e2 = emit_next(ws, available=avail)
    assert e2["retry"] == 1 and "load-bearing" in e2["validation_error"]
    e3 = emit_next(ws, available=avail)
    assert e3["retry"] == 2
    e4 = emit_next(ws, available=avail)
    assert e4["next_action"]["type"] == "terminal"
    assert e4["status"] == "research_blocked"
    assert "load-bearing" in e4["reason"] and "assumed" in e4["reason"].lower()


def test_research_with_citations_emits_verify_citations(tmp_path):
    ws = str(tmp_path)
    _write(ws, ".loop-engineer/RESEARCH.md", _CITED_DECISION)
    env = emit_next(ws, available={"deep-research"})
    assert env["stage"] == "research"
    assert env["next_action"]["type"] == "verify_citations"
    assert env["next_action"]["args"]["citations"] == [
        {"url": "https://p.example", "quote": "does not require a GPU"}]
    assert env["next_action"]["expects"]["artifact"] == ".loop-engineer/citations.md"
    assert env["next_action"]["expects"]["schema"] == "citations_v1"


def test_research_citation_mismatch_blocks(tmp_path):
    ws = str(tmp_path)
    _write(ws, ".loop-engineer/RESEARCH.md", _CITED_DECISION)
    _write(ws, ".loop-engineer/citations.md",
           "## https://p.example\ntotally unrelated content\n")
    avail = {"deep-research"}
    e1 = emit_next(ws, available=avail)
    assert e1["stage"] == "research"
    assert e1["next_action"]["type"] == "invoke_skill"  # re-dispatch research to fix
    e2 = emit_next(ws, available=avail)
    assert e2["retry"] == 1
    e3 = emit_next(ws, available=avail)
    assert e3["retry"] == 2
    e4 = emit_next(ws, available=avail)
    assert e4["next_action"]["type"] == "terminal"
    assert e4["status"] == "research_blocked"
    assert "p.example" in e4["reason"]


def test_research_complete_advances_to_plan(tmp_path):
    ws = str(tmp_path)
    _seed_research(ws)  # coverage passes, no citable claims
    env = emit_next(ws, available={"deep-research", "gsd-planner"})
    assert env["stage"] == "plan"
    assert env["next_action"]["name"] == "gsd-planner"


def test_no_research_needed_doc_advances(tmp_path):
    ws = str(tmp_path)
    _write(ws, ".loop-engineer/RESEARCH.md",
           "# RESEARCH\n## Decisions\n\n## Notes\n- [ASSUMED] trivial\n")
    env = emit_next(ws, available={"deep-research", "gsd-planner"})
    assert env["stage"] == "plan"


def test_research_global_ceiling_stops_oscillation(tmp_path):
    # A session that alternates an *incomplete* (absent) citations.md and a
    # *mismatched* (wrong-quote) one flips the ledger stage every round, so the
    # per-substage retry budgets keep resetting and never exhaust. The global
    # research-round ceiling must still terminate the loop.
    from kickass_loop_engineer.next import _MAX_RESEARCH_ROUNDS
    ws = str(tmp_path)
    _write(ws, ".loop-engineer/RESEARCH.md", _CITED_DECISION)
    cit_path = os.path.join(ws, ".loop-engineer", "citations.md")
    avail = {"deep-research"}
    env = None
    saw_blocked = False
    for i in range(_MAX_RESEARCH_ROUNDS + 1):
        if i % 2 == 0:
            if os.path.exists(cit_path):
                os.remove(cit_path)  # → incomplete → verify_citations round
        else:
            _write(ws, ".loop-engineer/citations.md",
                   "## https://p.example\ntotally unrelated content\n")  # → mismatch
        env = emit_next(ws, available=avail)
        if env["status"] == "research_blocked":
            saw_blocked = True
            break
    assert saw_blocked, "global ceiling never tripped — oscillation spins forever"
    assert env["next_action"]["type"] == "terminal"
    assert str(_MAX_RESEARCH_ROUNDS) in env["reason"]


def test_research_incomplete_path_blocks_after_retries(tmp_path):
    ws = str(tmp_path)
    _write(ws, ".loop-engineer/RESEARCH.md", _CITED_DECISION)
    # citations.md is NEVER written → every round is 'incomplete' →
    # verify_citations retries under stage research_citations, then blocks.
    avail = {"deep-research"}
    e1 = emit_next(ws, available=avail)
    assert e1["next_action"]["type"] == "verify_citations"
    e2 = emit_next(ws, available=avail)
    assert e2["retry"] == 1
    e3 = emit_next(ws, available=avail)
    assert e3["retry"] == 2
    e4 = emit_next(ws, available=avail)
    assert e4["next_action"]["type"] == "terminal"
    assert e4["status"] == "research_blocked"
    assert "p.example" in e4["reason"]


def test_research_dispatch_forces_owner_when_deep_research_unavailable(tmp_path):
    ws = str(tmp_path)
    _write(ws, ".loop-engineer/RESEARCH.md", _ASSUMED_DECISION)  # coverage fails
    # Neither owner (deep-research) nor fallback (research-analyst) installed:
    # research is mandatory → dispatch the owner by name, never ask_user.
    env = emit_next(ws, available={"gsd-planner"})
    assert env["stage"] == "research"
    assert env["next_action"]["type"] == "invoke_skill"
    assert env["next_action"]["name"] == "deep-research"
