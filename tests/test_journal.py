"""D2 journal enrichment: events.jsonl reconstructs a full pipeline run.

A fixture 2-slice run (fakes throughout, one failing-then-passing attempt on
the first slice) must journal ``attempt_started``, ``gate_result``,
``observer_ran``, ``format_retry``, and ``cost_recorded`` events, and a
test-local reconstruction helper (deliberately NOT shipped code) replays the
file into an ordered timeline that reconciles with the run's outcome and the
cost ledger.

Replay tolerance (documented journal contract): ``events.jsonl`` is
append-only and never deduplicated — a RESUMED run re-announces durable facts,
so ``mode_resolved`` may legitimately appear twice for one run_id. Consumers
must tolerate repeats; the reconstruction helper here does.
"""
import json
import os

import pytest

from kickass_loop_engineer import orchestrator
from kickass_loop_engineer.agents import Builder, Reviewer
from kickass_loop_engineer.cursor import PipelineCursor
from kickass_loop_engineer.ensemble import default_specs
from kickass_loop_engineer.ledger import CostLedger
from kickass_loop_engineer.memory import Memory
from kickass_loop_engineer.objective import Objective
from kickass_loop_engineer.orchestrator import GateSpec, Orchestrator
from kickass_loop_engineer.playbook import Playbook
from kickass_loop_engineer.terminal import TerminalState
from kickass_loop_engineer.verifier import EvidenceRecord, GatesResult, ProofRecord

from helpers import FakeProvider
from test_orchestrator import FILE_BLOCK, TWO_SLICE_JSON, FakeNotifier, FakeWorktrees

_OBSERVED = "AssertionError: expected 2 got 1"


class _ScriptedGates:
    """Deterministic ``run_gates`` stand-in: fail the scripted calls, pass the rest.

    A failing call returns a single failed record carrying observer output
    (mimicking verifier.run_gates, where the observer runs on failure); a
    passing call returns one PROVEN record per gate spec.
    """

    def __init__(self, fail_calls=()):
        self.calls = 0
        self.fail_calls = set(fail_calls)

    def __call__(self, specs, workspace, policy=None):
        self.calls += 1
        first = specs[0]
        if self.calls in self.fail_calls:
            return GatesResult(records=[EvidenceRecord(
                gate=first.name, command=first.command, passed=False,
                evidence="exit=1", observed=_OBSERVED)])
        proof = ProofRecord(gate_cmd=first.command, green_before=True,
                            red_when_reverted=True, green_after=True)
        return GatesResult(records=[EvidenceRecord(
            gate=s.name, command=s.command, passed=True,
            evidence="exit=0", proof=proof) for s in specs])


def _make_orchestrator(tmp_path, builder_provider, reviewer_provider,
                       mode="build", reviewer=None):
    workspace = str(tmp_path / "ws")
    os.makedirs(workspace, exist_ok=True)
    cursor = PipelineCursor(workspace)
    ledger = CostLedger(path=str(tmp_path / "ledger.json"), month="2026-07")
    orch = Orchestrator(
        objective=Objective(goal="build a thing", done_when="pytest passes"),
        workspace=workspace,
        builder=Builder(builder_provider),
        reviewer=reviewer if reviewer is not None else Reviewer(reviewer_provider),
        worktrees=FakeWorktrees(str(tmp_path / "wts")),
        ledger=ledger,
        notifier=FakeNotifier(),
        memory=Memory(str(tmp_path / "mem.db")),
        playbook=Playbook(str(tmp_path / "pb.json")),
        cursor=cursor,
        gates=[GateSpec(prove=False)],
        ensemble_n=2,
        run_id="j1",
        architect_mode="off",
        mode=mode,
        attempt_specs=default_specs(2),
    )
    return orch, cursor, ledger


def _run_fixture_pipeline(tmp_path, monkeypatch):
    """Two slices, two attempts each; the FIRST slice's first attempt fails its
    gate (with observer output) and the second attempt needs one corrective
    format retry before passing — the run still ends SUCCESS."""
    monkeypatch.setattr(orchestrator, "run_gates", _ScriptedGates(fail_calls={1}))
    builder_provider = FakeProvider(
        [
            TWO_SLICE_JSON,        # decompose
            FILE_BLOCK,            # core attempt 0 (its gate fails)
            "prose, no fences",    # core attempt 1 -> corrective format retry
            FILE_BLOCK,            # core attempt 1 corrective call (gate passes)
            FILE_BLOCK,            # api attempt 0
            FILE_BLOCK,            # api attempt 1
        ],
        cost_usd=0.5,
    )
    reviewer_provider = FakeProvider(["NO FINDINGS", "NO FINDINGS"], cost_usd=0.25)
    orch, cursor, ledger = _make_orchestrator(
        tmp_path, builder_provider, reviewer_provider)
    outcome = orch.run()
    assert outcome.state is TerminalState.SUCCESS  # fixture sanity
    return outcome, cursor, ledger


# ---------------------------------------------------------------------------
# Reconstruction helper — TEST-LOCAL by design (the journal's consumer
# contract is "a dumb JSONL replay suffices"; shipping a reader would let the
# writer and reader drift together and hide envelope regressions).
# ---------------------------------------------------------------------------

