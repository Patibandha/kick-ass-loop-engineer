"""Tests for the live pipeline orchestrator.

Fakes throughout: FakeProvider-backed Builder/Reviewer, a FakeWorktrees that
creates real tmp dirs and records promote/remove, a FakeNotifier that records
events, and monkeypatched ``orchestrator.run_gates`` for deterministic gate
outcomes. Real CostLedger/Memory/Playbook/PipelineCursor against tmp_path.
"""
import json
import logging
import os
import subprocess

import pytest

from kickass_loop_engineer import orchestrator
from kickass_loop_engineer.agents import Builder, Reviewer
from kickass_loop_engineer.ensemble import AttemptSpec, default_specs
from kickass_loop_engineer.ledger import CostLedger
from kickass_loop_engineer.memory import Memory
from kickass_loop_engineer.objective import Objective
from kickass_loop_engineer.orchestrator import GateSpec, Orchestrator
from kickass_loop_engineer.playbook import Playbook
from kickass_loop_engineer.cursor import PipelineCursor
from kickass_loop_engineer.pricing import load_pricing
from kickass_loop_engineer.providers.base import ProviderError
from kickass_loop_engineer.review import CrossModelReviewer
from kickass_loop_engineer.reproduce import (
    FIX_MODE_RULES,
    REPRO_SYSTEM,
    ReproResult,
    repro_check_command,
)
from kickass_loop_engineer.terminal import TerminalState
from kickass_loop_engineer.verifier import EvidenceRecord, GatesResult, OBSERVER_CAP
from kickass_loop_engineer.worktree import WorktreeError, WorktreeManager

from helpers import FakePolicy, FakeProvider, FakeVisionProvider

FILE_BLOCK = "=== FILE: out.py ===\nVALUE = 1\n=== END FILE ===\n"
TWO_SLICE_JSON = (
    '[{"role": "core", "objective": "core objective oc"},'
    ' {"role": "api", "objective": "api objective oa"}]'
)
DESIGN_MD = (
    "## Components\n- core: storage\n- api: http surface\n\n"
    "## Architecture\n```mermaid\ngraph TD\n  api --> core\n```\n\n"
    "## Primary flow\n```mermaid\nsequenceDiagram\n  api->>core: call\n```\n\n"
    "## Decisions\n| decision | why | alternative |\n|---|---|---|\n| x | y | z |\n\n"
    "```yaml\nlayering:\n  layers: [api, core]\n  forbidden: []\n```\n"
)
_VALID_DIAGRAMS = lambda fence: ""  # injected validator: every fence is valid


def _systems(provider):
    return [system for (system, _user) in provider.calls]


def _decompose_prompts(provider):
    from kickass_loop_engineer.decompose import SLICE_SYSTEM
    return [user for (system, user) in provider.calls if system == SLICE_SYSTEM]


def _architect_calls(provider):
    from kickass_loop_engineer.architect import ARCHITECT_SYSTEM
    return [user for (system, user) in provider.calls if system == ARCHITECT_SYSTEM]


class FakeWorktrees:
    """Creates real tmp dirs for isolation; records promote/remove calls."""

    def __init__(self, base):
        self.base = base
        os.makedirs(base, exist_ok=True)
        self.created = []
        self.promoted = []
        self.removed = []
        self._n = 0

    def create(self, name):
        self._n += 1
        path = os.path.join(self.base, f"{name}-{self._n}")
        os.makedirs(path, exist_ok=True)
        self.created.append(path)
        return path

    def promote(self, src, dest):
        self.promoted.append((src, dest))
        return []

    def remove(self, path):
        self.removed.append(path)


class FakeNotifier:
    """Records every event passed to notify()."""

    def __init__(self):
        self.events = []

    def notify(self, event):
        self.events.append(event)
        return True


def _pass_gates(specs, workspace, policy=None):
    return GatesResult(records=[
        EvidenceRecord(gate=s.name, command=s.command, passed=True,
                       evidence=f"exit=0 {workspace}")
        for s in specs
    ])


def _fail_gates(specs, workspace, policy=None):
    # Fail-fast: only the first gate runs and it fails.
    first = specs[0]
    return GatesResult(records=[
        EvidenceRecord(gate=first.name, command=first.command, passed=False,
                       evidence="fail")
    ])


def _ws(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return str(ws)


def _make(tmp_path, *, builder, reviewer, notifier=None, ledger=None,
          objective=None, ensemble_n=1, run_id="r1", oscillation_window=2,
          cursor=None, memory=None, workspace=None, worktrees=None, gates=None,
          pricing=None, architect_mode="off", diagram_validator=_VALID_DIAGRAMS,
          policy=None, visual_cfg=None, visual_provider=None, mode="build",
          attempt_specs=None, builder_factory=None):
    workspace = workspace or _ws(tmp_path)
    memory = memory or Memory(str(tmp_path / "mem.db"))
    cursor = cursor or PipelineCursor(workspace)
    orch = Orchestrator(
        objective=objective or Objective(goal="build a thing", done_when="pytest passes"),
        workspace=workspace, builder=builder, reviewer=reviewer,
        worktrees=worktrees or FakeWorktrees(str(tmp_path / "wts")),
        ledger=ledger or CostLedger(path=str(tmp_path / "ledger.json"), month="2026-07"),
        notifier=notifier or FakeNotifier(), memory=memory,
        playbook=Playbook(str(tmp_path / "pb.json")),
        cursor=cursor, gates=gates or [GateSpec(prove=False)],
        ensemble_n=ensemble_n, run_id=run_id,
        oscillation_window=oscillation_window,
        pricing=pricing,
        architect_mode=architect_mode, diagram_validator=diagram_validator,
        policy=policy, visual_cfg=visual_cfg, visual_provider=visual_provider,
        mode=mode,
        attempt_specs=attempt_specs, builder_factory=builder_factory,
    )
    return orch


def test_happy_path_returns_success_with_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    notifier = FakeNotifier()
    memory = Memory(str(tmp_path / "mem.db"))
    workspace = _ws(tmp_path)
    cursor = PipelineCursor(workspace)
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        notifier=notifier, memory=memory, cursor=cursor, workspace=workspace,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert outcome.evidence
    assert len(orch.worktrees.promoted) == 1
    assert memory.get("contract.build") is not None
    assert memory.get("verifier.proven") == "true"
    persisted = json.loads(open(cursor.path, encoding="utf-8").read())
    assert persisted["stage"] == "terminal" and persisted["status"] == "success"
    assert any(e["stage"] == "terminal" and e["level"] == "info" for e in notifier.events)


def test_terminal_cursor_detail_contains_outcome(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    workspace = _ws(tmp_path)
    cursor = PipelineCursor(workspace)
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        cursor=cursor, workspace=workspace,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    persisted = json.loads(open(cursor.path, encoding="utf-8").read())
    assert persisted["stage"] == "terminal"
    assert persisted["detail"]["outcome"]["state"] == "success"
    assert persisted["detail"]["outcome"]["evidence"], "evidence rides in the cursor"


def test_no_passing_attempt_is_stalled(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _fail_gates)
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK, FILE_BLOCK])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        ensemble_n=2,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.STALLED
    assert orch.worktrees.promoted == []
    assert len(orch.worktrees.removed) == 2  # each created attempt cleaned up


def test_risky_slice_objective_escalates(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    notifier = FakeNotifier()
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["garbage"])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        notifier=notifier,
        objective=Objective(goal="deploy to prod", done_when="it is live"),
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.APPROVAL_REQUIRED
    assert any(e["level"] == "escalate" for e in notifier.events)


def test_ledger_cap_stops_run(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    ledger = CostLedger(path=str(tmp_path / "ledger.json"), run_cap_usd=0.01, month="2026-07")
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK, FILE_BLOCK], cost_usd=0.02)),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        ledger=ledger, ensemble_n=2,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.BUDGET_EXCEEDED


def test_repeated_findings_oscillate(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider([TWO_SLICE_JSON, FILE_BLOCK, FILE_BLOCK])),
        reviewer=Reviewer(FakeProvider(["FINDING: x", "FINDING: x"])),
        ensemble_n=1, oscillation_window=2,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.OSCILLATION


def test_final_gate_failure_is_blocked(tmp_path, monkeypatch):
    workspace = _ws(tmp_path)

    def dispatch_gates(specs, ws_arg, policy=None):
        if ws_arg != workspace:  # per-attempt gates pass, final gate fails
            return _pass_gates(specs, ws_arg, policy)
        first = specs[0]
        return GatesResult(records=[
            EvidenceRecord(gate=first.name, command=first.command, passed=False,
                           evidence="final failed")
        ])

    monkeypatch.setattr(orchestrator, "run_gates", dispatch_gates)
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        ensemble_n=1, workspace=workspace,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.BLOCKED
    assert "final failed" in outcome.reason


def test_gate_list_runs_per_attempt_and_finally(tmp_path, monkeypatch):
    workspace = _ws(tmp_path)
    specs = [GateSpec(name="unit", command="pytest -q", prove=False),
             GateSpec(name="smoke", command="make test", prove=False)]
    calls = []

    def recording_gates(received, ws_arg, policy=None):
        calls.append((list(received), ws_arg))
        return _pass_gates(received, ws_arg, policy)

    monkeypatch.setattr(orchestrator, "run_gates", recording_gates)
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        gates=specs, workspace=workspace,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    # One gate-list run per attempt (single slice, n=1) + the final run.
    assert len(calls) == 2
    for received, _ws_arg in calls:
        assert received == specs  # the FULL list, in order, every time
    assert calls[-1][1] == workspace, "final run gates the main workspace"
    assert calls[0][1] != workspace, "attempt run gates its worktree"


def test_failing_gate_list_stalls_slice(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _fail_gates)
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        gates=[GateSpec(name="unit", command="pytest -q", prove=False),
               GateSpec(name="smoke", command="make test", prove=False)],
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.STALLED
    assert orch.worktrees.promoted == []


