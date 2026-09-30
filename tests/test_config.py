"""Tests for config builders."""
import os
import tempfile
import unittest

from kickass_loop_engineer.config import (
    architect_mode,
    build_ledger,
    build_notifier,
    build_orchestrator,
    task_mode,
)
from kickass_loop_engineer.ledger import CostLedger
from kickass_loop_engineer.notify import Notifier
from kickass_loop_engineer.objective import Objective
from kickass_loop_engineer.review import CrossModelReviewError


class ConfigBuilderTests(unittest.TestCase):
    def test_build_ledger_from_section(self):
        led = build_ledger({"ledger": {"run_cap_usd": 5.0, "month_cap_usd": 100.0,
                                       "path": "/tmp/x.json"}}, month="2026-06")
        self.assertIsInstance(led, CostLedger)
        self.assertEqual(led.run_cap_usd, 5.0)
        self.assertEqual(led.month, "2026-06")

    def test_build_ledger_defaults(self):
        led = build_ledger({}, month="2026-06")
        self.assertEqual(led.run_cap_usd, 10.0)

    def test_build_notifier_from_section(self):
        n = build_notifier({"notify": {"command": "cat"}})
        self.assertIsInstance(n, Notifier)
        self.assertEqual(n.command, "cat")

    def test_build_notifier_empty_is_noop(self):
        n = build_notifier({})
        self.assertIsNone(n.webhook)
        self.assertIsNone(n.command)


class EnsembleConfigTests(unittest.TestCase):
    def test_ensemble_n_default_and_override(self):
        from kickass_loop_engineer.config import ensemble_n
        self.assertEqual(ensemble_n({}), 2)
        self.assertEqual(ensemble_n({"ensemble": {"n": 3}}), 3)


class ParseAttemptsTests(unittest.TestCase):
    """``ensemble.attempts`` parsing → per-attempt ``AttemptSpec`` list."""

    def test_absent_attempts_yields_default_ladder_for_ensemble_n(self):
        from kickass_loop_engineer.config import parse_attempts
        from kickass_loop_engineer.ensemble import default_specs
        self.assertEqual(parse_attempts({}), default_specs(2))
        self.assertEqual(parse_attempts({"ensemble": {"n": 3}}), default_specs(3))

    def test_explicit_attempts_yield_one_spec_per_entry(self):
        from kickass_loop_engineer.config import parse_attempts
        specs = parse_attempts({"ensemble": {"attempts": [
            {"model": "a", "temperature": 0.9},
            {"model": "b"},
        ]}})
        self.assertEqual(len(specs), 2)
        self.assertEqual(specs[0].model, "a")
        self.assertEqual(specs[0].temperature, 0.9)
        self.assertEqual(specs[1].model, "b")
        self.assertIsNone(specs[1].temperature)

    def test_ensemble_n_is_ignored_with_a_warning_when_attempts_present(self):
        from kickass_loop_engineer.config import parse_attempts
        cfg = {"ensemble": {"n": 5, "attempts": [{"model": "a"}]}}
        with self.assertLogs("kickass_loop_engineer.config", level="WARNING") as captured:
            specs = parse_attempts(cfg)
        self.assertEqual(len(specs), 1)
        self.assertTrue(any("ensemble.n" in line for line in captured.output),
                        "warning should name the ignored ensemble.n key")

    def test_non_mapping_entry_raises_naming_the_field(self):
        from kickass_loop_engineer.config import parse_attempts
        with self.assertRaises(RuntimeError) as ctx:
            parse_attempts({"ensemble": {"attempts": ["just-a-string"]}})
        self.assertIn("ensemble.attempts[0]", str(ctx.exception))

    def test_unknown_key_raises_naming_the_key(self):
        from kickass_loop_engineer.config import parse_attempts
        with self.assertRaises(RuntimeError) as ctx:
            parse_attempts({"ensemble": {"attempts": [{"model": "a", "temp": 0.9}]}})
        message = str(ctx.exception)
        self.assertIn("ensemble.attempts[0]", message)
        self.assertIn("temp", message)

    def test_non_numeric_temperature_raises_naming_the_field(self):
        from kickass_loop_engineer.config import parse_attempts
        with self.assertRaises(RuntimeError) as ctx:
            parse_attempts({"ensemble": {"attempts": [
                {"model": "a", "temperature": "hot"}]}})
        message = str(ctx.exception)
        self.assertIn("ensemble.attempts[0].temperature", message)

    def test_boolean_temperature_raises_naming_the_field(self):
        from kickass_loop_engineer.config import parse_attempts
        with self.assertRaises(RuntimeError) as ctx:
            parse_attempts({"ensemble": {"attempts": [
                {"model": "a", "temperature": True}]}})
        self.assertIn("ensemble.attempts[0].temperature", str(ctx.exception))

    def test_scalar_attempts_section_raises(self):
        from kickass_loop_engineer.config import parse_attempts
        with self.assertRaises(RuntimeError) as ctx:
            parse_attempts({"ensemble": {"attempts": "two please"}})
        self.assertIn("ensemble.attempts", str(ctx.exception))

    def test_empty_attempts_list_raises(self):
        from kickass_loop_engineer.config import parse_attempts
        with self.assertRaises(RuntimeError) as ctx:
            parse_attempts({"ensemble": {"attempts": []}})
        self.assertIn("ensemble.attempts", str(ctx.exception))

    def test_cross_provider_attempt_missing_model_raises_naming_the_index(self):
        # A spec that switches to a model-required provider without naming a
        # model would otherwise die at construction with a raw TypeError; fail
        # at parse time with a RuntimeError naming the attempt instead.
        from kickass_loop_engineer.config import parse_attempts
        with self.assertRaises(RuntimeError) as ctx:
            parse_attempts({"ensemble": {"attempts": [
                {"provider": "openai_compat", "family": "gpt"}]}})
        message = str(ctx.exception)
        self.assertIn("ensemble.attempts[0]", message)
        self.assertIn("model", message)

    def test_temperature_free_provider_without_model_is_allowed(self):
        # claude_code/anthropic have a model DEFAULT — switching to one without
        # naming a model must NOT trip the cross-provider model requirement.
        from kickass_loop_engineer.config import parse_attempts
        specs = parse_attempts({"ensemble": {"attempts": [
            {"provider": "claude_code"}]}})
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].provider, "claude_code")


