"""Deterministic stage-to-skill router for kickAssLoopEngineer.

Each stage in the build loop has exactly one *owner* route and an optional
*fallback* route.  The router never does keyword matching or similarity
selection — routing is purely table-driven and therefore predictable.  Every
route is a :class:`StageRoute`, naming both the exact target and whether the
session should reach it with the Skill tool or the Agent tool.

Auto-invocation contract
------------------------
Only ``/loop-engineer`` auto-invokes; it is the sole entry point that the
harness triggers automatically when the user runs the loop.  Every owned
sub-skill is called by its exact name at a fixed stage.  Owned sub-skills
**must** set ``disable-model-invocation: true`` in their frontmatter so
that the harness forwards control without spawning an extra model turn.

Availability semantics
----------------------
A route is considered *available* if the leading segment of its ``name``
before the first ``:`` appears in the ``available`` set.  Examples:

* ``StageRoute("gsd-code-reviewer", "agent")`` — available iff
  ``"gsd-code-reviewer" in available``
* ``StageRoute("gstack:/review", "skill")`` — available iff
  ``"gstack" in available``
"""

from __future__ import annotations

from typing import NamedTuple, Optional

STAGES: tuple[str, ...] = (
    "refine",
    "research",
    "plan",
    "build",
    "verify",
    "review",
    "qa",
    "security",
    "ship",
)


class StageRoute(NamedTuple):
    """A routed target: the exact name to invoke and how (Skill vs Agent tool)."""

    name: str
    kind: str  # "skill" | "agent"


_OWNERS: dict[str, dict[str, Optional[StageRoute]]] = {
    "refine": {
        "owner": StageRoute("superpowers:brainstorming", "skill"),
        "fallback": StageRoute("gsd-discuss-phase", "skill"),
    },
    "research": {
        "owner": StageRoute("deep-research", "skill"),
        "fallback": StageRoute("research-analyst", "agent"),
    },
    "plan": {
        "owner": StageRoute("gsd-planner", "agent"),
        "fallback": StageRoute("superpowers:writing-plans", "skill"),
    },
    "build": {
        "owner": StageRoute("superpowers:test-driven-development", "skill"),
        "fallback": StageRoute("backend-developer", "agent"),
    },
    "verify": {
        "owner": StageRoute("gsd-verify-work", "skill"),
        "fallback": StageRoute("superpowers:verification-before-completion", "skill"),
    },
    "review": {
        "owner": StageRoute("gsd-code-reviewer", "agent"),
        "fallback": StageRoute("gstack:/review", "skill"),
    },
    "qa": {
        "owner": StageRoute("gstack:/qa", "skill"),
        "fallback": None,
    },
    "security": {
        "owner": StageRoute("gstack:/cso", "skill"),
        "fallback": StageRoute("security-auditor", "agent"),
    },
    "ship": {
        "owner": StageRoute("gstack:/ship", "skill"),
        "fallback": None,
    },
}


def _is_available(route: StageRoute, available: set[str]) -> bool:
    """Return True if the leading segment of *route*'s name is in *available*."""
    return route.name.split(":")[0] in available


class SkillRouter:
    """Deterministic router from build-loop stage to owner route.

    Only ``/loop-engineer`` auto-invokes.  Every other skill or agent is
    called by exact name at a fixed stage; owned sub-skills must set
    ``disable-model-invocation: true`` in their frontmatter.
    """

    def owner(self, stage: str) -> StageRoute:
        """Return the owner :class:`StageRoute` for *stage*.

        Raises
        ------
        KeyError
            If *stage* is not a recognised stage name.
        """
        return _OWNERS[stage]["owner"]

    def resolve(self, stage: str, *, available: set[str]) -> StageRoute:
        """Return the :class:`StageRoute` to invoke for *stage*.

        Resolution order (deterministic, no fuzzy matching):

        1. If the owner route is available → return owner.
        2. Else if a fallback is defined (not ``None``) → return fallback.
        3. Else → return owner regardless (caller handles missing skill).

        Parameters
        ----------
        stage:
            A stage name from :data:`STAGES`.
        available:
            Set of available skill/agent names or name prefixes.  A route
            is considered available when
            ``route.name.split(":")[0] in available``.

        Returns
        -------
        StageRoute
            The exact route (name + kind) to invoke.
        """
        entry = _OWNERS[stage]
        owner: StageRoute = entry["owner"]
        fallback: Optional[StageRoute] = entry["fallback"]

        if _is_available(owner, available):
            return owner
        if fallback is not None:
            return fallback
        return owner

    def is_sole_auto_invoked(self, skill_name: str) -> bool:
        """Return True only if *skill_name* is ``"loop-engineer"``.

        The ``/loop-engineer`` skill is the sole entry point that the
        harness auto-invokes.  All other sub-skills are called explicitly
        by name at their assigned stage.
        """
        return skill_name == "loop-engineer"