def test_success_evidence_carries_gates_result_dicts(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider([TWO_SLICE_JSON, FILE_BLOCK, FILE_BLOCK])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS", "NO FINDINGS"])),
        gates=[GateSpec(name="unit", command="pytest -q", prove=False),
               GateSpec(name="smoke", command="make test", prove=False)],
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    # Final gates result first, then one entry per promoted slice.
    assert len(outcome.evidence) == 3
    for entry in outcome.evidence:
        assert set(entry) == {"passed", "gates"}
        assert entry["passed"] is True
        assert [g["gate"] for g in entry["gates"]] == ["unit", "smoke"]
    json.dumps(outcome.evidence)  # must stay JSON-serializable


def test_final_empty_gates_result_blocks_with_clear_reason(tmp_path, monkeypatch):
    workspace = _ws(tmp_path)

    def dispatch_gates(specs, ws_arg, policy=None):
        if ws_arg != workspace:  # attempts pass; the final run yields no records
            return _pass_gates(specs, ws_arg, policy)
        return GatesResult(records=[])

    monkeypatch.setattr(orchestrator, "run_gates", dispatch_gates)
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        workspace=workspace,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.BLOCKED
    assert outcome.reason == "no gates configured"


def test_resume_skips_completed_slices(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    workspace = _ws(tmp_path)
    seed = PipelineCursor(workspace)
    seed.start(run_id="r1", objective_goal="build a thing")
    seed.complete_slice("core")

    builder = Builder(FakeProvider([TWO_SLICE_JSON, FILE_BLOCK]))
    orch = _make(
        tmp_path,
        builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        ensemble_n=1, run_id="r1",
        objective=Objective(goal="build a thing", done_when="pytest passes"),
        cursor=PipelineCursor(workspace), workspace=workspace,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    prompts = [user for (_system, user) in builder.provider.calls]
    # generate_slices call + exactly one build call for the remaining "api" slice.
    assert len(prompts) == 2
    assert not any("core objective oc" in p for p in prompts)
    assert any("api objective oa" in p for p in prompts)


def test_reviewer_findings_feed_next_slice_feedback(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    builder = Builder(FakeProvider([TWO_SLICE_JSON, FILE_BLOCK, FILE_BLOCK]))
    orch = _make(
        tmp_path,
        builder=builder,
        reviewer=Reviewer(FakeProvider(["FINDING: use argparse", "NO FINDINGS"])),
        ensemble_n=1,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    prompts = [user for (_system, user) in builder.provider.calls]
    assert any("use argparse" in p for p in prompts)


def test_post_build_escalation_on_winner_footprint(tmp_path, monkeypatch):
    # `migrations/*` is an escalation-only glob: the Workspace WritePolicy
    # allows the write, so the file reaches files_written and the post-build
    # escalation on the winner's footprint (behavior 6) must catch it.
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    notifier = FakeNotifier()
    migration_block = ("=== FILE: migrations/0001_init.sql ===\n"
                       "CREATE TABLE t (id INTEGER);\n=== END FILE ===\n")
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", migration_block])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        notifier=notifier,
        objective=Objective(goal="add a schema file", done_when="pytest passes"),
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.APPROVAL_REQUIRED
    assert "protected path" in outcome.reason
    assert orch.worktrees.promoted == []
    assert orch.worktrees.removed == orch.worktrees.created
    assert any(e["level"] == "escalate" for e in notifier.events)


def test_autonomy_stage_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    workspace = _ws(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    cursor = PipelineCursor(workspace)
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        cursor=cursor, workspace=workspace,
    )
    orch.run()

    events = [json.loads(line)
              for line in open(cursor.events_path, encoding="utf-8")]
    autonomy = [e for e in events
                if e["event"] == "stage" and e["payload"].get("stage") == "autonomy"]
    assert len(autonomy) == 1
    # Fresh memory lacks verifier.proven, so requested L3 auto-drops to L2
    # (assisted) even though the workspace IS a git repo and a budget is set.
    assert autonomy[0]["payload"]["status"] == "assisted"
    assert "verifier_proven" in autonomy[0]["payload"]["gaps"]


def test_finish_records_playbook_and_auditor(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    memory = Memory(str(tmp_path / "mem.db"))
    workspace = _ws(tmp_path)
    cursor = PipelineCursor(workspace)
    goal = "build a thing"
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        memory=memory, cursor=cursor, workspace=workspace,
        objective=Objective(goal=goal, done_when="pytest passes"),
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert orch.playbook.is_promoted(goal) is False  # 1 success < promote_after
    lessons = json.loads(open(orch.playbook.path, encoding="utf-8").read())
    assert lessons[goal]["successes"] == 1
    persisted = json.loads(open(cursor.path, encoding="utf-8").read())
    assert persisted["detail"]["auditor"] in ("KEEP", "PIVOT", "RETIRE", "KILL")
    canonical = memory.query(status="canonical")
    assert {row["key"] for row in canonical} >= {"contract.build", "verifier.proven"}


def test_snapshot_contains_winner_file_contents(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    reviewer_provider = FakeProvider(["NO FINDINGS"])
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK])),
        reviewer=Reviewer(reviewer_provider),
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert len(reviewer_provider.calls) == 1
    _system, prompt = reviewer_provider.calls[0]
    assert "--- out.py ---" in prompt
    assert "VALUE = 1" in prompt


class RaisingProvider(FakeProvider):
    """Serves queued responses, then raises ProviderError when exhausted."""

    def complete(self, system, user):
        if not self.responses:
            raise ProviderError("ollama connection refused")
        return super().complete(system, user)


def test_provider_error_yields_blocked_outcome(tmp_path, monkeypatch):
    # Slice generation succeeds ("not json" -> fallback), then the build
    # round's provider call raises: the run must still end in one terminal
    # outcome via _finish, with the slice's worktrees cleaned up.
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    notifier = FakeNotifier()
    orch = _make(
        tmp_path,
        builder=Builder(RaisingProvider(["not json"])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        notifier=notifier,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.BLOCKED
    assert "provider failure" in outcome.reason
    assert orch.worktrees.removed == orch.worktrees.created
    assert orch.worktrees.promoted == []
    assert any(e["stage"] == "terminal" for e in notifier.events)


class PromoteFailingWorktrees(FakeWorktrees):
    """FakeWorktrees whose promote always raises WorktreeError."""

    def promote(self, src, dest):
        raise WorktreeError("copy failed: disk full")


# Per-call estimate for a gpt-5.5 call with a 100k/100k token split:
# (100_000 * 1.25 + 100_000 * 10.0) / 1e6 = $1.125.
_PAID_KWARGS = dict(model="gpt-5.5", tokens=200_000,
                    prompt_tokens=100_000, completion_tokens=100_000)


def test_pricing_estimate_trips_run_cap_for_paid_model(tmp_path, monkeypatch):
    # cost_usd=0.0 (subscription-style provider): only the pricing estimate
    # can make the cap bind.
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    ledger = CostLedger(path=str(tmp_path / "ledger.json"), run_cap_usd=0.01,
                        month="2026-07")
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK], cost_usd=0.0,
                                     **_PAID_KWARGS)),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        ledger=ledger, pricing=load_pricing(),
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.BUDGET_EXCEEDED
    assert ledger.run_total() > 0


def test_decompose_call_is_ledgered(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    ledger = CostLedger(path=str(tmp_path / "ledger.json"), month="2026-07")
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK], model="gpt-5.5",
                                     tokens=200, prompt_tokens=100,
                                     completion_tokens=100)),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"], model="qwen2.5")),
        ledger=ledger, pricing=load_pricing(),
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    per_call = (100 * 1.25 + 100 * 10.0) / 1_000_000
    # decompose + one build round; total > per_call proves decompose is in.
    assert ledger.run_total() == pytest.approx(2 * per_call)


def test_reviewer_call_is_ledgered(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    ledger = CostLedger(path=str(tmp_path / "ledger.json"), month="2026-07")
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK], model="qwen2.5",
                                     tokens=200, prompt_tokens=100,
                                     completion_tokens=100)),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"], **_PAID_KWARGS)),
        ledger=ledger, pricing=load_pricing(),
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    # The builder (qwen2.5) prices to $0: the total is exactly the review call.
    assert ledger.run_total() == pytest.approx(1.125)


def test_unknown_model_warns_exactly_once_per_run(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider([TWO_SLICE_JSON, FILE_BLOCK, FILE_BLOCK],
                                     model="mystery-model-xyz", tokens=500)),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS", "NO FINDINGS"],
                                       model="qwen2.5")),
        pricing=load_pricing(),
    )
    with caplog.at_level(logging.WARNING,
                         logger="kickass_loop_engineer.orchestrator"):
        outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    warnings = [r for r in caplog.records if "no pricing for model" in r.getMessage()]
    assert len(warnings) == 1  # 3 builder calls (decompose + 2 builds), ONE warning


def test_reported_cost_is_used_as_is_over_estimate(tmp_path, monkeypatch):
    # claude_code style: the provider reports real dollars; the pricing row
    # (which would estimate $1.125/call) must NOT override it.
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    ledger = CostLedger(path=str(tmp_path / "ledger.json"), month="2026-07")
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK], cost_usd=0.42,
                                     **_PAID_KWARGS)),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"], model="qwen2.5")),
        ledger=ledger, pricing=load_pricing(),
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert ledger.run_total() == pytest.approx(0.84)  # 2 calls x $0.42, as reported


def test_no_pricing_table_uses_reported_cost_only(tmp_path, monkeypatch):
    # pricing=None: behavior identical to before — reported cost only, even
    # for a paid-looking model with real token counts.
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    ledger = CostLedger(path=str(tmp_path / "ledger.json"), month="2026-07")
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK], cost_usd=0.0,
                                     **_PAID_KWARGS)),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"], **_PAID_KWARGS)),
        ledger=ledger,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert ledger.run_total() == 0.0


def test_format_retry_round_ledgers_both_calls(tmp_path, monkeypatch):
    # Builder responses: decompose ("not json" -> fallback slice), a prose
    # build response that triggers the format retry, then the corrective FILE
    # block. Every call reports $0.42, so the BuildRound must carry the SUM of
    # the first build call AND its retry — otherwise the retry's spend would
    # escape the ledger and the budget cap could not bind.
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    ledger = CostLedger(path=str(tmp_path / "ledger.json"), month="2026-07")
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", "prose, no file blocks",
                                      FILE_BLOCK], cost_usd=0.42)),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        ledger=ledger,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    # decompose ($0.42) + build round summing first call + retry ($0.84).
    assert ledger.run_total() == pytest.approx(1.26)


