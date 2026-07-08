"""Tests for interview.py — fixed question templates and the measurability lint."""
from kickass_loop_engineer.interview import (
    QUESTION_TEMPLATES, SLOTS, lint_done_when, next_questions,
)


def test_slots_are_the_six_taxonomy_slots_in_fixed_order():
    assert SLOTS == ("purpose", "users", "constraints",
                     "success_metrics", "anti_goals", "risks")


def test_every_slot_has_a_fixed_question_template():
    assert set(QUESTION_TEMPLATES) == set(SLOTS)
    for slot, entry in QUESTION_TEMPLATES.items():
        assert entry["question"].strip(), slot
        assert entry["hint"].strip(), slot


def test_lint_accepts_allowlisted_command_prefix():
    ok, _ = lint_done_when("python3 -m pytest -q passes with 0 failures")
    assert ok
    ok, _ = lint_done_when("npm test exits 0")
    assert ok


def test_lint_accepts_numeric_threshold():
    for good in ("p95 < 200ms", "handles >= 10000 rows", "error rate 1% or less",
                 "responds in 250 ms", "throughput > 500 req"):
        ok, reason = lint_done_when(good)
        assert ok, (good, reason)


def test_lint_rejects_vibes():
    for bad in ("feels fast", "works well", "users are happy", "", "   "):
        ok, reason = lint_done_when(bad)
        assert not ok
        assert "measurable" in reason


def test_lint_rejects_bare_numbers_without_comparator_or_unit():
    ok, _ = lint_done_when("version 2 of the app works")
    assert not ok


def test_lint_prefix_match_has_word_boundaries():
    # "ruff" must not match inside "scruffy" — a bare substring match would
    # false-positive here.
    ok, reason = lint_done_when("make the UI less scruffy")
    assert not ok
    assert "measurable" in reason


def _spec_stub(**kwargs):
    """Duck-typed stand-in — next_questions never imports IdeaSpec, and using a
    stub here lets Task 1 commit fully green before IdeaSpec v2 lands in Task 2."""
    from types import SimpleNamespace
    base = dict(build="", done_when="", purpose="", users="", constraints="",
                success_metrics=[], anti_goals="", risks="", deferred=[])
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_next_questions_lists_all_unfilled_slots_in_order():
    spec = _spec_stub(build="a todo CLI", done_when="pytest passes",
                      purpose="track tasks")
    qs = next_questions(spec)
    assert [q["slot"] for q in qs] == ["users", "constraints", "success_metrics",
                                       "anti_goals", "risks"]
    assert all(q["question"] == QUESTION_TEMPLATES[q["slot"]]["question"] for q in qs)


def test_next_questions_prepends_lint_question_when_done_when_vague():
    spec = _spec_stub(build="x", done_when="feels fast")
    qs = next_questions(spec)
    assert qs[0]["slot"] == "done_when"
    assert "feels fast" in qs[0]["question"]  # interpolated current value


def test_next_questions_skips_deferred_and_filled_slots():
    spec = _spec_stub(build="x", done_when="pytest passes",
                      purpose="p", users="me", constraints="local only",
                      success_metrics=["pytest passes"],
                      deferred=["anti_goals", "risks"])
    assert next_questions(spec) == []