class DiverseEnsembleOrchestratorTests(unittest.TestCase):
    """build_orchestrator wires attempt specs + a spec -> Builder factory."""

    _BASE = {"builder": {"provider": "ollama", "model": "kimi-k2.7-code:cloud"},
             "reviewer": {"provider": "ollama", "model": "qwen2.5"}}

    def _orch(self, extra=None):
        cfg = {k: dict(v) for k, v in self._BASE.items()}
        cfg.update(extra or {})
        return build_orchestrator(cfg, workspace=tempfile.mkdtemp(), month="2026-07",
                                  objective=Objective(goal="g", done_when="d"))

    def test_back_compat_no_attempts_yields_todays_n_with_the_ladder(self):
        from kickass_loop_engineer.ensemble import default_specs
        orch = self._orch()
        self.assertEqual(orch.ensemble_n, 2)
        self.assertEqual(orch.attempt_specs, default_specs(2))
        orch3 = self._orch({"ensemble": {"n": 3}})
        self.assertEqual(orch3.ensemble_n, 3)
        self.assertEqual(orch3.attempt_specs, default_specs(3))

    def test_default_specs_build_builders_with_different_temperatures(self):
        orch = self._orch()
        temps = [orch.builder_factory(spec).provider.temperature
                 for spec in orch.attempt_specs]
        self.assertEqual(temps, [0.2, 0.7])

    def test_attempt_builders_keep_the_base_builder_model_and_host(self):
        orch = self._orch()
        builder = orch.builder_factory(orch.attempt_specs[0])
        self.assertEqual(builder.provider.model, "kimi-k2.7-code:cloud")

    def test_explicit_attempts_derive_n_and_use_each_specs_model_provider(self):
        from kickass_loop_engineer.providers.ollama import OllamaProvider
        from kickass_loop_engineer.providers.openai_compat import OpenAICompatProvider
        orch = self._orch({"ensemble": {"attempts": [
            {"model": "llama3.3", "temperature": 0.9},
            {"provider": "openai_compat", "model": "gpt-5.5", "family": "gpt"},
        ]}})
        self.assertEqual(orch.ensemble_n, 2)
        first = orch.builder_factory(orch.attempt_specs[0])
        second = orch.builder_factory(orch.attempt_specs[1])
        self.assertIsInstance(first.provider, OllamaProvider)
        self.assertEqual(first.provider.model, "llama3.3")
        self.assertEqual(first.provider.temperature, 0.9)
        self.assertIsInstance(second.provider, OpenAICompatProvider)
        self.assertEqual(second.provider.model, "gpt-5.5")

    def test_temperature_is_stripped_with_one_warning_for_anthropic_attempts(self):
        from kickass_loop_engineer.providers.anthropic_api import AnthropicProvider
        cfg_extra = {"ensemble": {"attempts": [
            {"provider": "anthropic", "model": "claude-sonnet-4-6",
             "temperature": 0.9}]}}
        with self.assertLogs("kickass_loop_engineer.config", level="WARNING") as captured:
            orch = self._orch(cfg_extra)
        strip_warnings = [line for line in captured.output if "temperature" in line]
        self.assertEqual(len(strip_warnings), 1, "warn exactly once per model")
        builder = orch.builder_factory(orch.attempt_specs[0])  # attempt still runs
        self.assertIsInstance(builder.provider, AnthropicProvider)
        self.assertFalse(hasattr(builder.provider, "temperature"))

    def test_temperature_strip_warning_fires_once_per_model_across_attempts(self):
        cfg_extra = {"ensemble": {"attempts": [
            {"provider": "anthropic", "model": "claude-sonnet-4-6",
             "temperature": 0.2},
            {"provider": "anthropic", "model": "claude-sonnet-4-6",
             "temperature": 0.7},
        ]}}
        with self.assertLogs("kickass_loop_engineer.config", level="WARNING") as captured:
            orch = self._orch(cfg_extra)
        orch.builder_factory(orch.attempt_specs[0])
        orch.builder_factory(orch.attempt_specs[1])
        strip_warnings = [line for line in captured.output if "temperature" in line]
        self.assertEqual(len(strip_warnings), 1)

    def test_claude_code_attempt_with_temperature_is_stripped_and_constructs(self):
        from kickass_loop_engineer.providers.claude_code import ClaudeCodeProvider
        with self.assertLogs("kickass_loop_engineer.config", level="WARNING"):
            orch = self._orch({"ensemble": {"attempts": [
                {"provider": "claude_code", "temperature": 0.9}]}})
        builder = orch.builder_factory(orch.attempt_specs[0])
        self.assertIsInstance(builder.provider, ClaudeCodeProvider)

    def test_guard_layer1_rejects_attempt_spec_in_the_reviewer_family(self):
        cfg_extra = {"ensemble": {"attempts": [
            {"model": "kimi-k2.7-code:cloud"},
            {"model": "qwen2.5-coder"},  # same family as the reviewer
        ]}}
        with self.assertRaises(CrossModelReviewError):
            self._orch(cfg_extra)

    def test_guard_layer1_default_ladder_checks_the_base_builder_family(self):
        cfg = {"builder": {"provider": "ollama", "model": "qwen-builder"},
               "reviewer": {"provider": "ollama", "model": "qwen2.5"}}
        with self.assertRaises(CrossModelReviewError):
            build_orchestrator(cfg, workspace=tempfile.mkdtemp(), month="2026-07",
                               objective=Objective(goal="g", done_when="d"))

    def test_per_attempt_family_declares_independence(self):
        orch = self._orch({"ensemble": {"attempts": [
            {"provider": "openai_compat", "model": "mystery-x",
             "family": "alpha"}]}})
        self.assertEqual(orch.attempt_specs[0].family, "alpha")

    def test_two_unknown_rejection_stays_for_undeclared_attempts(self):
        cfg = {"builder": {"provider": "ollama", "model": "kimi-k2.7-code:cloud"},
               "reviewer": {"provider": "ollama", "model": "mystery-r"},
               "ensemble": {"attempts": [{"model": "mystery-x"}]}}
        with self.assertRaises(CrossModelReviewError):
            build_orchestrator(cfg, workspace=tempfile.mkdtemp(), month="2026-07",
                               objective=Objective(goal="g", done_when="d"))

    def test_cross_provider_attempt_drops_base_builder_kwargs(self):
        # The base builder section carries ollama-only kwargs (host); an
        # attempt naming a DIFFERENT provider must not forward them.
        cfg = {"builder": {"provider": "ollama", "model": "kimi-k2.7-code:cloud",
                           "host": "http://localhost:11434"},
               "reviewer": {"provider": "ollama", "model": "qwen2.5"},
               "ensemble": {"attempts": [
                   {"provider": "openai_compat", "model": "gpt-5.5", "family": "gpt"},
                   {"model": "llama3.3"},
               ]}}
        orch = build_orchestrator(cfg, workspace=tempfile.mkdtemp(), month="2026-07",
                                  objective=Objective(goal="g", done_when="d"))
        self.assertEqual(orch.ensemble_n, 2)
        second = orch.builder_factory(orch.attempt_specs[1])
        self.assertEqual(second.provider.host, "http://localhost:11434")

    def test_model_roster(self):
        from kickass_loop_engineer.config import model_roster
        r = model_roster({"models": {"builder": "kimi-k2.7-code", "reviewer": "qwen2.5",
                                     "arbiter": "claude"}})
        self.assertEqual(r["builder"], "kimi-k2.7-code")
        self.assertEqual(r["reviewer"], "qwen2.5")


