# tests/test_router.py
"""Tests for the deterministic skill-router."""
import unittest
from kickass_loop_engineer.router import STAGES, SkillRouter, StageRoute


class RouterTests(unittest.TestCase):
    def test_every_stage_has_one_owner(self):
        r = SkillRouter()
        for stage in STAGES:
            self.assertTrue(r.owner(stage))

    def test_resolve_uses_owner_when_available(self):
        r = SkillRouter()
        self.assertEqual(r.resolve("review", available={"gsd-code-reviewer"}),
                         StageRoute("gsd-code-reviewer", "agent"))

    def test_resolve_falls_back_when_owner_missing(self):
        r = SkillRouter()
        self.assertEqual(r.resolve("review", available={"gstack"}),
                         StageRoute("gstack:/review", "skill"))

    def test_unknown_stage_raises(self):
        with self.assertRaises(KeyError):
            SkillRouter().owner("nonsense")

    def test_only_loop_engineer_auto_invokes(self):
        self.assertTrue(SkillRouter().is_sole_auto_invoked("loop-engineer"))
        self.assertFalse(SkillRouter().is_sole_auto_invoked("gsd-planner"))

    def test_resolve_returns_stage_route_with_kind(self):
        route = SkillRouter().resolve("plan", available={"gsd-planner"})
        assert route == StageRoute(name="gsd-planner", kind="agent")

    def test_verify_fallback_has_superpowers_prefix(self):
        route = SkillRouter().resolve("verify", available={"superpowers"})
        assert route.name == "superpowers:verification-before-completion"
        assert route.kind == "skill"

    def test_review_owner_is_agent_kind(self):
        assert SkillRouter().owner("review") == StageRoute("gsd-code-reviewer", "agent")

    def test_unavailable_owner_falls_back_with_its_own_kind(self):
        route = SkillRouter().resolve("build", available={"backend-developer"})
        assert route == StageRoute("backend-developer", "agent")

    def test_every_stage_has_kinded_owner(self):
        router = SkillRouter()
        for stage in STAGES:
            route = router.owner(stage)
            assert route.kind in ("skill", "agent"), stage

    def test_research_stage_between_refine_and_plan(self):
        assert STAGES.index("research") == STAGES.index("refine") + 1
        assert STAGES.index("research") < STAGES.index("plan")
        route = SkillRouter().owner("research")
        assert route == StageRoute("deep-research", "skill")
        fb = SkillRouter().resolve("research", available={"research-analyst"})
        assert fb == StageRoute("research-analyst", "agent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
