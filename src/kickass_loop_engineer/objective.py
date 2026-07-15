"""Objective definition: what the loop is trying to accomplish.

An objective pairs a free-form goal with explicit, checkable completion criteria.
The reviewer agent judges the workspace against these criteria each round and the
loop continues until they are met or a budget is exhausted.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Objective:
    """A target for the build/review loop.

    Attributes:
        goal: Free-form description of what to build or accomplish.
        done_when: Explicit, checkable criteria the reviewer uses to approve.
        constraints: Optional rules the builder must respect (style, scope).
        context: Optional pre-assembled repo context (map, existing files)
            rendered as a CONTEXT section in the builder brief; empty keeps
            the brief byte-identical to a context-free objective.
    """

    goal: str
    done_when: str
    constraints: str = ""
    context: str = ""

    def builder_brief(self, feedback: str = "") -> str:
        """Render the request handed to the builder for one round.

        Args:
            feedback: Reviewer feedback from the previous round, if any.

        Returns:
            A prompt describing the goal, criteria, constraints, repo
            context, and revisions.
        """
        sections = [f"GOAL:\n{self.goal}", f"DONE WHEN:\n{self.done_when}"]
        if self.constraints:
            sections.append(f"CONSTRAINTS:\n{self.constraints}")
        if self.context:
            sections.append(f"CONTEXT:\n{self.context}")
        if feedback:
            sections.append(f"REVISE per this review feedback:\n{feedback}")
        return "\n\n".join(sections)

    def reviewer_brief(self, workspace_snapshot: str) -> str:
        """Render the request handed to the reviewer for one round.

        Args:
            workspace_snapshot: Current files produced by the builder.

        Returns:
            A prompt asking the reviewer to judge the work against the criteria.
        """
        return (
            f"GOAL:\n{self.goal}\n\n"
            f"DONE WHEN:\n{self.done_when}\n\n"
            f"WORKSPACE FILES:\n{workspace_snapshot}"
        )