class ParseGatesTests(unittest.TestCase):
    def test_empty_config_yields_default_unit_gate(self):
        from kickass_loop_engineer.config import parse_gates
        from kickass_loop_engineer.gates import GateSpec
        self.assertEqual(parse_gates({}), [GateSpec()])

    def test_mapping_back_compat_yields_single_spec(self):
        from kickass_loop_engineer.config import parse_gates
        specs = parse_gates({"gates": {"name": "unit", "cmd": "pytest -q",
                                       "prove": False}})
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].name, "unit")
        self.assertEqual(specs[0].command, "pytest -q")
        self.assertIs(specs[0].prove, False)
        self.assertEqual(specs[0].observe, "")

    def test_list_yields_specs_in_order_with_observe(self):
        from kickass_loop_engineer.config import parse_gates
        specs = parse_gates({"gates": [
            {"name": "unit", "cmd": "pytest -q"},
            {"name": "ui", "cmd": "npx playwright test",
             "observe": "npx playwright-cli snapshot http://localhost:3000"},
        ]})
        self.assertEqual([s.name for s in specs], ["unit", "ui"])
        self.assertIs(specs[0].prove, True)
        self.assertEqual(specs[0].observe, "")
        self.assertEqual(specs[1].command, "npx playwright test")
        self.assertEqual(specs[1].observe,
                         "npx playwright-cli snapshot http://localhost:3000")

    def test_string_gate_entry_raises_clean_runtime_error(self):
        from kickass_loop_engineer.config import parse_gates
        with self.assertRaises(RuntimeError) as ctx:
            parse_gates({"gates": ["pytest -q"]})
        self.assertIn("mapping", str(ctx.exception))

    def test_scalar_gates_section_raises_clean_runtime_error(self):
        from kickass_loop_engineer.config import parse_gates
        with self.assertRaises(RuntimeError) as ctx:
            parse_gates({"gates": "pytest -q"})
        self.assertIn("mapping", str(ctx.exception))


