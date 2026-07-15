# tests/test_architect.py
"""Tests for the architect stage: Design parsing and design.md generation."""
import unittest

from kickass_loop_engineer.agents import Builder
from kickass_loop_engineer.architect import (
    ARCHITECT_SYSTEM,
    Design,
    _default_runner,
    generate_design,
    render_importlinter,
    validate_mermaid,
)
from kickass_loop_engineer.guardrails import VerificationResult
from kickass_loop_engineer.objective import Objective

from helpers import FakeProvider  # plain import: pytest inserts tests/ on sys.path (rootdir)

FULL_DESIGN = """# Design

## Components
- api: HTTP boundary that routes requests
- **core**: business logic and orchestration
- store: persistence layer

## Architecture
```mermaid
graph TD
    api --> core
    core --> store
```

## Primary flow
```mermaid
sequenceDiagram
    api->>core: handle(request)
    core->>store: save(entity)
```

## Decisions
| decision | why | alternative |
|---|---|---|
| sqlite | zero-ops | postgres |

```yaml
layering:
  layers: [api, core, store]
  forbidden:
    - {from: store, to: api}
```
"""


class DesignParseTests(unittest.TestCase):
    def test_full_sample_parses_components(self):
        design = Design.parse(FULL_DESIGN)
        self.assertEqual(
            design.components,
            [("api", "HTTP boundary that routes requests"),
             ("core", "business logic and orchestration"),
             ("store", "persistence layer")],
        )

    def test_full_sample_extracts_mermaid_fences(self):
        design = Design.parse(FULL_DESIGN)
        self.assertEqual(len(design.mermaid_fences), 2)
        self.assertTrue(design.mermaid_fences[0].startswith("graph TD"))
        self.assertTrue(design.mermaid_fences[1].startswith("sequenceDiagram"))

    def test_full_sample_parses_layering(self):
        design = Design.parse(FULL_DESIGN)
        self.assertEqual(design.layering["layers"], ["api", "core", "store"])
        self.assertEqual(design.layering["forbidden"], [{"from": "store", "to": "api"}])

    def test_full_sample_has_no_problems_and_keeps_markdown(self):
        design = Design.parse(FULL_DESIGN)
        self.assertEqual(design.problems, [])
        self.assertEqual(design.markdown, FULL_DESIGN)
        self.assertTrue(design.decisions_present)

    def test_missing_sections_each_reported_in_problems(self):
        design = Design.parse("# Design\n\nJust prose, no sections.\n")
        joined = "\n".join(design.problems)
        for section in ("Components", "Architecture", "Primary flow", "Decisions", "layering"):
            self.assertIn(section, joined)
        self.assertEqual(design.components, [])
        self.assertEqual(design.mermaid_fences, [])
        self.assertIsNone(design.layering)
        self.assertFalse(design.decisions_present)

    def test_malformed_layering_yaml_is_none_plus_problem_not_a_raise(self):
        markdown = FULL_DESIGN.replace(
            "layering:\n  layers: [api, core, store]",
            "layering:\n  layers: [api, core, store",  # unclosed flow sequence
        )
        design = Design.parse(markdown)
        self.assertIsNone(design.layering)
        self.assertTrue(any("layering" in p for p in design.problems))

    def test_yaml_block_without_layering_key_reports_problem(self):
        markdown = FULL_DESIGN.replace("layering:", "other:")
        design = Design.parse(markdown)
        self.assertIsNone(design.layering)
        self.assertTrue(any("layering" in p for p in design.problems))


def _ok_runner(fence):
    """Fake validator: every diagram is valid."""
    return ""


class ValidateMermaidTests(unittest.TestCase):
    def test_all_valid_fences_are_ok(self):
        status, messages = validate_mermaid(["graph TD\n    a --> b"], runner=_ok_runner)
        self.assertEqual((status, messages), ("ok", []))

    def test_runner_error_text_yields_invalid(self):
        status, messages = validate_mermaid(
            ["graph TD\n    a --> b"], runner=lambda f: "line 2: bad arrow"
        )
        self.assertEqual(status, "invalid")
        self.assertTrue(any("bad arrow" in m for m in messages))

    def test_runner_returning_none_yields_skipped_warning(self):
        status, messages = validate_mermaid(["graph TD\n    a --> b"], runner=lambda f: None)
        self.assertEqual(status, "skipped")
        self.assertTrue(any("SKIPPED" in m for m in messages))

    def test_runner_raising_filenotfound_yields_skipped(self):
        def runner(fence):
            raise FileNotFoundError("npx")
        status, messages = validate_mermaid(["graph TD\n    a --> b"], runner=runner)
        self.assertEqual(status, "skipped")
        self.assertTrue(any("SKIPPED" in m for m in messages))

    def test_empty_fences_are_invalid(self):
        status, messages = validate_mermaid([], runner=_ok_runner)
        self.assertEqual(status, "invalid")
        self.assertEqual(messages, ["design has no mermaid diagrams"])

    def test_skipped_precheck_flags_unknown_first_line(self):
        status, messages = validate_mermaid(
            ["not a diagram\n    a --> b"], runner=lambda f: None
        )
        self.assertEqual(status, "skipped")
        self.assertTrue(any("not a diagram" in m for m in messages))

    def test_skipped_precheck_flags_unbalanced_brackets(self):
        status, messages = validate_mermaid(
            ["graph TD\n    a[Open label --> b"], runner=lambda f: None
        )
        self.assertEqual(status, "skipped")
        self.assertTrue(any("unbalanced" in m for m in messages))

    def test_skipped_precheck_accepts_known_types_and_balance(self):
        fences = ["sequenceDiagram\n    a->>b: hi", "graph TD\n    a[Label] --> b(Other)"]
        status, messages = validate_mermaid(fences, runner=lambda f: None)
        self.assertEqual(status, "skipped")
        self.assertEqual(len(messages), 1)  # only the SKIPPED warning, no pre-check hits


