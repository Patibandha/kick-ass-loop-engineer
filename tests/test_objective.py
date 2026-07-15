"""Tests for Objective brief rendering, including the additive context field."""
from kickass_loop_engineer.objective import Objective


class TestBuilderBriefWithoutContext:
    def test_minimal_brief_is_byte_identical_to_the_legacy_literal(self):
        obj = Objective(goal="build a cli", done_when="tests pass")
        assert obj.builder_brief() == ("GOAL:\nbuild a cli\n\n"
                                       "DONE WHEN:\ntests pass")

    def test_full_brief_is_byte_identical_to_the_legacy_literal(self):
        obj = Objective(goal="build a cli", done_when="tests pass",
                        constraints="stdlib only")
        expected = ("GOAL:\nbuild a cli\n\n"
                    "DONE WHEN:\ntests pass\n\n"
                    "CONSTRAINTS:\nstdlib only\n\n"
                    "REVISE per this review feedback:\nuse argparse")
        assert obj.builder_brief("use argparse") == expected

    def test_explicit_empty_context_matches_the_default(self):
        with_default = Objective(goal="g", done_when="d", constraints="c")
        with_empty = Objective(goal="g", done_when="d", constraints="c",
                               context="")
        assert with_empty.builder_brief("fb") == with_default.builder_brief("fb")


class TestBuilderBriefWithContext:
    def test_context_renders_a_context_section(self):
        obj = Objective(goal="g", done_when="d",
                        context="REPO MAP:\nfoo.py\n  def bar():")
        expected = ("GOAL:\ng\n\n"
                    "DONE WHEN:\nd\n\n"
                    "CONTEXT:\nREPO MAP:\nfoo.py\n  def bar():")
        assert obj.builder_brief() == expected

    def test_context_sits_between_constraints_and_feedback(self):
        obj = Objective(goal="g", done_when="d", constraints="c",
                        context="ctx")
        expected = ("GOAL:\ng\n\n"
                    "DONE WHEN:\nd\n\n"
                    "CONSTRAINTS:\nc\n\n"
                    "CONTEXT:\nctx\n\n"
                    "REVISE per this review feedback:\nfb")
        assert obj.builder_brief("fb") == expected

    def test_reviewer_brief_ignores_context(self):
        with_context = Objective(goal="g", done_when="d", context="ctx")
        without = Objective(goal="g", done_when="d")
        assert (with_context.reviewer_brief("snap")
                == without.reviewer_brief("snap"))