def test_architect_auto_runs_on_multi_slice(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    workspace = _ws(tmp_path)
    memory = Memory(str(tmp_path / "mem.db"))
    cursor = PipelineCursor(workspace)
    builder = Builder(FakeProvider(
        [TWO_SLICE_JSON, DESIGN_MD, TWO_SLICE_JSON, FILE_BLOCK, FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS", "NO FINDINGS"])),
        workspace=workspace, memory=memory, cursor=cursor,
        architect_mode="auto",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert len(_architect_calls(builder.provider)) == 1
    design_path = os.path.join(workspace, "design.md")
    assert open(design_path, encoding="utf-8").read() == DESIGN_MD
    decomposes = _decompose_prompts(builder.provider)
    assert len(decomposes) == 2
    assert "DESIGN:" not in decomposes[0]
    assert "DESIGN:" in decomposes[1] and DESIGN_MD in decomposes[1]
    assert memory.get("design") == DESIGN_MD
    persisted = json.loads(open(cursor.path, encoding="utf-8").read())
    assert persisted["architect_done"] is True
    events = [json.loads(line) for line in open(cursor.events_path, encoding="utf-8")]
    assert any(e["event"] == "stage" and e["payload"].get("stage") == "architect"
               and e["payload"].get("status") == "done" for e in events)


def test_architect_call_is_ledgered(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    ledger = CostLedger(path=str(tmp_path / "ledger.json"), month="2026-07")
    builder = Builder(FakeProvider(
        [TWO_SLICE_JSON, DESIGN_MD, TWO_SLICE_JSON, FILE_BLOCK, FILE_BLOCK],
        model="gpt-5.5", tokens=200, prompt_tokens=100, completion_tokens=100))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS", "NO FINDINGS"], model="qwen2.5")),
        ledger=ledger, pricing=load_pricing(), architect_mode="auto",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    per_call = (100 * 1.25 + 100 * 10.0) / 1_000_000
    # decompose + architect + re-decompose + 2 builds = 5 ledgered calls.
    assert ledger.run_total() == pytest.approx(5 * per_call)


def test_architect_auto_skips_single_slice(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    workspace = _ws(tmp_path)
    builder = Builder(FakeProvider(["not json", FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        workspace=workspace, architect_mode="auto",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert _architect_calls(builder.provider) == []
    assert len(_decompose_prompts(builder.provider)) == 1
    assert not os.path.exists(os.path.join(workspace, "design.md"))


def test_architect_off_never_runs_even_multi_slice(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    workspace = _ws(tmp_path)
    builder = Builder(FakeProvider([TWO_SLICE_JSON, FILE_BLOCK, FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS", "NO FINDINGS"])),
        workspace=workspace, architect_mode="off",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert _architect_calls(builder.provider) == []
    assert not os.path.exists(os.path.join(workspace, "design.md"))


def test_architect_on_runs_for_single_slice(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    workspace = _ws(tmp_path)
    builder = Builder(FakeProvider(["not json", DESIGN_MD, "still not json", FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        workspace=workspace, architect_mode="on",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert len(_architect_calls(builder.provider)) == 1
    assert os.path.exists(os.path.join(workspace, "design.md"))
    decomposes = _decompose_prompts(builder.provider)
    assert len(decomposes) == 2 and "DESIGN:" in decomposes[1]


def test_resume_skips_done_architect_and_reuses_design(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    workspace = _ws(tmp_path)
    seed = PipelineCursor(workspace)
    seed.start(run_id="r1", objective_goal="build a thing")
    seed.mark_architect_done()
    with open(os.path.join(workspace, "design.md"), "w", encoding="utf-8") as handle:
        handle.write(DESIGN_MD)

    builder = Builder(FakeProvider([TWO_SLICE_JSON, FILE_BLOCK, FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS", "NO FINDINGS"])),
        run_id="r1",
        objective=Objective(goal="build a thing", done_when="pytest passes"),
        cursor=PipelineCursor(workspace), workspace=workspace,
        architect_mode="auto",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert _architect_calls(builder.provider) == []
    decomposes = _decompose_prompts(builder.provider)
    assert len(decomposes) == 1
    assert "DESIGN:" in decomposes[0] and DESIGN_MD in decomposes[0]


def test_architect_budget_checked_before_call(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    workspace = _ws(tmp_path)
    ledger = CostLedger(path=str(tmp_path / "ledger.json"), run_cap_usd=0.01,
                        month="2026-07")
    builder = Builder(FakeProvider([TWO_SLICE_JSON], cost_usd=0.02))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        ledger=ledger, workspace=workspace, architect_mode="auto",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.BUDGET_EXCEEDED
    assert _architect_calls(builder.provider) == []
    assert not os.path.exists(os.path.join(workspace, "design.md"))


def _pkg_ws(tmp_path, layout="flat"):
    """A workspace containing a detectable Python package ``mypkg``."""
    workspace = _ws(tmp_path)
    pkg = (os.path.join(workspace, "mypkg") if layout == "flat"
           else os.path.join(workspace, "src", "mypkg"))
    os.makedirs(pkg)
    open(os.path.join(pkg, "__init__.py"), "w").close()
    return workspace


def _run_architected(tmp_path, workspace, gates=None):
    builder = Builder(FakeProvider(
        [TWO_SLICE_JSON, DESIGN_MD, TWO_SLICE_JSON, FILE_BLOCK, FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS", "NO FINDINGS"])),
        workspace=workspace, architect_mode="auto", gates=gates,
    )
    return orch, orch.run()


def test_conformance_gate_appended_when_preconditions_hold(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    monkeypatch.setattr(orchestrator.shutil, "which",
                        lambda name: "/usr/bin/lint-imports")
    workspace = _pkg_ws(tmp_path)
    orch, outcome = _run_architected(tmp_path, workspace)

    assert outcome.state is TerminalState.SUCCESS
    arch_gates = [g for g in orch.gates if g.name == "architecture"]
    assert arch_gates == [GateSpec(name="architecture", command="lint-imports",
                                   prove=False)]
    ini = open(os.path.join(workspace, ".importlinter"), encoding="utf-8").read()
    assert "root_package = mypkg" in ini
    assert "type = layers" in ini


def test_conformance_gate_detects_src_layout_package(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    monkeypatch.setattr(orchestrator.shutil, "which",
                        lambda name: "/usr/bin/lint-imports")
    workspace = _pkg_ws(tmp_path, layout="src")
    orch, outcome = _run_architected(tmp_path, workspace)

    assert outcome.state is TerminalState.SUCCESS
    assert any(g.name == "architecture" for g in orch.gates)
    ini = open(os.path.join(workspace, ".importlinter"), encoding="utf-8").read()
    assert "root_package = mypkg" in ini


def test_conformance_gate_skipped_when_tool_missing(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    monkeypatch.setattr(orchestrator.shutil, "which", lambda name: None)
    workspace = _pkg_ws(tmp_path)
    with caplog.at_level(logging.WARNING):
        orch, outcome = _run_architected(tmp_path, workspace)

    assert outcome.state is TerminalState.SUCCESS
    assert all(g.name != "architecture" for g in orch.gates)
    assert not os.path.exists(os.path.join(workspace, ".importlinter"))
    assert any("SKIPPED" in r.message and "lint-imports" in r.message
               for r in caplog.records)


def test_conformance_gate_skipped_without_python_package(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    monkeypatch.setattr(orchestrator.shutil, "which",
                        lambda name: "/usr/bin/lint-imports")
    workspace = _ws(tmp_path)  # no package directory anywhere
    with caplog.at_level(logging.WARNING):
        orch, outcome = _run_architected(tmp_path, workspace)

    assert outcome.state is TerminalState.SUCCESS
    assert all(g.name != "architecture" for g in orch.gates)
    assert not os.path.exists(os.path.join(workspace, ".importlinter"))
    assert any("SKIPPED" in r.message and "package" in r.message
               for r in caplog.records)


def test_conformance_gate_skipped_without_layering(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    monkeypatch.setattr(orchestrator.shutil, "which",
                        lambda name: "/usr/bin/lint-imports")
    workspace = _pkg_ws(tmp_path)
    no_layering = DESIGN_MD.split("```yaml")[0]
    builder = Builder(FakeProvider(
        [TWO_SLICE_JSON, no_layering, TWO_SLICE_JSON, FILE_BLOCK, FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS", "NO FINDINGS"])),
        workspace=workspace, architect_mode="auto",
    )
    with caplog.at_level(logging.WARNING):
        outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert all(g.name != "architecture" for g in orch.gates)
    assert not os.path.exists(os.path.join(workspace, ".importlinter"))
    assert any("SKIPPED" in r.message and "layering" in r.message
               for r in caplog.records)


def test_importlinter_propagated_into_attempt_worktrees(tmp_path, monkeypatch):
    # git worktrees materialize committed files only, so the engine-rendered
    # (uncommitted) .importlinter must be copied into every attempt worktree
    # or the architecture gate errors there and stalls every attempt.
    monkeypatch.setattr(orchestrator.shutil, "which",
                        lambda name: "/usr/bin/lint-imports")
    gated_workspaces = []

    def gates_requiring_importlinter(specs, ws_arg, policy=None):
        gated_workspaces.append(ws_arg)
        if (any(s.name == "architecture" for s in specs)
                and not os.path.exists(os.path.join(ws_arg, ".importlinter"))):
            return GatesResult(records=[EvidenceRecord(
                gate="architecture", command="lint-imports", passed=False,
                evidence="could not read .importlinter")])
        return _pass_gates(specs, ws_arg, policy)

    monkeypatch.setattr(orchestrator, "run_gates", gates_requiring_importlinter)
    workspace = _pkg_ws(tmp_path)
    orch, outcome = _run_architected(tmp_path, workspace)

    assert outcome.state is TerminalState.SUCCESS
    attempt_workspaces = [ws for ws in gated_workspaces if ws != workspace]
    assert attempt_workspaces  # attempts were gated in their own worktrees
    for ws in attempt_workspaces:
        assert os.path.exists(os.path.join(ws, ".importlinter"))
    # The final gate list still runs in the main workspace, unchanged.
    assert gated_workspaces[-1] == workspace


def test_importlinter_propagation_failure_is_best_effort(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    monkeypatch.setattr(orchestrator.shutil, "which",
                        lambda name: "/usr/bin/lint-imports")

    def explode(src, dst):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(orchestrator.shutil, "copy2", explode)
    workspace = _pkg_ws(tmp_path)
    with caplog.at_level(logging.WARNING):
        orch, outcome = _run_architected(tmp_path, workspace)

    assert outcome.state is TerminalState.SUCCESS
    assert any(".importlinter" in r.getMessage() for r in caplog.records)


class TamperingWorktrees(FakeWorktrees):
    """Simulates promote() carrying a builder-rewritten .importlinter back."""

    WEAKENED = "[importlinter]\nroot_package = mypkg\n"

    def promote(self, src, dest):
        with open(os.path.join(dest, ".importlinter"), "w", encoding="utf-8") as h:
            h.write(self.WEAKENED)
        return super().promote(src, dest)


def test_importlinter_rerendered_after_tampered_promote(tmp_path, monkeypatch, caplog):
    # promote() brings untracked winner files back, so a builder that rewrote
    # .importlinter in its worktree must not weaken the declared contract:
    # the engine re-renders it after every promote and flags the tamper.
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    monkeypatch.setattr(orchestrator.shutil, "which",
                        lambda name: "/usr/bin/lint-imports")
    workspace = _pkg_ws(tmp_path)
    builder = Builder(FakeProvider(
        [TWO_SLICE_JSON, DESIGN_MD, TWO_SLICE_JSON, FILE_BLOCK, FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS", "NO FINDINGS"])),
        workspace=workspace, architect_mode="auto",
        worktrees=TamperingWorktrees(str(tmp_path / "wts")),
    )
    with caplog.at_level(logging.WARNING):
        outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    content = open(os.path.join(workspace, ".importlinter"), encoding="utf-8").read()
    assert content != TamperingWorktrees.WEAKENED
    assert "type = layers" in content  # the engine-rendered contract
    assert any("differed" in r.getMessage() and ".importlinter" in r.getMessage()
               for r in caplog.records)


def test_importlinter_untampered_promote_logs_no_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    monkeypatch.setattr(orchestrator.shutil, "which",
                        lambda name: "/usr/bin/lint-imports")
    workspace = _pkg_ws(tmp_path)
    with caplog.at_level(logging.WARNING):
        orch, outcome = _run_architected(tmp_path, workspace)

    assert outcome.state is TerminalState.SUCCESS
    assert os.path.exists(os.path.join(workspace, ".importlinter"))
    assert not any("differed" in r.getMessage() for r in caplog.records)


def test_conformance_gate_appended_once_on_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    monkeypatch.setattr(orchestrator.shutil, "which",
                        lambda name: "/usr/bin/lint-imports")
    workspace = _pkg_ws(tmp_path)
    seed = PipelineCursor(workspace)
    seed.start(run_id="r1", objective_goal="build a thing")
    seed.mark_architect_done()
    with open(os.path.join(workspace, "design.md"), "w", encoding="utf-8") as handle:
        handle.write(DESIGN_MD)

    builder = Builder(FakeProvider([TWO_SLICE_JSON, FILE_BLOCK, FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS", "NO FINDINGS"])),
        run_id="r1",
        objective=Objective(goal="build a thing", done_when="pytest passes"),
        cursor=PipelineCursor(workspace), workspace=workspace,
        architect_mode="auto",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert _architect_calls(builder.provider) == []
    assert len([g for g in orch.gates if g.name == "architecture"]) == 1
    assert os.path.exists(os.path.join(workspace, ".importlinter"))


def test_conformance_gate_never_duplicated_in_configured_gates(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    monkeypatch.setattr(orchestrator.shutil, "which",
                        lambda name: "/usr/bin/lint-imports")
    workspace = _pkg_ws(tmp_path)
    preconfigured = [GateSpec(prove=False),
                     GateSpec(name="architecture", command="lint-imports", prove=False)]
    orch, outcome = _run_architected(tmp_path, workspace, gates=preconfigured)

    assert outcome.state is TerminalState.SUCCESS
    assert len([g for g in orch.gates if g.name == "architecture"]) == 1


def test_archmap_written_after_each_promote_and_regenerated(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    workspace = _pkg_ws(tmp_path)
    pkg = os.path.join(workspace, "mypkg")
    with open(os.path.join(pkg, "a.py"), "w", encoding="utf-8") as handle:
        handle.write("from .b import helper\n")
    with open(os.path.join(pkg, "b.py"), "w", encoding="utf-8") as handle:
        handle.write("helper = 1\n")
    builder = Builder(FakeProvider([TWO_SLICE_JSON, FILE_BLOCK, FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS", "NO FINDINGS"])),
        workspace=workspace,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    archmap_path = os.path.join(workspace, "docs", "architecture.mermaid.md")
    assert os.path.exists(archmap_path)
    content = open(archmap_path, encoding="utf-8").read()
    assert "graph TD" in content
    # Two slices promoted -> two writes; regenerated, never appended.
    assert content.count("graph TD") == 1


def test_archmap_failure_never_fails_the_run(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)

    def explode(workspace):
        raise OSError("disk full")

    monkeypatch.setattr(orchestrator, "write_archmap", explode)
    builder = Builder(FakeProvider([TWO_SLICE_JSON, FILE_BLOCK, FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS", "NO FINDINGS"])),
    )
    with caplog.at_level(logging.WARNING):
        outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert any("archmap" in r.getMessage() for r in caplog.records)


def test_promote_failure_yields_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    notifier = FakeNotifier()
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        notifier=notifier,
        worktrees=PromoteFailingWorktrees(str(tmp_path / "wts")),
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.BLOCKED
    assert "promotion failed" in outcome.reason
    assert orch.worktrees.removed == orch.worktrees.created
    assert any(e["stage"] == "terminal" for e in notifier.events)

def test_retry_failure_partial_spend_is_ledgered(tmp_path, monkeypatch):
    # The build round's FIRST call completes (and costs money); the corrective
    # format retry then dies with a ProviderError. The first call's spend must
    # still reach the ledger via the exception's attached partial result.
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    ledger = CostLedger(path=str(tmp_path / "ledger.json"), month="2026-07")
    builder = Builder(RaisingProvider(["not json", "prose with no file blocks"],
                                      cost_usd=0.05))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        ledger=ledger,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.BLOCKED
    # decompose ($0.05, ledgered via on_usage) + the dead round's completed
    # first call ($0.05, ledgered from exc.partial_result) = $0.10.
    assert ledger.run_total() == pytest.approx(0.10)


def test_design_write_failure_degrades_and_run_continues(tmp_path, monkeypatch, caplog):
    # design.md unwritable (a directory squats on the path): the architect
    # stage logs a warning and continues with the in-memory design — the
    # re-decompose still sees it and the run still succeeds.
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    workspace = _ws(tmp_path)
    os.makedirs(os.path.join(workspace, "design.md"))
    memory = Memory(str(tmp_path / "mem.db"))
    builder = Builder(FakeProvider(
        [TWO_SLICE_JSON, DESIGN_MD, TWO_SLICE_JSON, FILE_BLOCK, FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS", "NO FINDINGS"])),
        workspace=workspace, memory=memory, architect_mode="auto",
    )
    with caplog.at_level(logging.WARNING, logger="kickass_loop_engineer.orchestrator"):
        outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert any("design.md" in r.message for r in caplog.records)
    decomposes = _decompose_prompts(builder.provider)
    assert len(decomposes) == 2 and "DESIGN:" in decomposes[1]
    assert memory.get("design") == DESIGN_MD


def test_resume_rearms_architect_when_design_md_missing(tmp_path, monkeypatch, caplog):
    # Cursor says the architect is done but design.md is gone: the resume
    # warns and re-arms the architect trigger instead of decomposing blind.
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    workspace = _ws(tmp_path)
    seed = PipelineCursor(workspace)
    seed.start(run_id="r1", objective_goal="build a thing")
    seed.mark_architect_done()

    builder = Builder(FakeProvider(
        [TWO_SLICE_JSON, DESIGN_MD, TWO_SLICE_JSON, FILE_BLOCK, FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS", "NO FINDINGS"])),
        run_id="r1", cursor=PipelineCursor(workspace), workspace=workspace,
        architect_mode="auto",
    )
    with caplog.at_level(logging.WARNING, logger="kickass_loop_engineer.orchestrator"):
        outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert len(_architect_calls(builder.provider)) == 1
    assert any("re-run" in r.message for r in caplog.records)


# --- Observer threading: observations feed retries, feedback, and stalls ---

def _fail_gates_with_observed(observed):
    """A run_gates fake whose failing first gate carries observer output."""
    def fake(specs, workspace, policy=None):
        first = specs[0]
        return GatesResult(records=[EvidenceRecord(
            gate=first.name, command=first.command, passed=False,
            evidence="fail", observed=observed)])
    return fake


def _stalled_with_observation(tmp_path, monkeypatch, observed):
    monkeypatch.setattr(orchestrator, "run_gates",
                        _fail_gates_with_observed(observed))
    builder = Builder(FakeProvider(["not json", FILE_BLOCK, FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        gates=[GateSpec(name="unit", command="pytest -q", prove=False)],
        ensemble_n=2,
    )
    return orch, builder, orch.run()


def test_observation_reaches_later_attempt_briefs_in_same_ensemble(tmp_path, monkeypatch):
    orch, builder, outcome = _stalled_with_observation(
        tmp_path, monkeypatch, "console error: undefined foo")

    assert outcome.state is TerminalState.STALLED
    prompts = [user for (_system, user) in builder.provider.calls]
    attempt1, attempt2 = prompts[1], prompts[2]  # prompts[0] is decompose
    assert "OBSERVED (gate: unit):" not in attempt1
    assert "OBSERVED (gate: unit):" in attempt2
    assert "console error: undefined foo" in attempt2


def test_stalled_evidence_carries_the_observation(tmp_path, monkeypatch):
    _orch, _builder, outcome = _stalled_with_observation(
        tmp_path, monkeypatch, "console error: undefined foo")

    assert outcome.state is TerminalState.STALLED
    assert "console error: undefined foo" in json.dumps(outcome.evidence)


def test_stalled_observation_lands_in_next_slice_feedback(tmp_path, monkeypatch):
    orch, _builder, outcome = _stalled_with_observation(
        tmp_path, monkeypatch, "console error: undefined foo")

    assert outcome.state is TerminalState.STALLED
    assert "console error: undefined foo" in orch._feedback


def test_losing_attempt_observation_feeds_next_slice_after_winner(tmp_path, monkeypatch):
    calls = {"n": 0}

    def first_fails_observed_then_pass(specs, workspace, policy=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _fail_gates_with_observed("a11y tree: Save missing")(
                specs, workspace, policy)
        return _pass_gates(specs, workspace, policy)

    monkeypatch.setattr(orchestrator, "run_gates", first_fails_observed_then_pass)
    builder = Builder(FakeProvider(
        [TWO_SLICE_JSON, FILE_BLOCK, FILE_BLOCK, FILE_BLOCK, FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS", "NO FINDINGS"])),
        gates=[GateSpec(name="unit", command="pytest -q", prove=False)],
        ensemble_n=2,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    prompts = [user for (_system, user) in builder.provider.calls]
    # prompts: [decompose, core-0, core-1, api-0, api-1]
    assert "OBSERVED (gate: unit):" in prompts[2]  # same-ensemble retry
    assert "OBSERVED (gate: unit):" in prompts[3]  # next slice's first attempt
    assert "a11y tree: Save missing" in prompts[3]


def test_no_observations_leaves_briefs_and_stall_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _fail_gates)
    builder = Builder(FakeProvider(["not json", FILE_BLOCK, FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        gates=[GateSpec(name="unit", command="pytest -q", prove=False)],
        ensemble_n=2,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.STALLED
    assert outcome.evidence == []
    prompts = [user for (_system, user) in builder.provider.calls]
    assert all("OBSERVED" not in p for p in prompts)
    assert orch._feedback == ""


def test_total_injected_observation_text_is_capped(tmp_path, monkeypatch):
    orch, _builder, outcome = _stalled_with_observation(
        tmp_path, monkeypatch, "x" * (OBSERVER_CAP * 2))

    assert outcome.state is TerminalState.STALLED
    assert len(orch._feedback) <= OBSERVER_CAP


def test_stalled_observation_appends_to_prior_review_feedback(tmp_path, monkeypatch):
    # Slice 1 promotes (gate passes) and its review leaves a finding; slice 2
    # stalls with observer output. The stall must APPEND to the finding-based
    # feedback, not replace it (M3 review rider).
    calls = {"n": 0}

    def first_passes_then_fails_observed(specs, workspace, policy=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _pass_gates(specs, workspace, policy)
        return _fail_gates_with_observed("console error: undefined foo")(
            specs, workspace, policy)

    monkeypatch.setattr(orchestrator, "run_gates",
                        first_passes_then_fails_observed)
    builder = Builder(FakeProvider([TWO_SLICE_JSON, FILE_BLOCK, FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["FINDING: rev issue"])),
        gates=[GateSpec(name="unit", command="pytest -q", prove=False)],
        ensemble_n=1,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.STALLED
    assert "rev issue" in orch._feedback
    assert "console error: undefined foo" in orch._feedback


def test_stall_feedback_history_is_bounded(tmp_path, monkeypatch):
    # Four stalled runs on the same orchestrator (resume path) append four
    # observation entries; the bounded history keeps only the last three.
    monkeypatch.setattr(orchestrator, "run_gates",
                        _fail_gates_with_observed("console error: undefined foo"))
    builder = Builder(FakeProvider(["not json", FILE_BLOCK] * 4))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        gates=[GateSpec(name="unit", command="pytest -q", prove=False)],
        ensemble_n=1,
    )
    for _ in range(4):
        outcome = orch.run()
        assert outcome.state is TerminalState.STALLED

    assert orch._feedback.count("console error: undefined foo") == 3


# --- Visual critique: advisory VLM findings on the winner path ---

def _visual_setup(tmp_path, monkeypatch, *, builder_responses=None,
                  reviewer_responses=("NO FINDINGS",),
                  visual_text="FINDING: save button overlaps footer",
                  policy=None):
    """An orchestrator with the visual stage configured (fake policy + VLM)."""
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"png")
    policy = policy or FakePolicy()
    vision = FakeVisionProvider(text=visual_text, cost_usd=0.25)
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(builder_responses or ["not json", FILE_BLOCK])),
        reviewer=Reviewer(FakeProvider(list(reviewer_responses))),
        policy=policy,
        visual_cfg={"screenshot_cmd":
                    f"npx playwright-cli screenshot http://localhost:3000 {shot}"},
        visual_provider=vision,
    )
    return orch, vision, policy


def test_visual_findings_join_the_review_feedback_stream(tmp_path, monkeypatch):
    orch, vision, _policy = _visual_setup(
        tmp_path, monkeypatch, reviewer_responses=("FINDING: rev issue",))
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert len(vision.calls) == 1
    assert orch._feedback == "rev issue\nsave button overlaps footer"


def test_visual_screenshot_runs_via_policy_in_winner_worktree(tmp_path, monkeypatch):
    orch, _vision, policy = _visual_setup(tmp_path, monkeypatch)
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert len(policy.runs) == 1
    command, cwd = policy.runs[0]
    assert command.startswith("npx playwright-cli screenshot")
    assert cwd in orch.worktrees.created


def test_visual_call_is_ledgered(tmp_path, monkeypatch):
    orch, _vision, _policy = _visual_setup(tmp_path, monkeypatch)
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert orch.ledger.run_total() == 0.25  # only the VLM call carries cost


def test_repeating_visual_findings_feed_oscillation(tmp_path, monkeypatch):
    orch, _vision, _policy = _visual_setup(
        tmp_path, monkeypatch,
        builder_responses=[TWO_SLICE_JSON, FILE_BLOCK, FILE_BLOCK],
        reviewer_responses=("NO FINDINGS", "NO FINDINGS"))
    outcome = orch.run()

    assert outcome.state is TerminalState.OSCILLATION


def test_visual_screenshot_failure_is_advisory_only(tmp_path, monkeypatch):
    orch, vision, _policy = _visual_setup(
        tmp_path, monkeypatch,
        policy=FakePolicy(passed=False, error="rejected: not allowlisted"))
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert vision.calls == []
    assert orch._feedback == ""


# --- Enhance mode: repo context flows into briefs and the architect ---

UPDATE_INSTRUCTION = ("these files EXIST — emit the complete UPDATED file for "
                      "any you change; do not drop existing behavior")


def _counting_repomap(monkeypatch, map_text="MAPTEXT",
                      files=(("old.py", "OLD = 1\n"),)):
    """Replace the orchestrator's repomap primitives with counting fakes."""
    counts = {"map": 0, "select": 0}

    def fake_map(root, budget_chars=8000):
        counts["map"] += 1
        return map_text

    def fake_select(root, objective_text, budget_bytes=24576):
        counts["select"] += 1
        return list(files)

    monkeypatch.setattr(orchestrator, "repo_map", fake_map)
    monkeypatch.setattr(orchestrator, "select_files", fake_select)
    return counts


def test_enhance_mode_briefs_carry_repo_context(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    counts = _counting_repomap(monkeypatch)
    builder = Builder(FakeProvider([TWO_SLICE_JSON, FILE_BLOCK, FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS", "NO FINDINGS"])),
        mode="enhance",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    build_prompts = [user for (_system, user) in builder.provider.calls[1:]]
    assert len(build_prompts) == 2
    for prompt in build_prompts:
        assert "CONTEXT:" in prompt
        assert "REPO MAP:\nMAPTEXT" in prompt
        assert "--- old.py ---\nOLD = 1" in prompt
        assert UPDATE_INSTRUCTION in prompt
    assert counts == {"map": 1, "select": 1}


def test_enhance_context_is_assembled_once_across_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    counts = _counting_repomap(monkeypatch)
    builder = Builder(FakeProvider(
        [TWO_SLICE_JSON, FILE_BLOCK, FILE_BLOCK, FILE_BLOCK, FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS", "NO FINDINGS"])),
        ensemble_n=2, mode="enhance",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    # 2 slices x 2 attempts, yet the context was assembled exactly once.
    assert counts == {"map": 1, "select": 1}


def test_enhance_architect_prompt_carries_the_repo_map(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    counts = _counting_repomap(monkeypatch)
    builder = Builder(FakeProvider(
        [TWO_SLICE_JSON, DESIGN_MD, TWO_SLICE_JSON, FILE_BLOCK, FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS", "NO FINDINGS"])),
        architect_mode="auto", mode="enhance",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    architect_prompts = _architect_calls(builder.provider)
    assert len(architect_prompts) == 1
    assert "REPO MAP:\nMAPTEXT" in architect_prompts[0]
    # The architect reuses the run's cached map — never a second computation.
    assert counts["map"] == 1


def test_enhance_mode_with_empty_repo_context_leaves_briefs_unchanged(
        tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    _counting_repomap(monkeypatch, map_text="", files=())
    builder = Builder(FakeProvider(["not json", FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        mode="enhance",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    prompts = [user for (_system, user) in builder.provider.calls]
    assert all("CONTEXT:" not in p for p in prompts)


def test_build_mode_never_touches_repomap_and_briefs_are_unchanged(
        tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    counts = _counting_repomap(monkeypatch)
    builder = Builder(FakeProvider(
        [TWO_SLICE_JSON, DESIGN_MD, TWO_SLICE_JSON, FILE_BLOCK, FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS", "NO FINDINGS"])),
        architect_mode="auto", mode="build",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert counts == {"map": 0, "select": 0}
    prompts = [user for (_system, user) in builder.provider.calls]
    for prompt in prompts:
        assert "CONTEXT:" not in prompt
        assert "REPO MAP:" not in prompt
        assert UPDATE_INSTRUCTION not in prompt


def test_build_mode_briefs_are_byte_identical_to_pre_m4(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    _counting_repomap(monkeypatch)
    builder = Builder(FakeProvider(["not json", FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        objective=Objective(goal="build a thing", done_when="pytest passes"),
        mode="build",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    _system, build_prompt = builder.provider.calls[1]
    # The pre-M4 brief, verbatim: no CONTEXT section, no repo map, nothing.
    assert build_prompt == "GOAL:\nbuild a thing\n\nDONE WHEN:\npytest passes"


def test_visual_off_leaves_the_review_stream_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    policy = FakePolicy()
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK])),
        reviewer=Reviewer(FakeProvider(["FINDING: rev issue"])),
        policy=policy,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert policy.runs == []  # no screenshot command ran
    assert orch._feedback == "rev issue"


# --- Task modes: auto-detection resolves at run() start and is recorded ---


def _git_ws(tmp_path, files=None):
    """A git-repo workspace with committed files (tracked → auto = enhance)."""
    ws = tmp_path / "gitws"
    ws.mkdir()
    files = files if files is not None else {"existing.py": "OLD = 1\n"}
    for rel, content in files.items():
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=ws, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return str(ws)


def _mode_events(path):
    with open(path, encoding="utf-8") as handle:
        lines = [json.loads(line) for line in handle if line.strip()]
    return [e for e in lines if e["event"] == "mode_resolved"]


def test_ctor_default_mode_is_auto():
    import inspect
    default = inspect.signature(Orchestrator.__init__).parameters["mode"].default
    assert default == "auto"


def test_auto_resolves_to_build_on_non_repo_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    counts = _counting_repomap(monkeypatch)
    workspace = _ws(tmp_path)
    cursor = PipelineCursor(workspace)
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        workspace=workspace, cursor=cursor, mode="auto",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert orch.mode == "build"
    assert counts == {"map": 0, "select": 0}
    persisted = json.loads(open(cursor.path, encoding="utf-8").read())
    assert persisted["mode"] == "build"


def test_auto_resolves_to_enhance_on_tracked_repo_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    counts = _counting_repomap(monkeypatch)
    workspace = _git_ws(tmp_path)
    cursor = PipelineCursor(workspace)
    builder = Builder(FakeProvider(["not json", FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        workspace=workspace, cursor=cursor, mode="auto",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert orch.mode == "enhance"
    assert counts == {"map": 1, "select": 1}
    _system, build_prompt = builder.provider.calls[1]
    assert "REPO MAP:\nMAPTEXT" in build_prompt
    persisted = json.loads(open(cursor.path, encoding="utf-8").read())
    assert persisted["mode"] == "enhance"


def test_explicit_audit_resolves_and_records_the_mode(tmp_path, monkeypatch):
    # Fix mode grew its own stage (reproduce-first) and is covered in the
    # fix-mode section below; audit short-circuits into the read-only sweep
    # (covered in the audit section below) and assembles the repo context
    # exactly once for its file selection.
    counts = _counting_repomap(monkeypatch)
    workspace = _git_ws(tmp_path)
    cursor = PipelineCursor(workspace)
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider([])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        workspace=workspace, cursor=cursor, mode="audit",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert orch.mode == "audit"
    assert counts == {"map": 1, "select": 1}
    persisted = json.loads(open(cursor.path, encoding="utf-8").read())
    assert persisted["mode"] == "audit"


def test_explicit_build_passes_through_on_a_tracked_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    counts = _counting_repomap(monkeypatch)
    workspace = _git_ws(tmp_path)
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        workspace=workspace, mode="build",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert orch.mode == "build"
    assert counts == {"map": 0, "select": 0}


def test_invalid_mode_raises_runtime_error_listing_modes(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        mode="refactor",
    )
    with pytest.raises(RuntimeError) as excinfo:
        orch.run()
    message = str(excinfo.value)
    for valid in ("auto", "build", "enhance", "fix", "audit"):
        assert valid in message


def test_resolved_mode_lands_in_notifier_and_cursor_events(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    notifier = FakeNotifier()
    workspace = _ws(tmp_path)
    cursor = PipelineCursor(workspace)
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        notifier=notifier, workspace=workspace, cursor=cursor, mode="auto",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    resolved = [e for e in notifier.events if e["event"] == "mode_resolved"]
    assert len(resolved) == 1
    assert resolved[0]["stage"] == "mode"
    assert resolved[0]["payload"] == {"mode": "build"}
    cursor_events = _mode_events(cursor.events_path)
    assert len(cursor_events) == 1
    assert cursor_events[0]["payload"] == {"mode": "build"}


# --- Fix mode: reproduce-first stage, proving gate, tamper restore ---

FIX_GOAL = "fix double bug"
FIX_REPRO_PATH = "tests/test_repro_fix-double-bug.py"
FIX_REPRO_GATE_CMD = repro_check_command(FIX_REPRO_PATH)
REPRO_TEST_BODY = ("from feature import double\n\n\n"
                   "def test_double_reproduces_bug():\n"
                   "    assert double(2) == 4\n")
FAILING_REPRO_BLOCK = (f"=== FILE: {FIX_REPRO_PATH} ===\n{REPRO_TEST_BODY}"
                       "=== END FILE ===\n")
PASSING_REPRO_BLOCK = (f"=== FILE: {FIX_REPRO_PATH} ===\n"
                       "def test_green():\n    assert True\n=== END FILE ===\n")
FIX_FILE_BLOCK = ("=== FILE: feature.py ===\ndef double(x):\n    return x * 2\n"
                  "=== END FILE ===\n")
NONFIX_FILE_BLOCK = "=== FILE: unrelated.py ===\nNOOP = 1\n=== END FILE ===\n"
WEAKENED_REPRO_BLOCK = (f"=== FILE: {FIX_REPRO_PATH} ===\n"
                        "def test_repro():\n    pass\n=== END FILE ===\n")
TAMPER_FIX_BLOCK = FIX_FILE_BLOCK + WEAKENED_REPRO_BLOCK
# No fix; a conftest that skip-marks every test_repro_* test (skipped tests
# exit 0) — the sibling-file gaming attack the isolated repro gate defeats.
CONFTEST_GAMING_BLOCK = (
    "=== FILE: conftest.py ===\n"
    "import pytest\n\n\n"
    "def pytest_collection_modifyitems(items):\n"
    "    for item in items:\n"
    "        if 'test_repro_' in str(item.fspath):\n"
    "            item.add_marker(pytest.mark.skip(reason='flaky'))\n"
    "=== END FILE ===\n"
)


def _tamper_events(notifier):
    return [e for e in notifier.events if e["event"] == "tamper_detected"]


def _bug_repo_ws(tmp_path):
    """A committed git workspace with a planted bug in feature.double."""
    ws = tmp_path / "bugws"
    ws.mkdir()
    (ws / "feature.py").write_text("def double(x):\n    return x + 1\n")
    (ws / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.pytest_cache/\n.loop-engineer/\n")
    for cmd in (["git", "init", "-q"],
                ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"],
                ["git", "add", "feature.py", ".gitignore"],
                ["git", "commit", "-qm", "planted bug baseline"]):
        subprocess.run(cmd, cwd=ws, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return str(ws)


def _fix_orch(tmp_path, workspace, responses):
    """A fix-mode orchestrator on real worktrees/gates against *workspace*."""
    builder = Builder(FakeProvider(list(responses)))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        objective=Objective(goal=FIX_GOAL,
                            done_when="python3 -m pytest -q passes"),
        workspace=workspace, cursor=PipelineCursor(workspace),
        worktrees=WorktreeManager(workspace, base_dir=str(tmp_path / "wts")),
        gates=[GateSpec(name="unit", command="python3 -m pytest -q",
                        prove=False)],
        mode="fix",
    )
    return builder, orch


def _fake_repro(monkeypatch, result, calls=None):
    """Replace the orchestrator's generate_repro with a canned-result fake."""
    def fake(builder, objective, workspace, policy=None, on_usage=None):
        if calls is not None:
            calls.append((objective.goal, workspace))
        return result
    monkeypatch.setattr(orchestrator, "generate_repro", fake)


@pytest.mark.slow
def test_fix_mode_fixing_builder_turns_repro_green_and_succeeds(
        tmp_path, caplog):
    from kickass_loop_engineer.decompose import SLICE_SYSTEM
    workspace = _bug_repo_ws(tmp_path)
    builder, orch = _fix_orch(
        tmp_path, workspace, [FAILING_REPRO_BLOCK, "not json", FIX_FILE_BLOCK])
    with caplog.at_level(logging.WARNING):
        outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS, outcome.reason
    systems = _systems(builder.provider)
    assert systems[0] == REPRO_SYSTEM, "repro stage runs first"
    assert systems[1] == SLICE_SYSTEM, "decompose only after the repro is pinned"
    fixed = open(os.path.join(workspace, "feature.py"), encoding="utf-8").read()
    assert "x * 2" in fixed, "the fix was promoted into the workspace"
    repro_gates = [g for g in orch.gates if g.command == FIX_REPRO_GATE_CMD]
    assert len(repro_gates) == 1 and repro_gates[0].prove is True
    persisted = json.loads(open(orch.cursor.path, encoding="utf-8").read())
    assert persisted["repro_path"] == FIX_REPRO_PATH
    assert any(g["command"] == FIX_REPRO_GATE_CMD
               and g.get("proof", {}).get("proven")
               for entry in outcome.evidence for g in entry["gates"]), \
        "the winner proved the repro red->green"
    build_prompts = [u for (s, u) in builder.provider.calls
                     if s not in (REPRO_SYSTEM, SLICE_SYSTEM)]
    assert build_prompts and all(FIX_MODE_RULES in p for p in build_prompts), \
        "every fix-mode brief forbids touching the repro"
    assert not any("tampering" in r.getMessage() for r in caplog.records)


@pytest.mark.slow
def test_fix_mode_non_fixing_builder_leaves_repro_red_and_stalls(tmp_path):
    workspace = _bug_repo_ws(tmp_path)
    _builder, orch = _fix_orch(
        tmp_path, workspace, [FAILING_REPRO_BLOCK, "not json", NONFIX_FILE_BLOCK])
    outcome = orch.run()

    assert outcome.state is TerminalState.STALLED
    assert "no attempt passed" in outcome.reason
    assert os.path.isfile(os.path.join(workspace, FIX_REPRO_PATH)), \
        "the committed repro still pins the bug"


@pytest.mark.slow
def test_fix_mode_unreproducible_bug_blocks_before_decompose(tmp_path):
    workspace = _bug_repo_ws(tmp_path)
    builder, orch = _fix_orch(
        tmp_path, workspace, [PASSING_REPRO_BLOCK, PASSING_REPRO_BLOCK])
    outcome = orch.run()

    assert outcome.state is TerminalState.BLOCKED
    assert "cannot reproduce" in outcome.reason
    assert _systems(builder.provider) == [REPRO_SYSTEM, REPRO_SYSTEM], \
        "one regeneration, then blocked — decompose never ran"
    persisted = json.loads(open(orch.cursor.path, encoding="utf-8").read())
    assert persisted["stage"] == "terminal" and persisted["status"] == "blocked"


@pytest.mark.slow
def test_fix_mode_tampered_repro_is_restored_before_gates(tmp_path, caplog):
    # The builder fixes the bug AND rewrites the repro: the pre-gate
    # restore reverts the tamper inside the worktree, so gates run the
    # engine-pinned repro; the genuine fix still wins and the pinned repro
    # reaches the workspace untouched.
    workspace = _bug_repo_ws(tmp_path)
    _builder, orch = _fix_orch(
        tmp_path, workspace, [FAILING_REPRO_BLOCK, "not json", TAMPER_FIX_BLOCK])
    with caplog.at_level(logging.WARNING):
        outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS, outcome.reason
    content = open(os.path.join(workspace, FIX_REPRO_PATH),
                   encoding="utf-8").read()
    assert content == REPRO_TEST_BODY, "the committed repro was restored verbatim"
    assert any("tampering" in r.getMessage()
               and FIX_REPRO_PATH in r.getMessage()
               for r in caplog.records), "the tamper is loudly flagged"
    events = _tamper_events(orch.notifier)
    assert events and events[0]["payload"]["path"] == FIX_REPRO_PATH, \
        "tampering emits a tamper_detected event, not just a log line"


@pytest.mark.slow
def test_fix_mode_tamper_without_fix_loses_at_gate_time(tmp_path, caplog):
    # Direct-rewrite tampering with NO fix: the pre-gate restore puts the
    # honest repro back before gates run, so the attempt fails at the
    # attempt level (STALLED) instead of forging a win.
    workspace = _bug_repo_ws(tmp_path)
    _builder, orch = _fix_orch(
        tmp_path, workspace,
        [FAILING_REPRO_BLOCK, "not json", WEAKENED_REPRO_BLOCK])
    with caplog.at_level(logging.WARNING):
        outcome = orch.run()

    assert outcome.state is TerminalState.STALLED
    assert _tamper_events(orch.notifier), "the gate-time tamper is evented"
    assert any("tampering" in r.getMessage() for r in caplog.records)


@pytest.mark.slow
def test_fix_mode_conftest_gaming_cannot_forge_success(tmp_path):
    # Adversarial probe: no fix, just a conftest.py that skip-marks
    # test_repro_*. The config-isolated repro gate (--noconftest -o addopts=)
    # still runs the honest repro against the unfixed bug and goes red.
    workspace = _bug_repo_ws(tmp_path)
    _builder, orch = _fix_orch(
        tmp_path, workspace,
        [FAILING_REPRO_BLOCK, "not json", CONFTEST_GAMING_BLOCK])
    outcome = orch.run()

    assert outcome.state is not TerminalState.SUCCESS, \
        "SUCCESS forged via conftest skip-marking"
    assert outcome.state is TerminalState.STALLED
    buggy = open(os.path.join(workspace, "feature.py"), encoding="utf-8").read()
    assert "x + 1" in buggy, "the bug is still there — nothing may report done"


def test_fix_mode_blocked_repro_finishes_via_terminal_path(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    _fake_repro(monkeypatch, ReproResult(
        path="", blocked=True, reason="cannot reproduce: repro stays green"))
    notifier = FakeNotifier()
    workspace = _ws(tmp_path)
    cursor = PipelineCursor(workspace)
    builder = Builder(FakeProvider([]))
    orch = _make(
        tmp_path, builder=builder, reviewer=Reviewer(FakeProvider([])),
        notifier=notifier, workspace=workspace, cursor=cursor, mode="fix",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.BLOCKED
    assert outcome.reason.startswith("cannot reproduce")
    assert builder.provider.calls == [], "no decompose, no build"
    persisted = json.loads(open(cursor.path, encoding="utf-8").read())
    assert persisted["stage"] == "terminal" and persisted["status"] == "blocked"
    assert persisted["mode"] == "fix"
    assert any(e["stage"] == "terminal" and e["event"] == "blocked"
               for e in notifier.events)


def test_fix_mode_appends_one_proving_repro_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    _fake_repro(monkeypatch,
                ReproResult(path="tests/test_repro_x.py", committed=True))
    workspace = _ws(tmp_path)
    cursor = PipelineCursor(workspace)
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        workspace=workspace, cursor=cursor, mode="fix",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    matching = [g for g in orch.gates
                if g.command == repro_check_command("tests/test_repro_x.py")]
    assert len(matching) == 1
    assert matching[0].prove is True and matching[0].name == "unit"
    persisted = json.loads(open(cursor.path, encoding="utf-8").read())
    assert persisted["repro_path"] == "tests/test_repro_x.py"


def test_fix_mode_resume_skips_repro_and_never_duplicates_gate(
        tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    calls = []
    _fake_repro(monkeypatch,
                ReproResult(path="tests/test_repro_x.py", committed=True),
                calls=calls)
    workspace = _ws(tmp_path)
    seed = PipelineCursor(workspace)
    seed.start(run_id="r1", objective_goal="fix double bug")
    seed.mark_repro_path("tests/test_repro_x.py")
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        run_id="r1",
        objective=Objective(goal="fix double bug", done_when="pytest passes"),
        cursor=PipelineCursor(workspace), workspace=workspace, mode="fix",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert calls == [], "a resumed run never regenerates the repro"
    matching = [g for g in orch.gates
                if g.command == repro_check_command("tests/test_repro_x.py")]
    assert len(matching) == 1, "the proving gate is present exactly once"


def test_fix_mode_briefs_carry_the_repro_prohibition(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    _fake_repro(monkeypatch,
                ReproResult(path="tests/test_repro_x.py", committed=True))
    builder = Builder(FakeProvider(["not json", FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])), mode="fix",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    _system, build_prompt = builder.provider.calls[1]
    assert FIX_MODE_RULES in build_prompt
    assert "tests/test_repro_" in FIX_MODE_RULES, \
        "the rules explicitly name the protected repro pattern"
    for config_name in ("conftest.py", "pytest.ini", "pyproject.toml",
                        "setup.cfg", "tox.ini"):
        assert config_name in FIX_MODE_RULES, \
            f"the rules must also forbid pytest config gaming via {config_name}"


def test_fix_mode_briefs_compose_repo_context_before_the_rules(
        tmp_path, monkeypatch):
    # Rider 3: a fix brief carries BOTH the repo context (map + selected files,
    # the same once-per-run assembly enhance uses) AND the fix-mode rules —
    # repo context first, rules LAST so they are never truncated away.
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    counts = _counting_repomap(monkeypatch)
    _fake_repro(monkeypatch,
                ReproResult(path="tests/test_repro_x.py", committed=True))
    builder = Builder(FakeProvider(["not json", FILE_BLOCK]))
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])), mode="fix",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    _system, build_prompt = builder.provider.calls[1]
    assert "REPO MAP:\nMAPTEXT" in build_prompt
    assert "--- old.py ---\nOLD = 1" in build_prompt
    assert FIX_MODE_RULES in build_prompt
    assert build_prompt.index("MAPTEXT") < build_prompt.index(FIX_MODE_RULES), \
        "repo context comes before the rules"
    assert counts == {"map": 1, "select": 1}, \
        "the repomap primitives are assembled exactly once for the run"


def test_fix_mode_failed_repro_restore_blocks_the_run(tmp_path, monkeypatch):
    # Fail CLOSED: the workspace is a git repo but the repro path was never
    # committed, so the post-promote `git checkout -- <path>` fails — the
    # run must terminate BLOCKED instead of gating a possibly-tampered repro.
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    _fake_repro(monkeypatch,
                ReproResult(path="tests/test_repro_x.py", committed=True))
    notifier = FakeNotifier()
    workspace = _git_ws(tmp_path)
    cursor = PipelineCursor(workspace)
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        notifier=notifier, workspace=workspace, cursor=cursor, mode="fix",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.BLOCKED
    assert "restore" in outcome.reason and "tests/test_repro_x.py" in outcome.reason
    persisted = json.loads(open(cursor.path, encoding="utf-8").read())
    assert persisted["stage"] == "terminal" and persisted["status"] == "blocked"


def test_resume_prefers_persisted_mode_over_fresh_resolution(
        tmp_path, monkeypatch, caplog):
    # A fix run resumed with mode=auto would re-resolve to enhance on a
    # tracked repo and silently drop the repro re-arming; the persisted
    # cursor mode wins, with a visible warning.
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    counts = _counting_repomap(monkeypatch)
    calls = []
    _fake_repro(monkeypatch,
                ReproResult(path="tests/test_repro_x.py", committed=True),
                calls=calls)
    workspace = _git_ws(tmp_path, files={
        "existing.py": "OLD = 1\n",
        "tests/test_repro_x.py": "def test_x():\n    assert True\n",
    })
    seed = PipelineCursor(workspace)
    seed.start(run_id="r1", objective_goal="fix double bug")
    seed.record_mode("fix")
    seed.mark_repro_path("tests/test_repro_x.py")
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        run_id="r1",
        objective=Objective(goal="fix double bug", done_when="pytest passes"),
        cursor=PipelineCursor(workspace), workspace=workspace, mode="auto",
    )
    with caplog.at_level(logging.WARNING):
        outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert orch.mode == "fix", "the persisted mode wins on resume"
    # Fix mode (like enhance) now assembles repo context once for the run
    # (rider 3); the mode is proven by the repro re-arming below, not by the
    # absence of repomap calls.
    assert counts == {"map": 1, "select": 1}
    assert calls == [], "the repro was re-armed, not regenerated"
    matching = [g for g in orch.gates
                if g.command == repro_check_command("tests/test_repro_x.py")]
    assert len(matching) == 1
    assert any("mode" in r.getMessage() and "fix" in r.getMessage()
               for r in caplog.records), "the mode mismatch is warned about"


# --- Audit mode: read-only reviewer sweep -> findings.md, short-circuit ---


def _hash_tree(workspace):
    """Snapshot every file OUTSIDE .loop-engineer/ as path -> (mtime, bytes)."""
    import hashlib
    seen = {}
    for base, _dirs, names in os.walk(workspace):
        rel_base = os.path.relpath(base, workspace)
        if rel_base == ".loop-engineer" or rel_base.startswith(
                ".loop-engineer" + os.sep):
            continue
        for name in names:
            full = os.path.join(base, name)
            with open(full, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
            seen[os.path.relpath(full, workspace)] = (
                os.stat(full).st_mtime_ns, digest)
    return seen


def _planted_defect_ws(tmp_path):
    """A git repo with a PLANTED DEFECT the fake reviewer will flag."""
    return _git_ws(tmp_path, files={
        "buggy.py": "def divide(a, b):\n    return a / 0  # planted defect\n",
    })


def test_audit_short_circuits_decompose_ensemble_worktrees_promote(
        tmp_path, monkeypatch):
    gate_calls = []

    def counting_gates(specs, ws_arg, policy=None):
        gate_calls.append((list(specs), ws_arg))
        return _pass_gates(specs, ws_arg, policy)

    monkeypatch.setattr(orchestrator, "run_gates", counting_gates)
    _counting_repomap(monkeypatch)
    builder = Builder(FakeProvider([]))
    workspace = _git_ws(tmp_path)
    orch = _make(
        tmp_path, builder=builder,
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        workspace=workspace, mode="audit",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert builder.provider.calls == [], "no decompose, no build rounds"
    assert orch.worktrees.created == [], "no attempt worktrees"
    assert orch.worktrees.promoted == [], "no promote"
    assert gate_calls == [], "the build pipeline's gate runner never fires"


def test_audit_outcome_shape_with_zero_findings(tmp_path, monkeypatch):
    _counting_repomap(monkeypatch)
    workspace = _git_ws(tmp_path)
    orch = _make(
        tmp_path, builder=Builder(FakeProvider([])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        workspace=workspace, mode="audit",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert outcome.reason == "audit complete: 0 findings"
    findings_md = os.path.join(workspace, ".loop-engineer", "findings.md")
    assert outcome.evidence == [{"findings_md": findings_md, "count": 0}]
    assert os.path.isfile(findings_md)


def test_audit_outcome_counts_findings(tmp_path, monkeypatch):
    _counting_repomap(monkeypatch)
    workspace = _git_ws(tmp_path)
    orch = _make(
        tmp_path, builder=Builder(FakeProvider([])),
        reviewer=Reviewer(FakeProvider(
            ["FINDING: old.py: off-by-one\nFINDING: old.py: unchecked input"])),
        workspace=workspace, mode="audit",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert outcome.reason == "audit complete: 2 findings"
    assert outcome.evidence[0]["count"] == 2


def test_audit_pipeline_flags_planted_defect_with_file_ref(tmp_path):
    # Spec DoD: audit produces .loop-engineer/findings.md whose entries carry
    # file references (tested with planted defects). Real repomap selection
    # against the fixture repo; the fake reviewer flags the planted bug.
    workspace = _planted_defect_ws(tmp_path)
    reviewer_provider = FakeProvider(
        ["FINDING: buggy.py: divide() divides by zero (planted defect)"])
    orch = _make(
        tmp_path, builder=Builder(FakeProvider([])),
        reviewer=Reviewer(reviewer_provider),
        objective=Objective(goal="audit divide in buggy module for bugs",
                            done_when="all defects reported"),
        workspace=workspace, mode="audit",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert outcome.reason == "audit complete: 1 findings"
    # The reviewer actually saw the planted file's contents.
    (_system, prompt), = reviewer_provider.calls
    assert "return a / 0" in prompt
    text = open(os.path.join(workspace, ".loop-engineer", "findings.md"),
                encoding="utf-8").read()
    assert "### buggy.py" in text
    assert "divides by zero" in text


def test_audit_writes_nothing_outside_loop_engineer(tmp_path):
    # Spec DoD: no file outside <workspace>/.loop-engineer/ is created or
    # modified — no source-tree writes, no worktrees, no promote.
    workspace = _planted_defect_ws(tmp_path)
    orch = _make(
        tmp_path, builder=Builder(FakeProvider([])),
        reviewer=Reviewer(FakeProvider(
            ["FINDING: buggy.py: divide() divides by zero"])),
        objective=Objective(goal="audit divide in buggy module for bugs",
                            done_when="all defects reported"),
        workspace=workspace, mode="audit",
    )
    before = _hash_tree(workspace)
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert _hash_tree(workspace) == before, \
        "no file outside .loop-engineer/ was created or modified"
    assert orch.worktrees.created == []
    assert os.path.isfile(
        os.path.join(workspace, ".loop-engineer", "findings.md"))


def test_audit_zero_selected_files_falls_back_to_all_tracked(tmp_path):
    # An objective whose keywords match nothing still sweeps the repo.
    workspace = _git_ws(tmp_path)  # tracked existing.py, unrelated goal words
    reviewer_provider = FakeProvider(["NO FINDINGS"])
    orch = _make(
        tmp_path, builder=Builder(FakeProvider([])),
        reviewer=Reviewer(reviewer_provider),
        objective=Objective(goal="zzzz qqqq", done_when="wwww"),
        workspace=workspace, mode="audit",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    (_system, prompt), = reviewer_provider.calls
    assert "--- existing.py ---" in prompt
    assert "OLD = 1" in prompt


def test_audit_ledgers_every_reviewer_call(tmp_path, monkeypatch):
    _counting_repomap(monkeypatch)
    ledger = CostLedger(path=str(tmp_path / "ledger.json"), month="2026-07")
    workspace = _git_ws(tmp_path)
    orch = _make(
        tmp_path, builder=Builder(FakeProvider([])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"], cost_usd=0.03)),
        ledger=ledger, workspace=workspace, mode="audit",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert ledger.run_total() == pytest.approx(0.03)
    persisted = json.loads(open(ledger.path, encoding="utf-8").read())
    assert persisted["months"]["2026-07"] == pytest.approx(0.03)


def test_audit_read_only_gates_run_in_the_main_workspace(tmp_path, monkeypatch):
    _counting_repomap(monkeypatch)
    policy = FakePolicy(passed=True)
    workspace = _git_ws(tmp_path)
    orch = _make(
        tmp_path, builder=Builder(FakeProvider([])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        gates=[GateSpec(name="unit", command="pytest -q"),
               GateSpec(name="security", command="bandit -r .")],
        policy=policy, workspace=workspace, mode="audit",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert policy.runs == [("bandit -r .", workspace)], \
        "only the read-only gate ran, and in the main workspace"
    text = open(os.path.join(workspace, ".loop-engineer", "findings.md"),
                encoding="utf-8").read()
    assert "PASS security" in text


def test_audit_budget_cap_stops_before_any_reviewer_call(tmp_path, monkeypatch):
    _counting_repomap(monkeypatch)
    ledger = CostLedger(path=str(tmp_path / "ledger.json"), run_cap_usd=0.01,
                        month="2026-07")
    ledger.record(0.02)  # the cap is already spent
    reviewer_provider = FakeProvider(["NO FINDINGS"])
    workspace = _git_ws(tmp_path)
    orch = _make(
        tmp_path, builder=Builder(FakeProvider([])),
        reviewer=Reviewer(reviewer_provider),
        ledger=ledger, workspace=workspace, mode="audit",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.BUDGET_EXCEEDED
    assert reviewer_provider.calls == []


def test_audit_provider_failure_is_blocked(tmp_path, monkeypatch):
    _counting_repomap(monkeypatch)
    ledger = CostLedger(path=str(tmp_path / "ledger.json"), month="2026-07")
    workspace = _git_ws(tmp_path)
    orch = _make(
        tmp_path, builder=Builder(FakeProvider([])),
        reviewer=Reviewer(RaisingProvider([])),
        ledger=ledger, workspace=workspace, mode="audit",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.BLOCKED
    assert "provider failure" in outcome.reason
    assert ledger.run_total() == 0.0, \
        "the very first call raised — no spend, so nothing to ledger"


def test_audit_retry_failure_partial_spend_is_ledgered(tmp_path, monkeypatch):
    # The chunk review's FIRST call completes (and costs money) but is
    # malformed; the corrective format retry then dies with a ProviderError.
    # The first call's spend must still reach the ledger via the exception's
    # attached partial result (the M1 money invariant).
    _counting_repomap(monkeypatch)
    ledger = CostLedger(path=str(tmp_path / "ledger.json"), month="2026-07")
    workspace = _git_ws(tmp_path)
    orch = _make(
        tmp_path, builder=Builder(FakeProvider([])),
        reviewer=Reviewer(RaisingProvider(["prose matching neither format"],
                                          cost_usd=0.05)),
        ledger=ledger, workspace=workspace, mode="audit",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.BLOCKED
    assert "provider failure" in outcome.reason
    assert ledger.run_total() == pytest.approx(0.05)


# --- M4 riders: audit proven-guard (r1) + per-chunk budget stop (r2) -------


def test_audit_success_does_not_record_verifier_proven(tmp_path, monkeypatch):
    # An audit proves nothing — its gates are prove=False — so a SUCCESS audit
    # must NOT record the verifier-proven fact (build/enhance/fix still do).
    _counting_repomap(monkeypatch)
    memory = Memory(str(tmp_path / "mem.db"))
    workspace = _git_ws(tmp_path)
    orch = _make(
        tmp_path, builder=Builder(FakeProvider([])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        memory=memory, workspace=workspace, mode="audit",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert memory.get("verifier.proven") is None


def test_audit_budget_stop_mid_sweep_stays_success_with_escalate_event(
        tmp_path, monkeypatch):
    # The cap trips AFTER chunk 1: the sweep stops cleanly, the outcome stays
    # SUCCESS with the completed findings (advisory — never discarded), and an
    # escalate-level audit_budget_stop event tells the operator the cap bound.
    _counting_repomap(monkeypatch,
                      files=(("a.py", "x" * 30000), ("b.py", "y" * 30000)))
    ledger = CostLedger(path=str(tmp_path / "ledger.json"),
                        run_cap_usd=0.02, month="2026-07")
    notifier = FakeNotifier()
    workspace = _git_ws(tmp_path)
    orch = _make(
        tmp_path, builder=Builder(FakeProvider([])),
        reviewer=Reviewer(FakeProvider(
            ["FINDING: a.py: x", "FINDING: b.py: y"], cost_usd=0.03)),
        ledger=ledger, notifier=notifier, workspace=workspace, mode="audit",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert "budget" in outcome.reason.lower()
    assert len(orch.reviewer.provider.calls) == 1, "only chunk 1 was reviewed"
    stops = [e for e in notifier.events if e["event"] == "audit_budget_stop"]
    assert len(stops) == 1
    assert stops[0]["level"] == "escalate"
    assert stops[0]["payload"]["cap_usd"] == 0.02
    assert outcome.evidence[0]["count"] == 1, "completed findings are kept"
    text = open(os.path.join(workspace, ".loop-engineer", "findings.md"),
                encoding="utf-8").read()
    assert "TRUNCATED" in text


# ---------------------------------------------------------------------------
# D1: diverse ensembles — per-attempt specs, injected builder factory, and the
# two-layer cross-model guard (layer 2: review-time winner identity).
# ---------------------------------------------------------------------------


def test_default_specs_give_attempts_different_temperatures(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    seen = []

    def factory(spec):
        seen.append(spec.temperature)
        return Builder(FakeProvider([FILE_BLOCK]))

    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json"])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        ensemble_n=2, attempt_specs=default_specs(2), builder_factory=factory,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert seen == [0.2, 0.7], "each attempt's builder gets its OWN temperature"


def test_attempt_specs_ride_on_the_ensemble_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    specs = default_specs(2)
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK, FILE_BLOCK])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        ensemble_n=2, attempt_specs=specs,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS


def test_without_factory_every_attempt_uses_the_shared_builder(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    provider = FakeProvider(["not json", FILE_BLOCK, FILE_BLOCK])
    orch = _make(
        tmp_path,
        builder=Builder(provider),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        ensemble_n=2,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert len(provider.calls) == 3  # decompose + both attempts on ONE provider


def test_per_attempt_builder_spend_is_ledgered(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    ledger = CostLedger(path=str(tmp_path / "ledger.json"), month="2026-07")

    def factory(spec):
        return Builder(FakeProvider([FILE_BLOCK], cost_usd=0.02))

    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json"])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        ledger=ledger, ensemble_n=2,
        attempt_specs=default_specs(2), builder_factory=factory,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert ledger.run_total() == pytest.approx(0.04), \
        "every per-attempt builder's spend reaches the ledger"


def test_review_time_guard_blocks_same_family_winner(tmp_path, monkeypatch):
    # The reviewer was constructed against a kimi LABEL, but the factory
    # actually wires a qwen provider (the reviewer's family) into the winning
    # attempt. The layer-2 check reads the constructed provider OBJECT, so the
    # label must not bypass builder != reviewer.
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    cross = CrossModelReviewer("kimi-claimed-label", "qwen2.5",
                               Reviewer(FakeProvider(["NO FINDINGS"])))

    def factory(spec):
        return Builder(FakeProvider([FILE_BLOCK], model="qwen2.5-coder"))

    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json"])),
        reviewer=cross,
        ensemble_n=1, attempt_specs=[AttemptSpec(model="kimi-claimed-label")],
        builder_factory=factory,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.BLOCKED
    assert "cross-model" in outcome.reason
    assert orch.worktrees.promoted == []


def test_review_time_guard_violation_cleans_up_attempt_worktrees(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    cross = CrossModelReviewer("kimi-claimed-label", "qwen2.5",
                               Reviewer(FakeProvider(["NO FINDINGS"])))

    def factory(spec):
        return Builder(FakeProvider([FILE_BLOCK], model="qwen2.5-coder"))

    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json"])),
        reviewer=cross,
        ensemble_n=2, attempt_specs=default_specs(2), builder_factory=factory,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.BLOCKED
    assert orch.worktrees.created, "the ensemble did build attempts"
    assert set(orch.worktrees.removed) >= set(orch.worktrees.created), \
        "a guard violation must never leak attempt worktrees"


def test_review_time_guard_spec_family_declares_independence(tmp_path, monkeypatch):
    # An unknown third-party model wins; its spec DECLARES a family distinct
    # from the reviewer's, so the layer-2 check passes and the run promotes.
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    cross = CrossModelReviewer("kimi-k2.7-code", "qwen2.5",
                               Reviewer(FakeProvider(["NO FINDINGS"])))

    def factory(spec):
        return Builder(FakeProvider([FILE_BLOCK], model="mystery-w"))

    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json"])),
        reviewer=cross,
        ensemble_n=1, attempt_specs=[AttemptSpec(model="mystery-w", family="alpha")],
        builder_factory=factory,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS
    assert len(orch.worktrees.promoted) == 1


def test_review_time_guard_two_unknown_families_still_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    cross = CrossModelReviewer("kimi-k2.7-code", "mystery-r",
                               Reviewer(FakeProvider(["NO FINDINGS"])))

    def factory(spec):
        return Builder(FakeProvider([FILE_BLOCK], model="mystery-w"))

    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json"])),
        reviewer=cross,
        ensemble_n=1, attempt_specs=[AttemptSpec(model="mystery-w")],
        builder_factory=factory,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.BLOCKED
    assert "cross-model" in outcome.reason