class DefaultRunnerTests(unittest.TestCase):
    """The maid-backed runner mapped through a FAKED VerificationPolicy (no Node)."""

    class _Policy:
        def __init__(self, result):
            self.result = result
            self.commands = []

        def run(self, command, cwd):
            self.commands.append(command)
            return self.result

    def test_tool_not_found_maps_to_none(self):
        policy = self._Policy(VerificationResult(
            passed=False, command="npx", error="verification tool not found"))
        self.assertIsNone(_default_runner("graph TD\n    a --> b", policy=policy))

    def test_pass_maps_to_empty_string_and_uses_allowlisted_prefix(self):
        policy = self._Policy(VerificationResult(passed=True, command="npx", returncode=0))
        self.assertEqual(_default_runner("graph TD\n    a --> b", policy=policy), "")
        self.assertTrue(policy.commands[0].startswith("npx --yes @probelabs/maid "))
        self.assertTrue(policy.commands[0].endswith(".mmd"))

    def test_failure_maps_to_error_text(self):
        policy = self._Policy(VerificationResult(
            passed=False, command="npx", returncode=1,
            stdout_tail="line 3: SE-BAD-ARROW expected -->", stderr_tail=""))
        outcome = _default_runner("graph TD\n    a --> b", policy=policy)
        self.assertIn("SE-BAD-ARROW", outcome)


class GenerateDesignTests(unittest.TestCase):
    def _objective(self):
        return Objective(goal="build a todo API", done_when="CRUD endpoints pass tests")

    def test_single_provider_call_with_architect_system(self):
        provider = FakeProvider([FULL_DESIGN])
        builder = Builder(provider)
        design, result = generate_design(builder, self._objective(), validate=_ok_runner)
        self.assertEqual(len(provider.calls), 1)
        system, user = provider.calls[0]
        self.assertEqual(system, ARCHITECT_SYSTEM)
        self.assertIn("build a todo API", user)
        self.assertIn("CRUD endpoints pass tests", user)
        self.assertIsInstance(design, Design)
        self.assertEqual(design.markdown, FULL_DESIGN)
        self.assertEqual(result.text, FULL_DESIGN)

    def test_system_prompt_names_the_required_sections(self):
        for required in ("## Components", "## Architecture", "## Primary flow",
                         "## Decisions", "layering", "layers", "forbidden"):
            self.assertIn(required, ARCHITECT_SYSTEM)


