"""Role decomposition and interface contract validation.

This module provides tools to split a multi-agent task into well-defined
*role slices*, declare the interface contract each slice exposes to its
dependents, and compute a topologically-ordered execution plan that
respects inter-role dependencies.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("kickass_loop_engineer.decompose")


class DecompositionError(Exception):
    """Raised when role slices form an invalid decomposition.

    Conditions that trigger this error:

    * An unknown dependency name is referenced (no matching role).
    * A role that others depend on has an empty ``contract`` field.
    * The dependency graph contains a cycle.
    """


@dataclass
class RoleSlice:
    """A single agent role with its objective and interface contract.

    Parameters
    ----------
    role:
        Unique identifier for this role (e.g. ``"backend"``).
    objective:
        Human-readable description of what this role must accomplish.
    contract:
        Machine- or human-readable interface description that downstream
        dependents build against (e.g. ``"GET /items -> [Item]"``).
        Roles that have dependents **must** supply a non-empty contract.
    depends_on:
        List of ``role`` names that must complete before this role starts.
    """

    role: str
    objective: str
    contract: str = ""
    depends_on: list = field(default_factory=list)


class RoleDAG:
    """A directed acyclic graph of :class:`RoleSlice` instances.

    Parameters
    ----------
    slices:
        An ordered collection of :class:`RoleSlice` objects.  Input order
        is used as a tiebreaker during topological sort.

    Example
    -------
    >>> slices = [
    ...     RoleSlice("backend", "build API", contract="GET /items -> [Item]"),
    ...     RoleSlice("frontend", "build UI", contract="renders items", depends_on=["backend"]),
    ... ]
    >>> dag = RoleDAG(slices)
    >>> dag.validate()
    >>> [s.role for s in dag.ordered()]
    ['backend', 'frontend']
    """

    def __init__(self, slices: List[RoleSlice]) -> None:
        self._slices: List[RoleSlice] = list(slices)
        self._map: Dict[str, RoleSlice] = {s.role: s for s in self._slices}

    def validate(self) -> None:
        """Validate the DAG for structural correctness.

        Checks performed (in order):

        1. Every name listed in a slice's ``depends_on`` must correspond to a
           role that exists in this DAG.
        2. Every role that is listed as a dependency by *any* other role must
           expose a non-empty ``contract``.

        Returns
        -------
        None
            On success.

        Raises
        ------
        DecompositionError
            On the first validation failure detected.
        """
        # Collect roles that are declared as dependencies.
        dependents: Dict[str, List[str]] = {}  # dependency_role -> [roles that need it]
        for s in self._slices:
            for dep in s.depends_on:
                dependents.setdefault(dep, []).append(s.role)

        # Rule 1: unknown dependencies.
        for dep in dependents:
            if dep not in self._map:
                requesters = ", ".join(dependents[dep])
                raise DecompositionError(
                    f"Unknown dependency '{dep}' referenced by: {requesters}"
                )

        # Rule 2: dependencies must define a contract.
        for dep in dependents:
            dep_slice = self._map[dep]
            if not dep_slice.contract:
                requesters = ", ".join(dependents[dep])
                raise DecompositionError(
                    f"Role '{dep}' is a dependency of [{requesters}] "
                    f"but has no contract defined."
                )

    def ordered(self) -> List[RoleSlice]:
        """Return role slices in topological (dependency-first) order.

        Uses Kahn's algorithm.  Among nodes that are simultaneously ready
        (all dependencies satisfied), input order is preserved (stable sort).

        Returns
        -------
        list[RoleSlice]
            Slices ordered so that every role appears after all of its
            dependencies.

        Raises
        ------
        DecompositionError
            If :meth:`validate` fails, or if the graph contains a cycle.
        """
        self.validate()

        # Build in-degree map and adjacency list.
        in_degree: Dict[str, int] = {s.role: 0 for s in self._slices}
        # adjacency: role -> list of roles that depend on it
        adjacency: Dict[str, List[str]] = {s.role: [] for s in self._slices}

        for s in self._slices:
            for dep in s.depends_on:
                in_degree[s.role] += 1
                adjacency[dep].append(s.role)

        # Preserve input order for stable output: use a list as a queue,
        # collecting zero-in-degree nodes in input order.
        queue: deque[str] = deque(
            s.role for s in self._slices if in_degree[s.role] == 0
        )
        result: List[RoleSlice] = []

        while queue:
            role = queue.popleft()
            result.append(self._map[role])
            # Process neighbors in input order for stability.
            for neighbor in sorted(
                adjacency[role],
                key=lambda r: next(i for i, s in enumerate(self._slices) if s.role == r),
            ):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(result) != len(self._slices):
            cycle_members = [s.role for s in self._slices if self._map[s.role] not in result]
            raise DecompositionError(
                f"Cycle detected among roles: {cycle_members}"
            )

        return result


SLICE_SYSTEM = (
    "You are a software architect. Split the objective into 1-5 role slices. "
    "Output ONLY a JSON array, no prose. Each element: "
    '{"role": "<unique-name>", "objective": "<what this role builds>", '
    '"contract": "<interface dependents build against>", "depends_on": ["<role>", ...]}. '
    "A role that others depend on MUST have a non-empty contract. "
    "If the objective does not warrant splitting, output a single-element array."
)


def _fallback(objective) -> list:
    return [RoleSlice(role="build", objective=objective.goal)]


def generate_slices(objective, builder, max_slices: int = 5) -> list:
    """Ask the builder model to decompose *objective* into validated role slices.

    The builder's reply must contain a JSON array of slice objects. On any
    failure — no JSON, wrong shape, more than *max_slices* entries, or a DAG
    that fails validation/toposort — the single-slice fallback is returned so
    the pipeline always has work to run.

    Parameters
    ----------
    objective:
        The run objective (``objective.goal`` seeds the prompt).
    builder:
        An ``agents.Builder``; its provider does the completion.
    max_slices:
        Hard cap on accepted slice count.

    Returns
    -------
    list[RoleSlice]
        Topologically ordered slices (never empty).
    """
    prompt = (f"OBJECTIVE:\n{objective.goal}\n\nDONE WHEN:\n{objective.done_when}\n\n"
              "Decompose into role slices as instructed.")
    try:
        text = builder.provider.complete(SLICE_SYSTEM, prompt).text or ""
    except Exception as exc:  # ProviderError or transport failure: fallback, never crash
        logger.warning("slice generation failed (%s); using single-slice fallback", exc)
        return _fallback(objective)

    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return _fallback(objective)
    try:
        raw = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return _fallback(objective)
    if not isinstance(raw, list) or not raw or len(raw) > max_slices:
        return _fallback(objective)

    slices: list[RoleSlice] = []
    for item in raw:
        if not isinstance(item, dict):
            return _fallback(objective)
        role, obj = item.get("role"), item.get("objective")
        if not (isinstance(role, str) and role.strip() and isinstance(obj, str) and obj.strip()):
            return _fallback(objective)
        deps = item.get("depends_on", [])
        if not (isinstance(deps, list) and all(isinstance(d, str) for d in deps)):
            return _fallback(objective)
        slices.append(RoleSlice(role=role.strip(), objective=obj.strip(),
                                contract=str(item.get("contract", "") or ""), depends_on=deps))
    if len({s.role for s in slices}) != len(slices):
        return _fallback(objective)
    try:
        return RoleDAG(slices).ordered()
    except DecompositionError as exc:
        logger.warning("invalid decomposition (%s); using single-slice fallback", exc)
        return _fallback(objective)