def _replay(events_path):
    """Replay events.jsonl into an ordered timeline plus per-attempt records.

    Tolerates repeated events (e.g. ``mode_resolved`` re-announced on resume)
    — repeats are kept in the timeline, never an error.

    Returns:
        (timeline, attempts, costs) where ``attempts`` maps
        ``(slice, attempt_index)`` to ``{"spec": {...}, "gates": [...],
        "observer": [...], "retries": [...]}`` and ``costs`` is the ordered
        ``cost_recorded`` payload list.
    """
    with open(events_path, encoding="utf-8") as handle:
        timeline = [json.loads(line) for line in handle if line.strip()]
    attempts, costs = {}, []
    for entry in timeline:
        event, payload = entry["event"], entry["payload"]
        if event == "attempt_started":
            attempts[(payload["slice"], payload["attempt"])] = {
                "spec": {"model": payload["model"],
                         "temperature": payload["temperature"]},
                "gates": [], "observer": [], "retries": [],
            }
        elif event == "gate_result":
            attempts[(payload["slice"], payload["attempt"])]["gates"].append(payload)
        elif event == "observer_ran":
            attempts[(payload["slice"], payload["attempt"])]["observer"].append(payload)
        elif event == "format_retry" and payload.get("stage") == "build":
            attempts[(payload["slice"], payload["attempt"])]["retries"].append(payload)
        elif event == "cost_recorded":
            costs.append(payload)
    return timeline, attempts, costs


def test_journal_contains_all_five_event_types(tmp_path, monkeypatch):
    _, cursor, _ = _run_fixture_pipeline(tmp_path, monkeypatch)
    timeline, _, _ = _replay(cursor.events_path)
    kinds = {entry["event"] for entry in timeline}
    assert {"attempt_started", "gate_result", "observer_ran",
            "format_retry", "cost_recorded"} <= kinds


def test_attempt_started_carries_slice_index_and_spec(tmp_path, monkeypatch):
    _, cursor, _ = _run_fixture_pipeline(tmp_path, monkeypatch)
    _, attempts, _ = _replay(cursor.events_path)
    assert set(attempts) == {("core", 0), ("core", 1), ("api", 0), ("api", 1)}
    specs = default_specs(2)
    for (_slice, i), record in attempts.items():
        assert record["spec"]["model"], "every attempt announces its model"
        assert record["spec"]["temperature"] == specs[i].temperature


def test_gate_results_are_journaled_per_gate_per_attempt(tmp_path, monkeypatch):
    _, cursor, _ = _run_fixture_pipeline(tmp_path, monkeypatch)
    _, attempts, _ = _replay(cursor.events_path)
    for record in attempts.values():
        assert record["gates"], "every attempt carries at least one gate verdict"
    failing = attempts[("core", 0)]["gates"]
    assert [g["ok"] for g in failing] == [False]
    assert failing[0]["proven"] is None, "no proof rides on a failed gate"
    for key in (("core", 1), ("api", 0), ("api", 1)):
        for gate in attempts[key]["gates"]:
            assert gate["ok"] is True
            assert gate["proven"] is True
            assert gate["gate"] == "unit"


def test_observer_ran_marks_the_failing_attempt_with_truncation_flag(
        tmp_path, monkeypatch):
    _, cursor, _ = _run_fixture_pipeline(tmp_path, monkeypatch)
    timeline, attempts, _ = _replay(cursor.events_path)
    observed = [e for e in timeline if e["event"] == "observer_ran"]
    assert len(observed) == 1
    payload = observed[0]["payload"]
    assert payload["gate"] == "unit"
    assert payload["truncated"] is False  # _OBSERVED is far below OBSERVER_CAP
    assert attempts[("core", 0)]["observer"], "it rides on the failing attempt"


def test_format_retry_is_journaled_with_stage_and_attempt(tmp_path, monkeypatch):
    _, cursor, _ = _run_fixture_pipeline(tmp_path, monkeypatch)
    timeline, attempts, _ = _replay(cursor.events_path)
    retries = [e["payload"] for e in timeline if e["event"] == "format_retry"]
    assert len(retries) == 1
    assert retries[0]["stage"] == "build"
    assert attempts[("core", 1)]["retries"] == retries


def test_cost_events_reconcile_with_the_ledger_total(tmp_path, monkeypatch):
    _, cursor, ledger = _run_fixture_pipeline(tmp_path, monkeypatch)
    _, _, costs = _replay(cursor.events_path)
    # decompose + 4 attempt builds + 2 reviews — every ledger.record site
    # journals; the LEDGER stays the source of truth, the events are
    # observational and must reconcile with it.
    assert len(costs) == 7
    assert {c["site"] for c in costs} == {"decompose", "build_attempt", "review"}
    assert all(c["model"] for c in costs)
    assert sum(c["usd"] for c in costs) == pytest.approx(ledger.run_total())
    assert ledger.run_total() == pytest.approx(3.5)  # retry spend included


