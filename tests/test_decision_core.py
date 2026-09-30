"""Decision-layer core: answer validation, rules, chain, redaction, broker, log."""
import pytest

from kickass_loop_engineer.decision.base import (DecisionError, Question,
                                                 validate_answer)
from kickass_loop_engineer.decision.broker import DecisionBroker
from kickass_loop_engineer.decision.chain import DecisionChain
from kickass_loop_engineer.decision.log import DecisionLog, decision_key
from kickass_loop_engineer.decision.redact import (MAX_LIST_ITEMS, MAX_TEXT_CHARS,
                                                   build_state)
from kickass_loop_engineer.decision.rules import RulesBackend

from helpers import FakeDecisionBackend

Q = Question("qa", "run qa?", ("run", "skip"), "run")


@pytest.mark.parametrize("choice, probs, confidence", [
    ("maybe", {"run": 1.0}, 0.9),                      # undeclared choice
    ("run", None, 0.9),                                # probabilities missing
    ("run", {"run": 0.5, "other": 0.5}, 0.9),          # undeclared option
    ("run", {"run": 0.5, "skip": 0.1}, 0.9),           # does not sum to 1
    ("run", {"run": 1.2, "skip": -0.2}, 0.9),          # outside [0, 1]
    ("run", {"run": float("nan"), "skip": 0.0}, 0.9),  # NaN
    ("run", {"run": 1.0, "skip": 0.0}, 1.5),           # confidence > 1
    ("run", {"run": 1.0, "skip": 0.0}, True),          # boolean confidence
    ("run", {"run": 1.0, "skip": 0.0}, "high"),        # non-numeric
])
def test_should_reject_malformed_answers(choice, probs, confidence):
    with pytest.raises(DecisionError):
        validate_answer(Q, choice, probs, confidence)


def test_should_normalize_near_one_probabilities_over_declared_options():
    ans = validate_answer(Q, "skip", {"skip": 0.99}, "0.8")
    assert ans.choice == "skip"
    assert ans.probabilities == pytest.approx({"run": 0.0, "skip": 1.0})
    assert ans.confidence == 0.8


def test_should_reject_a_question_whose_default_is_not_an_option():
    with pytest.raises(ValueError):
        Question("x", "?", ("a", "b"), "c")


def test_rules_backend_answers_every_default():
    batch = RulesBackend().decide({}, [Q, Question("r", "?", ("low", "high"), "high")])
    assert {k: a.choice for k, a in batch.answers.items()} == {"qa": "run", "r": "high"}


def test_chain_should_fall_back_to_rules_when_backend_fails():
    chain = DecisionChain([FakeDecisionBackend(error=DecisionError("down"))])
    batch = chain.decide({}, [Q])
    assert batch.backend == "rules" and batch.answers["qa"].choice == "run"
    assert chain.fallbacks == [("fake-decider", "down")]


def test_chain_breaker_should_stop_calling_a_failing_backend():
    failing = FakeDecisionBackend(error=DecisionError("down"))
    chain = DecisionChain([failing], breaker_threshold=2)
    for _ in range(5):
        chain.decide({}, [Q])
    assert len(failing.calls) == 2


def test_state_should_drop_keys_outside_the_allowlist_and_truncate():
    state = build_state({"objective": "x" * (MAX_TEXT_CHARS + 50),
                         "slices": list(range(MAX_LIST_ITEMS + 5)),
                         "stdout_tail": "leak", "env": {"HOME": "/root"}})
    assert set(state) == {"objective", "slices"}
    assert len(state["objective"]) == MAX_TEXT_CHARS
    assert len(state["slices"]) == MAX_LIST_ITEMS


@pytest.mark.parametrize("secret", [
    "sk-ant-api03-abcdefghijklmnopqrstuv",
    "AKIAABCDEFGHIJKLMNOP",
    "-----BEGIN RSA PRIVATE KEY-----",
    "password = hunter2hunter2",
    "ghp_" + "a" * 36,
])
def test_state_should_fail_closed_on_secret_shaped_values(secret):
    with pytest.raises(DecisionError):
        build_state({"objective": f"use {secret} to deploy"})


def _broker(tmp_path, backend, **kw):
    return DecisionBroker(DecisionChain([backend]), DecisionLog(str(tmp_path)),
                          run_id="r1", **kw)


def test_broker_should_apply_confident_model_choice(tmp_path):
    broker = _broker(tmp_path, FakeDecisionBackend({"qa": "skip"}, confidence=0.9))
    assert broker.decide("staffing", {"objective": "docs"}, [Q]) == {"qa": "skip"}


def test_broker_should_use_default_below_min_confidence(tmp_path):
    broker = _broker(tmp_path, FakeDecisionBackend({"qa": "skip"}, confidence=0.4),
                     min_confidence=0.7)
    assert broker.decide("staffing", {"objective": "docs"}, [Q]) == {"qa": "run"}
    record = DecisionLog(str(tmp_path)).records()[-1]
    assert record["answers"]["qa"]["choice"] == "skip"
    assert record["answers"]["qa"]["accepted"] is False


def test_broker_should_replay_recorded_decision_without_calling_backend(tmp_path):
    first = FakeDecisionBackend({"qa": "skip"})
    _broker(tmp_path, first).decide("staffing", {"objective": "docs"}, [Q])
    second = FakeDecisionBackend({"qa": "run"})
    applied = _broker(tmp_path, second).decide("staffing", {"objective": "docs"}, [Q])
    assert applied == {"qa": "skip"} and second.calls == []


def test_broker_should_meter_paid_calls(tmp_path):
    spent = []
    broker = _broker(tmp_path, FakeDecisionBackend(cost_usd=0.00001),
                     on_cost=lambda site, batch: spent.append((site, batch.cost_usd)))
    broker.decide("staffing", {"objective": "docs"}, [Q])
    assert spent == [("decide:staffing", 0.00001)]


def test_broker_should_never_send_a_rejected_state_to_the_model(tmp_path):
    model = FakeDecisionBackend({"qa": "skip"})
    events = []
    broker = _broker(tmp_path, model, on_event=lambda e, p: events.append(e))
    applied = broker.decide("staffing", {"objective": "AKIAABCDEFGHIJKLMNOP"}, [Q])
    assert applied == {"qa": "run"} and model.calls == []
    assert "decision_state_rejected" in events


def test_log_should_skip_corrupt_lines(tmp_path):
    log = DecisionLog(str(tmp_path))
    log.append({"kind": "decision", "run_id": "r1", "key": "k"})
    with open(log.path, "a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    assert len(log.records()) == 1


def test_decision_key_should_change_with_options():
    other = Question("qa", "run qa?", ("run", "skip", "light"), "run")
    assert decision_key({}, [Q]) != decision_key({}, [other])
