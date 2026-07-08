"""Tests for config builders."""
import os
import tempfile
import unittest

from kickass_loop_engineer.config import build_ledger, build_notifier, build_orchestrator
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

    def test_model_roster(self):
        from kickass_loop_engineer.config import model_roster
        r = model_roster({"models": {"builder": "kimi-k2.7-code", "reviewer": "qwen2.5",
                                     "arbiter": "claude"}})
        self.assertEqual(r["builder"], "kimi-k2.7-code")
        self.assertEqual(r["reviewer"], "qwen2.5")


class BuildOrchestratorTests(unittest.TestCase):
    def test_build_orchestrator_wires_defaults(self):
        tmp = tempfile.mkdtemp()
        cfg = {"builder": {"provider": "ollama", "model": "kimi-k2.7-code:cloud"},
               "models": {"builder": "kimi-k2.7-code:cloud", "reviewer": "qwen2.5"}}
        orch = build_orchestrator(cfg, workspace=tmp, month="2026-07",
                                  objective=Objective(goal="g", done_when="pytest -q passes"))
        self.assertEqual(orch.gate.name, "unit")
        self.assertEqual(orch.ensemble_n, 2)
        self.assertTrue(orch.run_id)

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

    def test_build_loop_is_gone(self):
        import kickass_loop_engineer as pkg
        self.assertFalse(hasattr(pkg, "build_loop"))
        self.assertFalse(hasattr(pkg, "RunStatus"))


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