class BuildVerificationPolicyTests(unittest.TestCase):
    def test_empty_config_yields_bundled_allowlist_only(self):
        from kickass_loop_engineer.config import build_verification_policy
        from kickass_loop_engineer.guardrails import DEFAULT_VERIFY_PREFIXES
        policy = build_verification_policy({})
        self.assertEqual(policy.allowed_prefixes, DEFAULT_VERIFY_PREFIXES)

    def test_scalar_extra_prefixes_rejected_not_split_per_character(self):
        from kickass_loop_engineer.config import build_verification_policy
        cfg = {"guardrails": {"extra_verify_prefixes": "mytool check"}}
        with self.assertRaises(RuntimeError) as ctx:
            build_verification_policy(cfg)
        self.assertIn("LIST", str(ctx.exception))

    def test_null_extra_prefixes_behaves_as_absent(self):
        from kickass_loop_engineer.config import build_verification_policy
        from kickass_loop_engineer.guardrails import DEFAULT_VERIFY_PREFIXES
        policy = build_verification_policy({"guardrails": {"extra_verify_prefixes": None}})
        self.assertEqual(policy.allowed_prefixes, DEFAULT_VERIFY_PREFIXES)

    def test_extra_prefixes_extend_allowlist_and_warn(self):
        from kickass_loop_engineer.config import build_verification_policy
        cfg = {"guardrails": {"extra_verify_prefixes": ["mytool check"]}}
        with self.assertLogs("kickass_loop_engineer.config", level="WARNING") as captured:
            policy = build_verification_policy(cfg)
        self.assertTrue(any("mytool check" in line for line in captured.output),
                        "warning should name the extra prefixes")
        self.assertIs(policy.validate("mytool check .")[0], True)


