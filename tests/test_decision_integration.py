"""3.2 staffing controller wired through config, orchestrator, and ``next``."""
import json
import os

import pytest

from kickass_loop_engineer import orchestrator
from kickass_loop_engineer.agents import Builder, Reviewer
from kickass_loop_engineer.config import EXAMPLE_CONFIG, build_decision
from kickass_loop_engineer.decision.base import DecisionError
from kickass_loop_engineer.decision.chain import DecisionChain
from kickass_loop_engineer.decision.controller import configured_review
from kickass_loop_engineer.decision.log import DecisionLog
from kickass_loop_engineer.decision.settings import DecisionSettings, parse_settings
from kickass_loop_engineer.decision.staffing import TESTS_CONSTRAINT, StaffingPlan
from kickass_loop_engineer.ensemble import AttemptSpec
from kickass_loop_engineer.guardrails import SCRUBBED_ENV_KEYS
from kickass_loop_engineer.next import emit_next
from kickass_loop_engineer.objective import Objective
from kickass_loop_engineer.terminal import TerminalState

from helpers import FakeDecisionBackend, FakeProvider
from test_next import _seed_engine_terminal, _seed_plan, _write
from test_orchestrator import FILE_BLOCK, _fail_gates, _make, _pass_gates

ONE_SLICE = '[{"role": "core", "objective": "core objective"}]'


def _armed(tmp_path, backend, *, settings=None, builder_responses=None,
           objective=None, **kw):
    """An orchestrator armed through the REAL constructor path."""
    return _make(tmp_path,
                 builder=Builder(FakeProvider(builder_responses
                                              or [ONE_SLICE] + [FILE_BLOCK] * 6)),
                 reviewer=Reviewer(FakeProvider(["NO FINDINGS"] * 6)),
                 objective=objective,
                 decision_settings=settings or DecisionSettings(backend="jev"),
                 decision_chain=DecisionChain([backend]), **kw)


def _plan(ws):
    with open(os.path.join(ws, ".loop-engineer", "staffing.json"), encoding="utf-8") as fh:
        return json.load(fh)