class GenerateDesignRetryTests(unittest.TestCase):
    def _objective(self):
        return Objective(goal="build a todo API", done_when="CRUD endpoints pass tests")

    def test_invalid_diagrams_drive_bounded_retries_then_degrade(self):
        provider = FakeProvider([FULL_DESIGN, FULL_DESIGN, FULL_DESIGN, FULL_DESIGN])
        builder = Builder(provider)
        design, _ = generate_design(
            builder, self._objective(),
            max_diagram_retries=2, validate=lambda f: "line 2: bad arrow",
        )
        self.assertEqual(len(provider.calls), 3)  # initial + exactly 2 retries
        self.assertTrue(any("invalid" in p for p in design.problems))

    def test_retry_prompt_carries_validator_errors(self):
        provider = FakeProvider([FULL_DESIGN, FULL_DESIGN, FULL_DESIGN])
        builder = Builder(provider)
        generate_design(
            builder, self._objective(),
            max_diagram_retries=2, validate=lambda f: "line 2: bad arrow",
        )
        self.assertIn("bad arrow", provider.calls[1][1])
        self.assertIn("build a todo API", provider.calls[1][1])

    def test_retry_stops_as_soon_as_diagrams_validate(self):
        # Two fences per validation round: round 1 flags the first fence,
        # round 2 finds both valid → the loop must stop at two provider calls.
        outcomes = iter(["line 2: bad arrow", "", "", ""])
        provider = FakeProvider([FULL_DESIGN, FULL_DESIGN, FULL_DESIGN])
        builder = Builder(provider)
        design, _ = generate_design(
            builder, self._objective(),
            max_diagram_retries=2, validate=lambda f: next(outcomes),
        )
        self.assertEqual(len(provider.calls), 2)
        self.assertFalse(any("invalid" in p for p in design.problems))

    def test_skipped_validation_means_one_call_plus_problem(self):
        provider = FakeProvider([FULL_DESIGN, FULL_DESIGN])
        builder = Builder(provider)
        design, _ = generate_design(
            builder, self._objective(), max_diagram_retries=2, validate=lambda f: None,
        )
        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(any("SKIPPED" in p for p in design.problems))

    def test_skipped_with_failing_precheck_does_not_retry(self):
        bad = FULL_DESIGN.replace("graph TD", "not a diagram")
        provider = FakeProvider([bad, bad])
        builder = Builder(provider)
        design, _ = generate_design(
            builder, self._objective(), max_diagram_retries=2, validate=lambda f: None,
        )
        self.assertEqual(len(provider.calls), 1)  # pre-check failures never drive retries
        self.assertTrue(any("SKIPPED" in p for p in design.problems))
        self.assertTrue(any("not a diagram" in p for p in design.problems))

    def test_retry_spend_is_summed_into_the_returned_result(self):
        provider = FakeProvider(
            [FULL_DESIGN, FULL_DESIGN, FULL_DESIGN],
            cost_usd=0.5, tokens=10, prompt_tokens=7, completion_tokens=3,
        )
        builder = Builder(provider)
        _, result = generate_design(
            builder, self._objective(),
            max_diagram_retries=2, validate=lambda f: "line 2: bad arrow",
        )
        self.assertEqual(len(provider.calls), 3)
        self.assertEqual(result.tokens, 30)  # SUM across all three paid calls
        self.assertAlmostEqual(result.cost_usd, 1.5)
        self.assertEqual(result.prompt_tokens, 21)
        self.assertEqual(result.completion_tokens, 9)
        self.assertEqual(result.text, FULL_DESIGN)  # text/model stay the LAST call's

    def test_single_call_result_is_unchanged_by_accumulation(self):
        provider = FakeProvider(
            [FULL_DESIGN], cost_usd=0.5, tokens=10, prompt_tokens=7, completion_tokens=3,
        )
        builder = Builder(provider)
        _, result = generate_design(builder, self._objective(), validate=_ok_runner)
        self.assertEqual(result.tokens, 10)
        self.assertAlmostEqual(result.cost_usd, 0.5)
        self.assertEqual(result.prompt_tokens, 7)
        self.assertEqual(result.completion_tokens, 3)

    def test_zero_retries_degrades_after_the_first_call(self):
        provider = FakeProvider([FULL_DESIGN, FULL_DESIGN])
        builder = Builder(provider)
        design, _ = generate_design(
            builder, self._objective(), max_diagram_retries=0, validate=lambda f: "bad",
        )
        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(any("invalid" in p for p in design.problems))


class TestRenderImportlinter(unittest.TestCase):
    LAYERING = {
        "layers": ["api", "core", "store"],
        "forbidden": [{"from": "store", "to": "api"}],
    }

    def test_full_layering_renders_exact_ini(self):
        expected = (
            "[importlinter]\n"
            "root_package = mypkg\n"
            "\n"
            "[importlinter:contract:layers]\n"
            "name = declared layering\n"
            "type = layers\n"
            "layers =\n"
            "    api\n"
            "    core\n"
            "    store\n"
            "containers =\n"
            "    mypkg\n"
            "\n"
            "[importlinter:contract:forbidden-1]\n"
            "name = forbidden: store -> api\n"
            "type = forbidden\n"
            "source_modules =\n"
            "    mypkg.store\n"
            "forbidden_modules =\n"
            "    mypkg.api\n"
        )
        self.assertEqual(render_importlinter(self.LAYERING, "mypkg"), expected)

    def test_multiple_forbidden_entries_render_one_contract_each(self):
        layering = {
            "layers": ["a", "b"],
            "forbidden": [{"from": "b", "to": "a"}, {"from": "a", "to": "b"}],
        }
        ini = render_importlinter(layering, "pkg")
        self.assertIn("[importlinter:contract:forbidden-1]", ini)
        self.assertIn("[importlinter:contract:forbidden-2]", ini)
        self.assertIn("name = forbidden: b -> a", ini)
        self.assertIn("name = forbidden: a -> b", ini)

    def test_already_qualified_modules_are_not_double_prefixed(self):
        layering = {
            "layers": ["mypkg.api", "mypkg.core"],
            "forbidden": [{"from": "mypkg.core", "to": "mypkg.api"}],
        }
        ini = render_importlinter(layering, "mypkg")
        self.assertNotIn("mypkg.mypkg", ini)
        self.assertIn("source_modules =\n    mypkg.core\n", ini)
        self.assertIn("layers =\n    api\n    core\n", ini)

    def test_none_layering_returns_none(self):
        self.assertIsNone(render_importlinter(None, "mypkg"))

    def test_empty_layering_returns_none(self):
        self.assertIsNone(render_importlinter({}, "mypkg"))
        self.assertIsNone(render_importlinter({"layers": [], "forbidden": []}, "mypkg"))

    def test_layers_without_forbidden_renders_layers_contract_only(self):
        ini = render_importlinter({"layers": ["api", "core"]}, "mypkg")
        self.assertIn("[importlinter:contract:layers]", ini)
        self.assertNotIn("forbidden", ini)


if __name__ == "__main__":
    unittest.main()
