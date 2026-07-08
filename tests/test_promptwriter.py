"""Tests for the Prompt Writer."""
import os, tempfile, unittest
from kickass_loop_engineer.promptwriter import PromptWriter, IdeaSpec


def _full_spec(**overrides):
    base = dict(build="a todo CLI", done_when="python3 -m pytest -q passes",
                purpose="track tasks", users="just me", constraints="local only",
                success_metrics=["python3 -m pytest -q passes", "add+list < 50ms"],
                anti_goals="no web UI", risks="none known")
    base.update(overrides)
    return IdeaSpec(**base)


class PromptWriterTests(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()

    def test_not_ready_when_done_when_missing(self):
        res = PromptWriter().assess(IdeaSpec(build="a todo app", done_when=""))
        self.assertFalse(res.ready)
        self.assertTrue(any("done_when" in g for g in res.gaps))

    def test_ready_spec_writes_spec_and_goal(self):
        spec = IdeaSpec(build="a todo CLI", anti_goals="no web UI",
                        done_when="`pytest` passes and `todo add` works",
                        purpose="track tasks", users="just me",
                        success_metrics=["pytest passes"],
                        deferred=["constraints", "risks"])
        res = PromptWriter().assess(spec)
        self.assertTrue(res.ready, res.gaps)
        paths = PromptWriter().write(spec, self.ws)
        self.assertTrue(os.path.exists(os.path.join(self.ws, "SPEC.md")))
        self.assertTrue(os.path.exists(os.path.join(self.ws, "GOAL.md")))
        self.assertIn("todo CLI", open(os.path.join(self.ws, "SPEC.md")).read())
        self.assertIn("pytest", open(os.path.join(self.ws, "GOAL.md")).read())

    def test_write_refuses_when_not_ready(self):
        with self.assertRaises(ValueError):
            PromptWriter().write(IdeaSpec(build="x", done_when=""), self.ws)

    def test_ideaspec_v2_has_six_slots_and_deferred(self):
        spec = IdeaSpec(build="x", done_when="pytest passes", purpose="p", users="me",
                        constraints="local", success_metrics=["pytest passes"],
                        anti_goals="no web UI", risks="none known", deferred=[])
        self.assertEqual(spec.anti_goals, "no web UI")
        self.assertFalse(hasattr(spec, "exclude"))

    def test_assess_returns_full_gap_list_not_first_miss(self):
        res = PromptWriter().assess(IdeaSpec(build="", done_when="feels fast"))
        slots_in_gaps = [g.split(":")[0] for g in res.gaps]
        self.assertIn("build", slots_in_gaps)
        self.assertIn("done_when", slots_in_gaps)  # lint failure
        for slot in ("purpose", "users", "constraints", "success_metrics",
                     "anti_goals", "risks"):
            self.assertIn(slot, slots_in_gaps)
        self.assertFalse(res.ready)
        self.assertFalse(hasattr(res, "gap"))

    def test_assess_ready_when_all_slots_filled_or_deferred(self):
        spec = IdeaSpec(build="a todo CLI", done_when="python3 -m pytest -q passes",
                        purpose="track tasks", users="just me",
                        success_metrics=["python3 -m pytest -q passes"],
                        deferred=["constraints", "anti_goals", "risks"])
        res = PromptWriter().assess(spec)
        self.assertTrue(res.ready)
        self.assertEqual(res.gaps, [])

    def test_assess_lint_gate_blocks_unmeasurable_done_when(self):
        spec = IdeaSpec(build="x", done_when="feels fast", purpose="p", users="u",
                        constraints="c", success_metrics=["m1"], anti_goals="a",
                        risks="r")
        res = PromptWriter().assess(spec)
        self.assertFalse(res.ready)
        self.assertTrue(any("measurable" in g for g in res.gaps))

    def test_write_raises_with_all_gaps_joined(self):
        with self.assertRaises(ValueError) as ctx:
            PromptWriter().write(IdeaSpec(build="", done_when=""), "/tmp/nowhere")
        message = str(ctx.exception)
        self.assertIn("build", message)
        self.assertIn("purpose", message)

    def test_spec_md_renders_all_six_slots(self):
        paths = PromptWriter().write(_full_spec(), self.ws)
        text = open(paths["spec"], encoding="utf-8").read()
        for heading in ("## Purpose", "## Users", "## Constraints",
                        "## Success metrics", "## Anti-goals", "## Risks",
                        "## Consider", "## Done when (measurable)"):
            self.assertIn(heading, text)
        self.assertIn("- python3 -m pytest -q passes", text)  # metrics as list items
        self.assertIn("no web UI", text)

    def test_spec_md_marks_deferred_slots(self):
        spec = _full_spec(risks="", deferred=["risks"])
        paths = PromptWriter().write(spec, self.ws)
        text = open(paths["spec"], encoding="utf-8").read()
        self.assertIn("_deferred_", text.split("## Risks")[1].split("##")[0])

    def test_goal_md_checks_include_success_metrics(self):
        paths = PromptWriter().write(_full_spec(), self.ws)
        text = open(paths["goal"], encoding="utf-8").read()
        checks = text.split("## Checks")[1].split("##")[0]
        self.assertIn("- python3 -m pytest -q passes", checks)
        self.assertIn("- add+list < 50ms", checks)  # metrics ARE verifier targets


if __name__ == "__main__":
    unittest.main(verbosity=2)