def test_disarmed_orchestrator_writes_no_staffing(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    orch = _make(tmp_path, builder=Builder(FakeProvider([ONE_SLICE, FILE_BLOCK])),
                 reviewer=Reviewer(FakeProvider(["NO FINDINGS"])))
    assert orch.run().state is TerminalState.SUCCESS
    assert not os.path.exists(os.path.join(orch.workspace, ".loop-engineer",
                                           "staffing.json"))


def test_model_can_shrink_the_ensemble_to_one_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    backend = FakeDecisionBackend({"attempts_0_core": "1"})
    orch = _armed(tmp_path, backend, ensemble_n=3)
    assert orch.run().state is TerminalState.SUCCESS
    assert orch._total_attempts == 1
    assert _plan(orch.workspace)["slices"]["core"] == {"attempts": 1, "tier": 0}


def test_tier_cascade_stops_at_first_passing_tier(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    tiers = (AttemptSpec(model="cheap"), AttemptSpec(model="strong"))
    built = []
    orch = _armed(tmp_path, FakeDecisionBackend({"attempts_0_core": "2"}),
                  settings=DecisionSettings(tiers=tiers))
    orch.builder_factory = lambda spec: (built.append(spec.model), orch.builder)[1]
    assert orch.run().state is TerminalState.SUCCESS
    assert built == ["cheap"], "the strong tier is never paid for"


def test_stall_recovery_climbs_one_tier_when_model_says_retry(tmp_path, monkeypatch):
    calls = {"n": 0}

    def gates(specs, workspace, policy=None):
        calls["n"] += 1
        return (_fail_gates if calls["n"] == 1 else _pass_gates)(specs, workspace, policy)

    monkeypatch.setattr(orchestrator, "run_gates", gates)
    tiers = (AttemptSpec(model="cheap"), AttemptSpec(model="strong"))
    built = []
    orch = _armed(tmp_path, FakeDecisionBackend({"attempts_0_core": "1",
                                                 "recover_core": "retry"}),
                  settings=DecisionSettings(tiers=tiers))
    orch.builder_factory = lambda spec: (built.append(spec.model), orch.builder)[1]
    assert orch.run().state is TerminalState.SUCCESS
    assert built == ["cheap", "strong"]


def test_stall_without_retry_decision_still_stalls(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _fail_gates)
    orch = _armed(tmp_path, FakeDecisionBackend({"attempts_0_core": "1"}))
    assert orch.run().state is TerminalState.STALLED
    assert orch._total_attempts == 1


def test_sensitive_objective_tightens_review_and_requires_tests(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    backend = FakeDecisionBackend({"risk": "low", "security": "L1", "review": "light",
                                   "qa": "skip", "tests": "gates_only"})
    orch = _armed(tmp_path, backend, objective=Objective(
        goal="add stripe payment checkout", done_when="pytest passes"))
    orch.run()
    plan = _plan(orch.workspace)
    assert plan["security"] == "L3" and plan["qa"] is True
    assert set(orch.review_block_on) == {"HIGH", "CRITICAL"}


def test_write_tests_choice_reaches_the_builder_constraints(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    orch = _armed(tmp_path, FakeDecisionBackend({"tests": "write_tests"}))
    orch.run()
    assert TESTS_CONSTRAINT in orch.objective.constraints


def test_backend_outage_falls_back_to_config_behavior(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    orch = _armed(tmp_path, FakeDecisionBackend(error=DecisionError("down")),
                  ensemble_n=2)
    assert orch.run().state is TerminalState.SUCCESS
    assert orch._total_attempts == 2
    records = DecisionLog(orch.workspace).records()
    assert {r["backend"] for r in records if r["kind"] == "decision"} == {"rules"}
    assert records[-1]["kind"] == "outcome" and records[-1]["state"] == "success"


def test_plan_skips_paid_stages_and_reports_them(tmp_path):
    ws = str(tmp_path)
    _seed_plan(ws)
    _seed_engine_terminal(ws)
    _write(ws, ".loop-engineer/review.md", "NO FINDINGS\n")
    StaffingPlan(run_id="run-42", security="L1", qa=False, devops=False).save(ws)
    env = emit_next(ws)
    assert env["next_action"]["type"] == "terminal"
    assert set(env["skipped_stages"]) == {"qa", "security", "ship"}
    assert env["staffing"]["skipped_by_plan"] == ["qa", "security", "ship"]


def test_l4_plan_requires_human_signoff_before_ship(tmp_path):
    ws = str(tmp_path)
    _seed_plan(ws)
    _seed_engine_terminal(ws)
    for rel, text in ((".loop-engineer/review.md", "NO FINDINGS\n"),
                      (".loop-engineer/qa.md", "VERDICT: PASS\n"),
                      (".loop-engineer/security.md", "VERDICT: PASS\n")):
        _write(ws, rel, text)
    StaffingPlan(run_id="run-42", security="L4", domains=["payments"]).save(ws)
    env = emit_next(ws)
    assert env["next_action"]["type"] == "ask_user"
    assert env["status"] == "signoff_required"
    _write(ws, ".loop-engineer/signoff.md", "APPROVED: Meet\n")
    assert emit_next(ws)["stage"] == "ship"


def test_build_decision_is_off_without_a_section():
    assert build_decision({}) == (None, None)


def test_build_decision_parses_tiers_and_floors():
    settings, chain = build_decision({"decision": {
        "backend": "rules", "min_confidence": 0.8, "floors": {"security": "L2"},
        "signoff_domains": ["payments"], "tiers": [{"model": "a"}, {"model": "b"}]}})
    assert settings.security_min == "L2" and settings.tier_names() == ("t0", "t1")
    assert [b.name for b in chain.backends] == ["rules"]


@pytest.mark.parametrize("section", [
    {"backend": "gpt"}, {"floors": {"security": "L9"}}, {"min_confidence": 2},
    {"signoff_domains": ["weather"]}, {"max_attempts": 0},
])
def test_bad_decision_config_fails_fast(section):
    with pytest.raises(RuntimeError):
        parse_settings(section, ())


def test_example_config_documents_the_decision_section():
    assert "# decision:" in EXAMPLE_CONFIG and "jev-1.13.0" in EXAMPLE_CONFIG


def test_decision_key_never_reaches_verification_subprocesses():
    assert "TYPESAFE_API_KEY" in SCRUBBED_ENV_KEYS


@pytest.mark.parametrize("block_on, expected", [
    ((), "light"), (("CRITICAL",), "standard"), (("high", "CRITICAL"), "strict")])
def test_configured_review_depth(block_on, expected):
    assert configured_review(block_on) == expected