def test_gate_verdicts_match_the_outcome_evidence(tmp_path, monkeypatch):
    outcome, cursor, _ = _run_fixture_pipeline(tmp_path, monkeypatch)
    _, attempts, _ = _replay(cursor.events_path)
    # outcome.evidence = [final gates] + one winner GatesResult per slice; each
    # winner's journaled attempt must show the same all-green verdict.
    winner_evidence = outcome.evidence[1:]
    assert len(winner_evidence) == 2
    for evidence, slice_role in zip(winner_evidence, ("core", "api")):
        assert evidence["passed"] is True
        journaled_winners = [
            record for (role, _i), record in attempts.items()
            if role == slice_role and all(g["ok"] for g in record["gates"])
        ]
        assert journaled_winners, f"slice {slice_role} has a journaled winner"
        gate_names = {g["gate"] for w in journaled_winners for g in w["gates"]}
        assert gate_names == {g["gate"] for g in evidence["gates"]}


def test_timeline_orders_attempt_started_before_its_gate_results(
        tmp_path, monkeypatch):
    _, cursor, _ = _run_fixture_pipeline(tmp_path, monkeypatch)
    timeline, _, _ = _replay(cursor.events_path)
    started, verdicts = {}, {}
    for pos, entry in enumerate(timeline):
        payload = entry["payload"]
        if entry["event"] == "attempt_started":
            started[(payload["slice"], payload["attempt"])] = pos
        elif entry["event"] == "gate_result":
            verdicts.setdefault((payload["slice"], payload["attempt"]), pos)
    for key, verdict_pos in verdicts.items():
        assert started[key] < verdict_pos


def test_resume_reannounces_mode_resolved_and_replay_tolerates_it(
        tmp_path, monkeypatch):
    _run_fixture_pipeline(tmp_path, monkeypatch)
    # Second invocation of the SAME run: resumes the cursor, skips both
    # completed slices, and re-announces the resolved mode. The duplicate
    # mode_resolved line is the documented replay contract (M4 rider): the
    # journal is append-only, consumers tolerate repeats, no dedupe state.
    monkeypatch.setattr(orchestrator, "run_gates", _ScriptedGates())
    orch, cursor, _ = _make_orchestrator(
        tmp_path, FakeProvider([TWO_SLICE_JSON], cost_usd=0.5),
        FakeProvider([], cost_usd=0.25))
    outcome = orch.run()
    assert outcome.state is TerminalState.SUCCESS
    timeline, attempts, _ = _replay(cursor.events_path)  # replay must not choke
    modes = [e for e in timeline if e["event"] == "mode_resolved"]
    assert len(modes) == 2
    assert [e["payload"]["mode"] for e in modes] == ["build", "build"]
    assert set(attempts) == {("core", 0), ("core", 1), ("api", 0), ("api", 1)}


# ---------------------------------------------------------------------------
# Reviewer-side format retries: honest stage context. The one wired hook
# fires for BOTH slice reviews and audit sweeps (the retry loop lives in
# agents.Reviewer), so the event must say which one actually retried.
# ---------------------------------------------------------------------------

_MALFORMED_REVIEW = "the code seems mostly okay to me, a few loose thoughts"


def test_review_format_retry_carries_slice_context(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _ScriptedGates())
    builder_provider = FakeProvider(
        ["not json", FILE_BLOCK, FILE_BLOCK], cost_usd=0.5)
    reviewer_provider = FakeProvider(
        [_MALFORMED_REVIEW, "NO FINDINGS"], cost_usd=0.25)
    orch, cursor, _ = _make_orchestrator(
        tmp_path, builder_provider, reviewer_provider)
    outcome = orch.run()
    assert outcome.state is TerminalState.SUCCESS
    timeline, _, _ = _replay(cursor.events_path)
    retries = [e["payload"] for e in timeline if e["event"] == "format_retry"]
    assert retries == [{"stage": "review", "slice": "build"}]


def test_audit_format_retry_is_labeled_audit_not_review(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "repo_map",
                        lambda root, budget_chars=8000: "MAP")
    monkeypatch.setattr(orchestrator, "select_files",
                        lambda root, objective_text, budget_bytes=0:
                        [("a.py", "A = 1\n")])
    reviewer_provider = FakeProvider(
        [_MALFORMED_REVIEW, "NO FINDINGS"], cost_usd=0.25)
    orch, cursor, _ = _make_orchestrator(
        tmp_path, FakeProvider([]), reviewer_provider, mode="audit")
    outcome = orch.run()
    assert outcome.state is TerminalState.SUCCESS
    timeline, _, _ = _replay(cursor.events_path)
    retries = [e["payload"] for e in timeline if e["event"] == "format_retry"]
    assert retries == [{"stage": "audit"}]


def test_caller_prewired_reviewer_on_retry_is_not_clobbered(tmp_path):
    def sentinel():
        return None

    reviewer = Reviewer(FakeProvider(["NO FINDINGS"]), on_retry=sentinel)
    _make_orchestrator(tmp_path, FakeProvider([]), None, reviewer=reviewer)
    assert reviewer.on_retry is sentinel