class BuildOrchestratorTests(unittest.TestCase):
    def test_build_orchestrator_wires_defaults(self):
        tmp = tempfile.mkdtemp()
        cfg = {"builder": {"provider": "ollama", "model": "kimi-k2.7-code:cloud"},
               "models": {"builder": "kimi-k2.7-code:cloud", "reviewer": "qwen2.5"}}
        orch = build_orchestrator(cfg, workspace=tmp, month="2026-07",
                                  objective=Objective(goal="g", done_when="pytest -q passes"))
        self.assertEqual([g.name for g in orch.gates], ["unit"])
        self.assertIs(orch.gates[0].prove, True)
        self.assertEqual(orch.ensemble_n, 2)
        self.assertTrue(orch.run_id)

    def test_build_orchestrator_format_retries_defaults_to_one(self):
        tmp = tempfile.mkdtemp()
        cfg = {"builder": {"provider": "ollama", "model": "kimi-k2.7-code:cloud"},
               "models": {"builder": "kimi-k2.7-code:cloud", "reviewer": "qwen2.5"}}
        orch = build_orchestrator(cfg, workspace=tmp, month="2026-07",
                                  objective=Objective(goal="g", done_when="pytest -q passes"))
        self.assertEqual(orch.format_retries, 1)

    def test_build_orchestrator_wires_format_retries(self):
        tmp = tempfile.mkdtemp()
        cfg = {"builder": {"provider": "ollama", "model": "kimi-k2.7-code:cloud"},
               "models": {"builder": "kimi-k2.7-code:cloud", "reviewer": "qwen2.5"},
               "format_retries": 2}
        orch = build_orchestrator(cfg, workspace=tmp, month="2026-07",
                                  objective=Objective(goal="g", done_when="pytest -q passes"))
        self.assertEqual(orch.format_retries, 2)

    def test_build_orchestrator_wires_format_retries_to_reviewer(self):
        tmp = tempfile.mkdtemp()
        cfg = {"builder": {"provider": "ollama", "model": "kimi-k2.7-code:cloud"},
               "models": {"builder": "kimi-k2.7-code:cloud", "reviewer": "qwen2.5"},
               "format_retries": 2}
        orch = build_orchestrator(cfg, workspace=tmp, month="2026-07",
                                  objective=Objective(goal="g", done_when="pytest -q passes"))
        self.assertEqual(orch.reviewer.reviewer.format_retries, 2)

    def test_build_orchestrator_reviewer_format_retries_defaults_to_one(self):
        tmp = tempfile.mkdtemp()
        cfg = {"builder": {"provider": "ollama", "model": "kimi-k2.7-code:cloud"},
               "models": {"builder": "kimi-k2.7-code:cloud", "reviewer": "qwen2.5"}}
        orch = build_orchestrator(cfg, workspace=tmp, month="2026-07",
                                  objective=Objective(goal="g", done_when="pytest -q passes"))
        self.assertEqual(orch.reviewer.reviewer.format_retries, 1)

    def test_build_orchestrator_wires_verification_policy(self):
        tmp = tempfile.mkdtemp()
        cfg = {"builder": {"provider": "ollama", "model": "kimi-k2.7-code:cloud"},
               "models": {"builder": "kimi-k2.7-code:cloud", "reviewer": "qwen2.5"}}
        orch = build_orchestrator(cfg, workspace=tmp, month="2026-07",
                                  objective=Objective(goal="g", done_when="pytest -q passes"))
        self.assertIsNotNone(orch.policy)
        self.assertIs(orch.policy.validate("pytest -q")[0], True)

    def test_build_orchestrator_wires_gate_list(self):
        tmp = tempfile.mkdtemp()
        cfg = {"builder": {"provider": "ollama", "model": "kimi-k2.7-code:cloud"},
               "models": {"builder": "kimi-k2.7-code:cloud", "reviewer": "qwen2.5"},
               "gates": [
                   {"name": "unit", "cmd": "pytest -q", "prove": False},
                   {"name": "smoke", "cmd": "make test"},
               ]}
        orch = build_orchestrator(cfg, workspace=tmp, month="2026-07",
                                  objective=Objective(goal="g", done_when="pytest -q passes"))
        self.assertEqual([g.name for g in orch.gates], ["unit", "smoke"])
        self.assertEqual([g.command for g in orch.gates], ["pytest -q", "make test"])
        self.assertIs(orch.gates[0].prove, False)
        self.assertIs(orch.gates[1].prove, True)

    def test_build_orchestrator_rejects_same_family_models(self):
        tmp = tempfile.mkdtemp()
        cfg = {"models": {"builder": "kimi-a", "reviewer": "kimi-b"}}
        with self.assertRaises(CrossModelReviewError):
            build_orchestrator(cfg, workspace=tmp, month="2026-07",
                               objective=Objective(goal="g", done_when="d"))

    def test_build_orchestrator_checks_actual_wired_models(self):
        tmp = tempfile.mkdtemp()
        cfg = {"models": {"builder": "kimi-a", "reviewer": "qwen2.5"},
               "reviewer": {"provider": "ollama", "model": "kimi-b"}}
        with self.assertRaises(CrossModelReviewError):
            build_orchestrator(cfg, workspace=tmp, month="2026-07",
                               objective=Objective(goal="g", done_when="d"))

    def test_build_orchestrator_rejects_same_provider_without_model(self):
        tmp = tempfile.mkdtemp()
        cfg = {"builder": {"provider": "claude_code", "force_subscription": True},
               "reviewer": {"provider": "claude_code", "force_subscription": True}}
        with self.assertRaises(CrossModelReviewError):
            build_orchestrator(cfg, workspace=tmp, month="2026-07",
                               objective=Objective(goal="g", done_when="d"))

    def test_build_orchestrator_reviewer_section_consistent_passes(self):
        tmp = tempfile.mkdtemp()
        cfg = {"models": {"builder": "kimi-a", "reviewer": "qwen2.5"},
               "reviewer": {"provider": "ollama", "model": "qwen2.5-coder"}}
        orch = build_orchestrator(cfg, workspace=tmp, month="2026-07",
                                  objective=Objective(goal="g", done_when="d"))
        self.assertTrue(orch.run_id)

    def test_build_orchestrator_declared_families_allow_unknown_models(self):
        tmp = tempfile.mkdtemp()
        cfg = {"builder": {"provider": "ollama", "model": "mystery-a", "family": "alpha"},
               "reviewer": {"provider": "ollama", "model": "mystery-b", "family": "beta"}}
        orch = build_orchestrator(cfg, workspace=tmp, month="2026-07",
                                  objective=Objective(goal="g", done_when="d"))
        self.assertEqual(orch.reviewer.builder_family, "alpha")
        self.assertEqual(orch.reviewer.reviewer_family, "beta")

    def test_build_orchestrator_declared_same_family_rejected(self):
        tmp = tempfile.mkdtemp()
        cfg = {"builder": {"provider": "ollama", "model": "mystery-a", "family": "alpha"},
               "reviewer": {"provider": "ollama", "model": "mystery-b", "family": "alpha"}}
        with self.assertRaises(CrossModelReviewError):
            build_orchestrator(cfg, workspace=tmp, month="2026-07",
                               objective=Objective(goal="g", done_when="d"))

    def test_build_builder_pops_family_before_provider_construction(self):
        from kickass_loop_engineer.config import build_builder
        builder = build_builder({"builder": {"provider": "ollama", "model": "mystery-a",
                                             "family": "alpha"}})
        self.assertEqual(builder.provider.model, "mystery-a")
        self.assertFalse(hasattr(builder.provider, "family"))

    def test_build_builder_resolves_context_window_from_bundled_pricing(self):
        from kickass_loop_engineer.config import build_builder
        builder = build_builder({"builder": {"provider": "ollama",
                                             "model": "kimi-k2.7-code:cloud"}})
        self.assertEqual(builder.context_window, 256000)

    def test_build_builder_accepts_explicit_pricing_table(self):
        from kickass_loop_engineer.config import build_builder
        table = {"kimi": {"in": 0.0, "out": 0.0, "context_window": 1234}}
        builder = build_builder({"builder": {"provider": "ollama",
                                             "model": "kimi-k2.7-code:cloud"}},
                                pricing=table)
        self.assertEqual(builder.context_window, 1234)

    def test_build_builder_unknown_model_gets_zero_window(self):
        from kickass_loop_engineer.config import build_builder
        builder = build_builder({"builder": {"provider": "ollama", "model": "mystery-a",
                                             "family": "alpha"}})
        self.assertEqual(builder.context_window, 0)

    def test_build_orchestrator_passes_its_pricing_table_to_the_builder(self):
        tmp = tempfile.mkdtemp()
        cfg = {"builder": {"provider": "ollama", "model": "kimi-k2.7-code:cloud"},
               "models": {"builder": "kimi-k2.7-code:cloud", "reviewer": "qwen2.5"},
               "pricing": {"kimi": {"in": 0.0, "out": 0.0, "context_window": 4321}}}
        orch = build_orchestrator(cfg, workspace=tmp, month="2026-07",
                                  objective=Objective(goal="g", done_when="pytest -q passes"))
        self.assertEqual(orch.builder.context_window, 4321)

    def test_build_loop_is_gone(self):
        import kickass_loop_engineer as pkg
        self.assertFalse(hasattr(pkg, "build_loop"))
        self.assertFalse(hasattr(pkg, "RunStatus"))


