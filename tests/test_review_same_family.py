"""reviewer.allow_same_family: an explicit, journaled waiver of the cross-model guard."""
import json
import logging
import os
from types import SimpleNamespace

import pytest

from kickass_loop_engineer import orchestrator
from kickass_loop_engineer.agents import Builder, Reviewer
from kickass_loop_engineer.config import build_orchestrator
from kickass_loop_engineer.objective import Objective
from kickass_loop_engineer.review import (CrossModelReviewError, CrossModelReviewer,
                                          ensure_cross_model)

from helpers import FakeProvider
from test_orchestrator import FILE_BLOCK, _make, _pass_gates

OPUS, FABLE = "claude-opus-5-5", "claude-fable-5-1"


def test_same_family_still_raises_by_default():
    with pytest.raises(CrossModelReviewError):
        ensure_cross_model(OPUS, FABLE)


def test_override_allows_same_family_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="kickass_loop_engineer.review"):
        assert ensure_cross_model(OPUS, FABLE, allow_same_family=True) == ("claude", "claude")
    assert "WAIVED" in caplog.text


def test_override_reaches_the_review_time_check():
    reviewer = CrossModelReviewer(OPUS, FABLE, Reviewer(FakeProvider([])),
                                  allow_same_family=True)
    reviewer.check(SimpleNamespace(model=OPUS, name="claude_code"))  # no raise


def test_config_wires_the_override_for_claude_builder_and_reviewer(tmp_path):
    config = {
        "builder": {"provider": "claude_code", "model": OPUS},
        "reviewer": {"provider": "claude_code", "model": FABLE, "allow_same_family": True},
    }
    orch = build_orchestrator(config, workspace=str(tmp_path), month="2026-10",
                              objective=Objective(goal="g", done_when="d"))
    assert orch.reviewer.allow_same_family is True
    del config["reviewer"]["allow_same_family"]
    with pytest.raises(CrossModelReviewError):
        build_orchestrator(config, workspace=str(tmp_path), month="2026-10",
                           objective=Objective(goal="g", done_when="d"))


def test_only_a_literal_true_waives_the_guard(tmp_path):
    config = {"builder": {"provider": "claude_code", "model": OPUS},
              "reviewer": {"provider": "claude_code", "model": FABLE,
                           "allow_same_family": "yes"}}
    with pytest.raises(CrossModelReviewError):
        build_orchestrator(config, workspace=str(tmp_path), month="2026-10",
                           objective=Objective(goal="g", done_when="d"))


def test_every_run_journals_the_waiver(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    reviewer = CrossModelReviewer(OPUS, FABLE, Reviewer(FakeProvider(["NO FINDINGS"])),
                                  allow_same_family=True)
    orch = _make(tmp_path, builder=Builder(FakeProvider(["not json", FILE_BLOCK])),
                 reviewer=reviewer)
    orch.run()
    events_path = os.path.join(orch.workspace, ".loop-engineer", "events.jsonl")
    with open(events_path, encoding="utf-8") as fh:
        events = [json.loads(line)["event"] for line in fh if line.strip()]
    assert "review_independence_waived" in events
