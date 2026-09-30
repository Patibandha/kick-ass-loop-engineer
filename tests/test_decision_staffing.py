"""Staffing: floors can never be undercut, whatever the model chooses."""
import itertools
from types import SimpleNamespace

import pytest

from kickass_loop_engineer.decision.floors import (REVIEW_LEVELS, SECURITY_LEVELS,
                                                   detect_domains)
from kickass_loop_engineer.decision.staffing import (RISK_LEVELS, StaffingPlan,
                                                     apply_run_choices, load_plan,
                                                     run_questions, slice_questions)

RANK_S = {lvl: i for i, lvl in enumerate(SECURITY_LEVELS)}
RANK_R = {lvl: i for i, lvl in enumerate(REVIEW_LEVELS)}


@pytest.mark.parametrize("text, expected", [
    ("add a stripe checkout page", ["payments"]),
    ("fix the login session timeout", ["auth"]),
    ("write a database migration and deploy it", ["data", "infra"]),
    ("rename a variable in the README", []),
    ("improve the authoring guide", []),  # 'auth' must match on word boundary
])
def test_detect_domains(text, expected):
    assert detect_domains(text) == expected


def _apply(choices, domains, **kw):
    return apply_run_choices(StaffingPlan(run_id="r"), choices, domains=domains,
                             security_min=kw.get("security_min", "L1"),
                             review_min=kw.get("review_min", "light"),
                             signoff_domains=kw.get("signoff", ()))


@pytest.mark.parametrize("domains", [[], ["data"], ["auth"], ["payments", "infra"]])
def test_no_model_choice_can_undercut_the_floors(domains):
    """Exhaustive over every run-level choice combination (the property test)."""
    for risk, sec, rev, qa in itertools.product(RISK_LEVELS, SECURITY_LEVELS,
                                                REVIEW_LEVELS, ("run", "skip")):
        plan = _apply({"risk": risk, "security": sec, "review": rev, "qa": qa,
                       "devops": "skip", "tests": "gates_only"}, domains)
        assert RANK_S[plan.security] >= RANK_S[sec]
        if domains:
            assert RANK_S[plan.security] >= RANK_S["L2"]
            assert plan.qa and RANK_R[plan.review] >= RANK_R["standard"]
            assert plan.risk != "low"
        if {"auth", "payments", "secrets"} & set(domains) or risk == "critical":
            assert plan.security in ("L3", "L4") and plan.review == "strict"


def test_low_risk_docs_change_is_staffed_lean():
    plan = _apply({"risk": "low", "security": "L1", "review": "light", "qa": "skip",
                   "devops": "skip", "tests": "gates_only"}, [])
    assert (plan.security, plan.review, plan.qa, plan.devops, plan.write_tests) == \
        ("L1", "light", False, False, False)


def test_signoff_domain_forces_l4():
    plan = _apply({"security": "L1"}, ["payments"], signoff=("payments",))
    assert plan.security == "L4"


def test_config_minimums_bind():
    plan = _apply({"security": "L1", "review": "light"}, [],
                  security_min="L2", review_min="standard")
    assert (plan.security, plan.review) == ("L2", "standard")


def test_high_risk_forces_tests():
    assert _apply({"risk": "high", "tests": "gates_only"}, []).write_tests


def test_block_on_is_a_union_with_config():
    plan = StaffingPlan(run_id="r", review="standard")
    assert plan.block_on(("HIGH",)) == ("HIGH", "CRITICAL")


def test_run_questions_defaults_reproduce_pre_3_2_behavior():
    qs = {q.id: q for q in run_questions(slice_count=2, architect_auto=True,
                                         configured_review="light")}
    assert qs["architect"].default == "run" and qs["qa"].default == "run"
    assert qs["review"].default == "light"
    single = {q.id for q in run_questions(slice_count=1, architect_auto=False,
                                          configured_review="light")}
    assert "architect" not in single


def test_slice_questions_are_unique_and_bounded():
    slices = [SimpleNamespace(role="API layer", objective="x"),
              SimpleNamespace(role="api-layer", objective="y")]
    qs, ids = slice_questions(slices, max_attempts=3, default_attempts=5,
                              tier_names=("t0", "t1"))
    assert len({q.id for q in qs}) == 4
    assert qs[0].options == ("1", "2", "3") and qs[0].default == "3"
    assert ids["API layer"][1].startswith("tier_")


def test_plan_round_trips_through_the_artifact(tmp_path):
    plan = StaffingPlan(run_id="r", security="L3", qa=False)
    plan.save(str(tmp_path))
    assert load_plan(str(tmp_path))["security"] == "L3"
    assert load_plan(str(tmp_path / "missing")) is None