class UiBuilderRulesTests(unittest.TestCase):
    _BUILDER = {"provider": "ollama", "model": "kimi-k2.7-code:cloud"}
    _UI_GATES = [
        {"name": "unit", "cmd": "pytest -q"},
        {"name": "ui", "cmd": "npx playwright test",
         "observe": "npx playwright-cli snapshot http://localhost:3000"},
    ]

    def test_ui_gate_appends_ui_rules_to_builder_system_prompt(self):
        from kickass_loop_engineer.agents import BUILDER_SYSTEM, UI_BUILDER_RULES
        from kickass_loop_engineer.config import build_builder
        builder = build_builder({"builder": self._BUILDER, "gates": self._UI_GATES})
        self.assertTrue(builder.system_prompt.startswith(BUILDER_SYSTEM))
        self.assertTrue(builder.system_prompt.endswith(UI_BUILDER_RULES))

    def test_non_ui_gates_leave_builder_prompt_byte_identical(self):
        from kickass_loop_engineer.agents import BUILDER_SYSTEM
        from kickass_loop_engineer.config import build_builder
        builder = build_builder({"builder": self._BUILDER, "gates": [
            {"name": "unit", "cmd": "pytest -q"},
            {"name": "security", "cmd": "bandit -r ."},
        ]})
        self.assertEqual(builder.system_prompt, BUILDER_SYSTEM)

    def test_default_config_leaves_builder_prompt_byte_identical(self):
        from kickass_loop_engineer.agents import BUILDER_SYSTEM
        from kickass_loop_engineer.config import build_builder
        self.assertEqual(build_builder({}).system_prompt, BUILDER_SYSTEM)

    def test_build_orchestrator_wires_ui_rules_into_its_builder(self):
        from kickass_loop_engineer.agents import UI_BUILDER_RULES
        cfg = {"builder": self._BUILDER,
               "models": {"builder": "kimi-k2.7-code:cloud", "reviewer": "qwen2.5"},
               "gates": self._UI_GATES}
        orch = build_orchestrator(cfg, workspace=tempfile.mkdtemp(), month="2026-07",
                                  objective=Objective(goal="g", done_when="d"))
        self.assertIn(UI_BUILDER_RULES, orch.builder.system_prompt)


class VisualConfigTests(unittest.TestCase):
    _CMD = "npx playwright-cli screenshot http://localhost:3000 shot.png"

    def _orch(self, extra):
        cfg = {"builder": {"provider": "ollama", "model": "kimi-k2.7-code:cloud"},
               "models": {"builder": "kimi-k2.7-code:cloud", "reviewer": "qwen2.5"}}
        cfg.update(extra)
        return build_orchestrator(cfg, workspace=tempfile.mkdtemp(), month="2026-07",
                                  objective=Objective(goal="g", done_when="d"))

    def test_absent_visual_section_disables_the_feature(self):
        orch = self._orch({})
        self.assertIsNone(orch.visual_cfg)
        self.assertIsNone(orch.visual_provider)

    def test_visual_section_wires_screenshot_cmd_and_nested_provider(self):
        orch = self._orch({"visual": {
            "screenshot_cmd": self._CMD,
            "provider": {"provider": "ollama", "model": "llava"},
        }})
        self.assertEqual(orch.visual_cfg, {"screenshot_cmd": self._CMD})
        self.assertEqual(orch.visual_provider.model, "llava")

    def test_visual_provider_section_accepts_openai_compat(self):
        from kickass_loop_engineer.providers.openai_compat import OpenAICompatProvider
        orch = self._orch({"visual": {
            "screenshot_cmd": self._CMD,
            "provider": {"provider": "openai_compat", "model": "gpt-5.5",
                         "base_url": "http://localhost:9"},
        }})
        self.assertIsInstance(orch.visual_provider, OpenAICompatProvider)

    def test_visual_without_provider_section_raises_clean_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._orch({"visual": {"screenshot_cmd": self._CMD}})
        self.assertIn("provider", str(ctx.exception))

    def test_visual_without_screenshot_cmd_raises_clean_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._orch({"visual": {"provider": {"provider": "ollama", "model": "llava"}}})
        self.assertIn("screenshot_cmd", str(ctx.exception))

    def test_visual_provider_section_without_provider_name_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._orch({"visual": {"screenshot_cmd": self._CMD,
                                   "provider": {"model": "llava"}}})
        self.assertIn("provider", str(ctx.exception))

    def test_scalar_visual_section_raises_clean_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._orch({"visual": "yes please"})
        self.assertIn("visual", str(ctx.exception))


