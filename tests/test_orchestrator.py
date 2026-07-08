"""Tests for the live pipeline orchestrator.

Fakes throughout: FakeProvider-backed Builder/Reviewer, a FakeWorktrees that
creates real tmp dirs and records promote/remove, a FakeNotifier that records
events, and monkeypatched ``orchestrator.run_gate`` for deterministic gate
outcomes. Real CostLedger/Memory/Playbook/PipelineCursor against tmp_path.
"""
import json
import os
import subprocess

import pytest

from kickass_loop_engineer import orchestrator
from kickass_loop_engineer.agents import Builder, Reviewer
from kickass_loop_engineer.ledger import CostLedger
from kickass_loop_engineer.memory import Memory
from kickass_loop_engineer.objective import Objective
from kickass_loop_engineer.orchestrator import GateSpec, Orchestrator
from kickass_loop_engineer.playbook import Playbook
from kickass_loop_engineer.cursor import PipelineCursor
from kickass_loop_engineer.providers.base import ProviderError
from kickass_loop_engineer.terminal import TerminalState
from kickass_loop_engineer.verifier import EvidenceRecord
from kickass_loop_engineer.worktree import WorktreeError

from helpers import FakeProvider

FILE_BLOCK = "=== FILE: out.py ===\nVALUE = 1\n=== END FILE ===\n"
TWO_SLICE_JSON = (
    '[{"role": "core", "objective": "core objective oc"},'
    ' {"role": "api", "objective": "api objective oa"}]'
)


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


def _pass_gate(name, command, workspace, with_proof=False):
    return EvidenceRecord(gate=name, command=command, passed=True,
                          evidence=f"exit=0 {workspace}")


def _fail_gate(name, command, workspace, with_proof=False):
    return EvidenceRecord(gate=name, command=command, passed=False, evidence="fail")


def _ws(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return str(ws)


def _make(tmp_path, *, builder, reviewer, notifier=None, ledger=None,
          objective=None, ensemble_n=1, run_id="r1", oscillation_window=2,
          cursor=None, memory=None, workspace=None, worktrees=None):
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
        cursor=cursor, gate=GateSpec(), ensemble_n=ensemble_n, run_id=run_id,
        oscillation_window=oscillation_window, prove=False,
    )
    return orch


def test_happy_path_returns_success_with_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gate", _pass_gate)
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
    monkeypatch.setattr(orchestrator, "run_gate", _pass_gate)
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
    monkeypatch.setattr(orchestrator, "run_gate", _fail_gate)
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
    monkeypatch.setattr(orchestrator, "run_gate", _pass_gate)
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
    monkeypatch.setattr(orchestrator, "run_gate", _pass_gate)
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
    monkeypatch.setattr(orchestrator, "run_gate", _pass_gate)
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

    def dispatch_gate(name, command, ws_arg, with_proof=False):
        passed = ws_arg != workspace  # per-attempt gates pass, final gate fails
        return EvidenceRecord(gate=name, command=command, passed=passed,
                              evidence="final failed" if not passed else "ok")

    monkeypatch.setattr(orchestrator, "run_gate", dispatch_gate)
    orch = _make(
        tmp_path,
        builder=Builder(FakeProvider(["not json", FILE_BLOCK])),
        reviewer=Reviewer(FakeProvider(["NO FINDINGS"])),
        ensemble_n=1, workspace=workspace,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.BLOCKED
    assert "final failed" in outcome.reason


def test_resume_skips_completed_slices(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gate", _pass_gate)
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
    monkeypatch.setattr(orchestrator, "run_gate", _pass_gate)
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
    monkeypatch.setattr(orchestrator, "run_gate", _pass_gate)
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
    monkeypatch.setattr(orchestrator, "run_gate", _pass_gate)
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
    monkeypatch.setattr(orchestrator, "run_gate", _pass_gate)
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
    monkeypatch.setattr(orchestrator, "run_gate", _pass_gate)
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
    monkeypatch.setattr(orchestrator, "run_gate", _pass_gate)
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


def test_promote_failure_yields_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gate", _pass_gate)
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
