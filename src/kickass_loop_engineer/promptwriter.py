"""Prompt Writer: renders a vetted SPEC.md and GOAL.md from an IdeaSpec.

The Prompt Writer enforces the unified six-slot readiness gate before writing
any files. Readiness requires ALL of:

* ``build`` — a non-empty description of what to build.
* ``done_when`` — a non-empty, measurable completion criterion that passes the
  :func:`~kickass_loop_engineer.interview.lint_done_when` measurability lint.
* Every slot in :data:`~kickass_loop_engineer.interview.SLOTS` (``purpose``,
  ``users``, ``constraints``, ``success_metrics``, ``anti_goals``, ``risks``)
  is either filled or explicitly listed in ``IdeaSpec.deferred`` — there is no
  way to silently skip a slot.

:meth:`PromptWriter.assess` always returns the FULL set of gaps (never just
the first miss), so a caller (or the ``--interview`` CLI flag) can present the
whole picture at once.

When the spec passes the readiness gate, :meth:`PromptWriter.write` renders two
Markdown documents into the caller-supplied workspace directory:

* ``SPEC.md`` — the full six-slot idea spec (build, purpose, users,
  constraints, success metrics, anti-goals, risks, consider, done-when).
* ``GOAL.md`` — a goal sheet derived from done_when and success_metrics (plan
  placeholder, checks derived from done_when + each success metric, evidence
  placeholder, approval boundaries).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .interview import QUESTION_TEMPLATES, SLOTS, lint_done_when, slot_filled


@dataclass
class IdeaSpec:
    """A raw idea refined through the six-slot think-tank taxonomy.

    Attributes:
        build: Description of what to build.
        done_when: Measurable completion criterion (must pass the lint).
        purpose: The job this does and the outcome it produces.
        users: Who (or what) uses it.
        constraints: Hard constraints the build must respect.
        success_metrics: Measurable metrics; each becomes a verifier check.
        anti_goals: Explicit non-goals (absorbs 1.0's ``exclude``).
        risks: Known unknowns / what could go wrong.
        consider: Additional considerations (optional, never gated).
        deferred: Slot names the user explicitly deferred; they do not gap.
    """

    build: str
    done_when: str
    purpose: str = ""
    users: str = ""
    constraints: str = ""
    success_metrics: list = field(default_factory=list)
    anti_goals: str = ""
    risks: str = ""
    consider: str = ""
    deferred: list = field(default_factory=list)


@dataclass
class Assessment:
    """Result of the readiness gate.

    Attributes:
        ready: True when the spec satisfies the full six-slot gate.
        gaps: The FULL list of human-readable gap descriptions — never just
              the first miss. Empty when ``ready`` is True.
    """

    ready: bool
    gaps: list = field(default_factory=list)


class PromptWriter:
    """Assess and render an :class:`IdeaSpec` into workspace documents.

    Methods
    -------
    assess(spec)
        Check whether *spec* is ready to be written.
    write(spec, workspace)
        Render ``SPEC.md`` and ``GOAL.md`` into *workspace*.

    Examples
    --------
    >>> spec = IdeaSpec(build="a todo CLI", done_when="pytest passes",
    ...                 purpose="track tasks", users="just me",
    ...                 success_metrics=["pytest passes"],
    ...                 deferred=["constraints", "anti_goals", "risks"])
    >>> pw = PromptWriter()
    >>> res = pw.assess(spec)
    >>> res.ready
    True
    >>> paths = pw.write(spec, "/tmp/my-project")
    >>> list(paths.keys())
    ['spec', 'goal']
    """

    def assess(self, spec: IdeaSpec) -> Assessment:
        """Evaluate whether *spec* satisfies the unified six-slot readiness gate.

        Builds the FULL gap list — never just the first miss:

        1. ``build`` empty → a gap.
        2. ``done_when`` empty → a gap; otherwise it must pass
           :func:`~kickass_loop_engineer.interview.lint_done_when` or its
           failure reason is appended as a gap.
        3. Each slot in :data:`~kickass_loop_engineer.interview.SLOTS` that is
           neither filled (per
           :func:`~kickass_loop_engineer.interview.slot_filled`) nor listed in
           ``spec.deferred`` → a gap using that slot's question hint.

        Args:
            spec: The idea specification to assess.

        Returns:
            An :class:`Assessment` with ``ready=True`` iff ``gaps`` is empty.
        """
        gaps: list = []
        if not spec.build or not spec.build.strip():
            gaps.append("build: needs a description of what to build")
        if not spec.done_when or not spec.done_when.strip():
            gaps.append("done_when: needs a measurable completion criterion")
        else:
            ok, reason = lint_done_when(spec.done_when)
            if not ok:
                gaps.append(f"done_when: {reason}")

        deferred = set(spec.deferred or [])
        for slot in SLOTS:
            if slot in deferred or slot_filled(spec, slot):
                continue
            gaps.append(f"{slot}: {QUESTION_TEMPLATES[slot]['hint']}")

        return Assessment(ready=not gaps, gaps=gaps)

    def write(self, spec: IdeaSpec, workspace: str) -> dict:
        """Render ``SPEC.md`` and ``GOAL.md`` into *workspace*.

        Args:
            spec: The idea specification to render.
            workspace: Directory to write documents into.  Must exist.

        Returns:
            A dict with keys ``"spec"`` and ``"goal"`` mapping to absolute paths.

        Raises:
            ValueError: When :meth:`assess` returns ``ready=False`` — the
                message is every gap joined with ``"; "``.
            OSError: When the files cannot be written (propagated to the caller).
        """
        assessment = self.assess(spec)
        if not assessment.ready:
            raise ValueError("; ".join(assessment.gaps))

        spec_content = self._render_spec(spec)
        goal_content = self._render_goal(spec)

        spec_path = os.path.join(workspace, "SPEC.md")
        goal_path = os.path.join(workspace, "GOAL.md")

        with open(spec_path, "w", encoding="utf-8") as handle:
            handle.write(spec_content)
        with open(goal_path, "w", encoding="utf-8") as handle:
            handle.write(goal_content)

        return {"spec": spec_path, "goal": goal_path}

    def _render_spec(self, spec: IdeaSpec) -> str:
        """Render the SPEC.md content from *spec*.

        Sections appear in a fixed order — Build, Purpose, Users, Constraints,
        Success metrics, Anti-goals, Risks, Consider, Done when (measurable).
        Each slot renders its value when filled, ``_deferred_`` when its name
        is in ``spec.deferred``, or ``_none_`` otherwise. The gate normally
        guarantees every slot but ``consider`` is filled-or-deferred, so
        ``_none_`` is a defensive fallback rather than an expected state.

        Args:
            spec: The source idea specification.

        Returns:
            Markdown string for ``SPEC.md``.
        """
        deferred = set(spec.deferred or [])

        def render_text_slot(name: str, value: str) -> str:
            if name in deferred:
                return "_deferred_"
            value = (value or "").strip()
            return value if value else "_none_"

        def render_metrics_slot() -> str:
            if "success_metrics" in deferred:
                return "_deferred_"
            if not spec.success_metrics:
                return "_none_"
            return "\n".join(f"- {metric}" for metric in spec.success_metrics)

        lines = [
            "# SPEC",
            "",
            "## Build",
            "",
            spec.build.strip(),
            "",
            "## Purpose",
            "",
            render_text_slot("purpose", spec.purpose),
            "",
            "## Users",
            "",
            render_text_slot("users", spec.users),
            "",
            "## Constraints",
            "",
            render_text_slot("constraints", spec.constraints),
            "",
            "## Success metrics",
            "",
            render_metrics_slot(),
            "",
            "## Anti-goals",
            "",
            render_text_slot("anti_goals", spec.anti_goals),
            "",
            "## Risks",
            "",
            render_text_slot("risks", spec.risks),
            "",
            "## Consider",
            "",
            spec.consider.strip() if spec.consider.strip() else "_none_",
            "",
            "## Done when (measurable)",
            "",
            spec.done_when.strip(),
            "",
        ]
        return "\n".join(lines)

    def _render_goal(self, spec: IdeaSpec) -> str:
        """Render the GOAL.md content derived from *spec*.

        The ``## Checks`` section derives from ``done_when`` plus every
        ``success_metrics`` entry — success metrics ARE the verifier targets
        the loop enforces, not documentation. Exact duplicates (``done_when``
        often repeats the first metric) are deduped while preserving order.

        Args:
            spec: The source idea specification.

        Returns:
            Markdown string for ``GOAL.md``.
        """
        seen: set = set()
        checks: list = []
        for value in [spec.done_when.strip(), *(m.strip() for m in spec.success_metrics)]:
            if value and value not in seen:
                seen.add(value)
                checks.append(value)

        lines = [
            "# GOAL",
            "",
            "## Plan",
            "",
            f"Build: {spec.build.strip()}",
            "",
            "## Checks",
            "",
            *[f"- {check}" for check in checks],
            "",
            "## Evidence",
            "",
            "_To be filled in by the verifier after each gate run._",
            "",
            "## Approval boundaries",
            "",
            "- All checks in the Checks section must pass.",
            "- Evidence must be from executable gate output, not opinion.",
            "",
        ]
        return "\n".join(lines)