class ExampleConfigTests(unittest.TestCase):
    def test_example_file_matches_single_source(self):
        from kickass_loop_engineer.config import EXAMPLE_CONFIG
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "loop-engineer.example.yaml"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), EXAMPLE_CONFIG)

    def test_example_config_is_valid_yaml_with_expected_sections(self):
        import yaml
        from kickass_loop_engineer.config import EXAMPLE_CONFIG
        parsed = yaml.safe_load(EXAMPLE_CONFIG)
        self.assertEqual(parsed["builder"]["provider"], "ollama")

    def test_example_config_gates_parse_to_default_unit_gate(self):
        import yaml
        from kickass_loop_engineer.config import EXAMPLE_CONFIG, parse_gates
        from kickass_loop_engineer.gates import GateSpec
        parsed = yaml.safe_load(EXAMPLE_CONFIG)
        self.assertEqual(parse_gates(parsed), [GateSpec()])

    def test_example_config_documents_3_0_keys(self):
        from kickass_loop_engineer.config import EXAMPLE_CONFIG
        for key in ("openai_compat", "base_url", "api_key_env", "family",
                    "format_retries", "pricing_path", "extra_verify_prefixes",
                    "observe"):
            self.assertIn(key, EXAMPLE_CONFIG, f"starter template should document {key!r}")

    def test_example_config_documents_visual_section_in_nested_provider_form(self):
        from kickass_loop_engineer.config import EXAMPLE_CONFIG
        for key in ("visual:", "screenshot_cmd", "advisory"):
            self.assertIn(key, EXAMPLE_CONFIG, f"starter template should document {key!r}")
        # the nested visual.provider form (deliberate deviation from the spec's
        # flat sketch): a provider section under visual, standard provider keys
        self.assertRegex(EXAMPLE_CONFIG, r"#\s+provider:.*\n#\s+provider:")

    def test_example_config_offers_an_agentic_builder(self):
        from kickass_loop_engineer.config import EXAMPLE_CONFIG
        self.assertIn("provider: claude_code", EXAMPLE_CONFIG)
        self.assertIn("agentic: edits the worktree in place (Claude Max subscription)",
                      EXAMPLE_CONFIG)

    def test_example_config_lists_gemini_among_the_providers(self):
        from kickass_loop_engineer.config import EXAMPLE_CONFIG
        self.assertIn("gemini", EXAMPLE_CONFIG)

    def test_example_config_observer_comment_is_no_longer_stale(self):
        from kickass_loop_engineer.config import EXAMPLE_CONFIG
        self.assertNotIn("lands in M3", EXAMPLE_CONFIG)

    def test_example_config_documents_ensemble_attempts(self):
        from kickass_loop_engineer.config import EXAMPLE_CONFIG
        for key in ("attempts:", "temperature:"):
            self.assertIn(key, EXAMPLE_CONFIG,
                          f"starter template should document {key!r}")

    def test_example_config_documents_task_mode_with_every_choice(self):
        from kickass_loop_engineer.config import EXAMPLE_CONFIG
        from kickass_loop_engineer.modes import REQUESTABLE_MODES
        self.assertIn("mode:", EXAMPLE_CONFIG)
        for mode in REQUESTABLE_MODES:
            self.assertIn(mode, EXAMPLE_CONFIG,
                          f"starter template should list mode {mode!r}")

    def test_scaffolded_config_plus_ui_snippet_builds_an_orchestrator(self):
        # Regression test for the scaffolded surface: everything
        # `loop-engineer init --ui` writes must construct cleanly.
        import yaml
        from kickass_loop_engineer.config import EXAMPLE_CONFIG, UI_GATES_SNIPPET
        parsed = yaml.safe_load(EXAMPLE_CONFIG + UI_GATES_SNIPPET)
        orch = build_orchestrator(
            parsed, workspace=tempfile.mkdtemp(), month="2026-07",
            objective=Objective(goal="g", done_when="d"))
        self.assertEqual([g.name for g in orch.gates], ["unit", "ui"])
        self.assertEqual(orch.mode, "auto")

    def test_scaffolded_config_without_ui_snippet_builds_an_orchestrator(self):
        import yaml
        from kickass_loop_engineer.config import EXAMPLE_CONFIG
        parsed = yaml.safe_load(EXAMPLE_CONFIG)
        orch = build_orchestrator(
            parsed, workspace=tempfile.mkdtemp(), month="2026-07",
            objective=Objective(goal="g", done_when="d"))
        self.assertEqual(orch.mode, "auto")


class ArchitectModeTests(unittest.TestCase):
    def test_default_is_auto(self):
        self.assertEqual(architect_mode({}), "auto")

    def test_accepts_on_off_auto_including_yaml_booleans(self):
        for value, expected in (("on", "on"), ("off", "off"), ("auto", "auto"),
                                (True, "on"), (False, "off")):
            self.assertEqual(architect_mode({"architect": value}), expected)

    def test_invalid_value_raises_runtime_error_listing_valid_values(self):
        with self.assertRaises(RuntimeError) as ctx:
            architect_mode({"architect": "sometimes"})
        message = str(ctx.exception)
        self.assertIn("sometimes", message)
        for valid in ("auto", "on", "off"):
            self.assertIn(valid, message)

    def test_build_orchestrator_passes_architect_mode(self):
        tmp = tempfile.mkdtemp()
        orch = build_orchestrator({"architect": "on"}, workspace=tmp, month="2026-07",
                                  objective=Objective(goal="g", done_when="d"))
        self.assertEqual(orch.architect_mode, "on")

    def test_build_orchestrator_default_architect_mode_is_auto(self):
        tmp = tempfile.mkdtemp()
        orch = build_orchestrator({}, workspace=tmp, month="2026-07",
                                  objective=Objective(goal="g", done_when="d"))
        self.assertEqual(orch.architect_mode, "auto")

    def test_build_orchestrator_rejects_invalid_architect_mode(self):
        tmp = tempfile.mkdtemp()
        with self.assertRaises(RuntimeError):
            build_orchestrator({"architect": "maybe"}, workspace=tmp, month="2026-07",
                               objective=Objective(goal="g", done_when="d"))


