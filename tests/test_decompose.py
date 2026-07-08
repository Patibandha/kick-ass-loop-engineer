# tests/test_decompose.py
"""Tests for role decomposition and interface contracts."""
import unittest

import pytest

from kickass_loop_engineer.agents import Builder
from kickass_loop_engineer.decompose import DecompositionError, RoleDAG, RoleSlice, generate_slices
from kickass_loop_engineer.objective import Objective
from kickass_loop_engineer.providers.base import ProviderError

from helpers import FakeProvider  # plain import: pytest inserts tests/ on sys.path (rootdir)


class DecomposeTests(unittest.TestCase):
    def _slices(self):
        return [
            RoleSlice("backend", "build API", contract="GET /items -> [Item]", depends_on=[]),
            RoleSlice("frontend", "build UI", contract="renders items", depends_on=["backend"]),
        ]

    def test_topological_order_respects_dependencies(self):
        dag = RoleDAG(self._slices())
        order = [s.role for s in dag.ordered()]
        self.assertLess(order.index("backend"), order.index("frontend"))

    def test_cycle_is_rejected(self):
        slices = [RoleSlice("a", "x", contract="c", depends_on=["b"]),
                  RoleSlice("b", "y", contract="c", depends_on=["a"])]
        with self.assertRaises(DecompositionError):
            RoleDAG(slices).ordered()

    def test_dependency_must_define_a_contract(self):
        slices = [RoleSlice("backend", "api", contract="", depends_on=[]),
                  RoleSlice("frontend", "ui", contract="c", depends_on=["backend"])]
        with self.assertRaises(DecompositionError):
            RoleDAG(slices).validate()

    def test_unknown_dependency_is_rejected(self):
        slices = [RoleSlice("frontend", "ui", contract="c", depends_on=["ghost"])]
        with self.assertRaises(DecompositionError):
            RoleDAG(slices).validate()


OBJ = Objective(goal="build a todo CLI", done_when="pytest passes")


class _RaisingProvider(FakeProvider):
    """FakeProvider whose completion always fails, simulating a backend outage."""

    def complete(self, system, user):
        raise ProviderError("boom")


def _builder(text):
    return Builder(FakeProvider([text]))


def _assert_fallback(slices):
    assert len(slices) == 1
    assert slices[0].role == "build"
    assert slices[0].objective == OBJ.goal


def test_generate_slices_parses_valid_json_array():
    text = ('[{"role": "core", "objective": "todo storage", "contract": "add/list API"},'
            ' {"role": "cli", "objective": "argparse front", "depends_on": ["core"]}]')
    slices = generate_slices(OBJ, _builder(text))
    assert [s.role for s in slices] == ["core", "cli"]
    assert slices[1].depends_on == ["core"]


def test_generate_slices_extracts_array_from_surrounding_prose():
    text = 'Here is my plan:\n[{"role": "core", "objective": "x"}]\nGood luck!'
    assert [s.role for s in generate_slices(OBJ, _builder(text))] == ["core"]


def test_generate_slices_falls_back_to_single_slice_on_garbage():
    slices = generate_slices(OBJ, _builder("I cannot produce JSON, sorry"))
    _assert_fallback(slices)


def test_generate_slices_falls_back_on_invalid_dag():
    text = ('[{"role": "a", "objective": "x", "depends_on": ["b"]},'
            ' {"role": "b", "objective": "y", "depends_on": ["a"]}]')  # cycle
    slices = generate_slices(OBJ, _builder(text))
    assert len(slices) == 1 and slices[0].role == "build"


def test_generate_slices_caps_slice_count():
    items = ",".join(f'{{"role": "r{i}", "objective": "o"}}' for i in range(10))
    slices = generate_slices(OBJ, _builder(f"[{items}]"), max_slices=5)
    assert len(slices) == 1 and slices[0].role == "build"


def test_generate_slices_falls_back_when_provider_raises():
    slices = generate_slices(OBJ, Builder(_RaisingProvider([])))
    _assert_fallback(slices)


def test_generate_slices_falls_back_on_unparseable_bracketed_text():
    slices = generate_slices(OBJ, _builder("[not json]"))
    _assert_fallback(slices)


@pytest.mark.parametrize(
    "text",
    [
        '[{"role": "a"}]',  # missing "objective"
        '[{"role": "a", "objective": "x", "depends_on": "core"}]',  # non-list depends_on
    ],
    ids=["missing_objective", "non_list_depends_on"],
)
def test_generate_slices_falls_back_on_malformed_item(text):
    slices = generate_slices(OBJ, _builder(text))
    _assert_fallback(slices)


if __name__ == "__main__":
    unittest.main(verbosity=2)