class TaskModeTests(unittest.TestCase):
    def test_default_is_auto(self):
        self.assertEqual(task_mode({}), "auto")

    def test_accepts_every_requestable_mode(self):
        for value in ("auto", "build", "enhance", "fix", "audit"):
            self.assertEqual(task_mode({"mode": value}), value)

    def test_invalid_value_raises_runtime_error_listing_modes(self):
        with self.assertRaises(RuntimeError) as ctx:
            task_mode({"mode": "refactor"})
        message = str(ctx.exception)
        self.assertIn("refactor", message)
        for valid in ("auto", "build", "enhance", "fix", "audit"):
            self.assertIn(valid, message)

    def test_build_orchestrator_passes_mode(self):
        tmp = tempfile.mkdtemp()
        orch = build_orchestrator({"mode": "enhance"}, workspace=tmp, month="2026-07",
                                  objective=Objective(goal="g", done_when="d"))
        self.assertEqual(orch.mode, "enhance")

    def test_build_orchestrator_default_mode_is_auto(self):
        tmp = tempfile.mkdtemp()
        orch = build_orchestrator({}, workspace=tmp, month="2026-07",
                                  objective=Objective(goal="g", done_when="d"))
        self.assertEqual(orch.mode, "auto")

    def test_build_orchestrator_rejects_invalid_mode(self):
        tmp = tempfile.mkdtemp()
        with self.assertRaises(RuntimeError):
            build_orchestrator({"mode": "refactor"}, workspace=tmp, month="2026-07",
                               objective=Objective(goal="g", done_when="d"))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestEscalationAllowTokens:
    """`escalation.allow_tokens` — the per-run exemption for risky keywords."""

    def test_absent_section_returns_empty_tuple(self):
        from kickass_loop_engineer.config import escalation_allow_tokens
        assert escalation_allow_tokens({}) == ()
        assert escalation_allow_tokens({"escalation": {}}) == ()
        assert escalation_allow_tokens({"escalation": {"allow_tokens": None}}) == ()

    def test_valid_list_parses_normalized(self):
        from kickass_loop_engineer.config import escalation_allow_tokens
        parsed = escalation_allow_tokens(
            {"escalation": {"allow_tokens": ["Deploy", "  SPEND  "]}})
        assert parsed == ("deploy", "spend")

    def test_duplicate_tokens_are_collapsed(self):
        from kickass_loop_engineer.config import escalation_allow_tokens
        parsed = escalation_allow_tokens(
            {"escalation": {"allow_tokens": ["deploy", "DEPLOY"]}})
        assert parsed == ("deploy",)

    def test_non_list_raises_runtime_error(self):
        import pytest
        from kickass_loop_engineer.config import escalation_allow_tokens
        with pytest.raises(RuntimeError) as exc:
            escalation_allow_tokens({"escalation": {"allow_tokens": "deploy"}})
        assert "allow_tokens" in str(exc.value)
        assert "'deploy'" in str(exc.value)

    def test_empty_string_entry_raises_runtime_error(self):
        import pytest
        from kickass_loop_engineer.config import escalation_allow_tokens
        with pytest.raises(RuntimeError) as exc:
            escalation_allow_tokens({"escalation": {"allow_tokens": ["deploy", "  "]}})
        assert "non-empty" in str(exc.value)

    def test_unknown_token_raises_runtime_error_naming_the_allowed_set(self):
        import pytest
        from kickass_loop_engineer.config import escalation_allow_tokens
        with pytest.raises(RuntimeError) as exc:
            escalation_allow_tokens({"escalation": {"allow_tokens": ["reboot"]}})
        message = str(exc.value)
        assert "'reboot'" in message
        for known in ("deploy", "delete", "drop table", "spend", "rm -rf",
                      "force push"):
            assert known in message

    def test_each_allowed_token_logs_one_warning(self, caplog):
        import logging as _logging
        from kickass_loop_engineer.config import escalation_allow_tokens
        with caplog.at_level(_logging.WARNING,
                             logger="kickass_loop_engineer.config"):
            escalation_allow_tokens(
                {"escalation": {"allow_tokens": ["deploy", "spend"]}})
        warnings = [r.getMessage() for r in caplog.records
                    if r.levelno == _logging.WARNING]
        assert ("escalation: risky token 'deploy' exempted for this run by config"
                in warnings)
        assert ("escalation: risky token 'spend' exempted for this run by config"
                in warnings)

    def test_build_orchestrator_passes_allow_tokens(self):
        tmp = tempfile.mkdtemp()
        orch = build_orchestrator({"escalation": {"allow_tokens": ["deploy"]}},
                                  workspace=tmp, month="2026-07",
                                  objective=Objective(goal="g", done_when="d"))
        assert orch.escalation_allow_tokens == ("deploy",)

    def test_build_orchestrator_default_allow_tokens_is_empty(self):
        tmp = tempfile.mkdtemp()
        orch = build_orchestrator({}, workspace=tmp, month="2026-07",
                                  objective=Objective(goal="g", done_when="d"))
        assert orch.escalation_allow_tokens == ()
