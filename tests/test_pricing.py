"""Tests for the bundled pricing registry (load, resolve, estimate, overlays)."""
import importlib.resources
import os
import tempfile
import unittest

from kickass_loop_engineer.pricing import (
    context_window,
    estimate_cost,
    load_pricing,
    resolve,
)


class LoadPricingTests(unittest.TestCase):
    def test_bundled_table_is_nonempty_dict(self):
        table = load_pricing()
        self.assertIsInstance(table, dict)
        self.assertTrue(table)

    def test_pricing_yaml_ships_inside_the_package(self):
        resource = importlib.resources.files("kickass_loop_engineer").joinpath("pricing.yaml")
        self.assertTrue(resource.is_file())

    def test_malformed_pricing_path_raises_runtime_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.yaml")
            with open(bad, "w", encoding="utf-8") as handle:
                handle.write("gpt-5.5: {in: 1.25, out: [unclosed\n")
            with self.assertRaises(RuntimeError):
                load_pricing(path=bad)

    def test_unreadable_pricing_path_raises_runtime_error(self):
        with self.assertRaises(RuntimeError):
            load_pricing(path="/nonexistent/pricing.yaml")

    def test_non_mapping_path_overlay_raises_runtime_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "list.yaml")
            with open(bad, "w", encoding="utf-8") as handle:
                handle.write("- gpt-5.5\n- gpt-\n")  # valid YAML, but a list
            with self.assertRaises(RuntimeError):
                load_pricing(path=bad)


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.table = load_pricing()

    def test_longest_prefix_wins_over_shorter(self):
        row = resolve("gpt-5.5-turbo", self.table)
        self.assertEqual(row, self.table["gpt-5.5"])

    def test_shorter_prefix_catches_other_family_members(self):
        row = resolve("gpt-4o-x", self.table)
        self.assertEqual(row, self.table["gpt-"])

    def test_unknown_model_resolves_to_none(self):
        self.assertIsNone(resolve("nonexistent-model", self.table))

    def test_match_is_case_insensitive(self):
        row = resolve("GPT-5.5", self.table)
        self.assertEqual(row, self.table["gpt-5.5"])


class EstimateCostTests(unittest.TestCase):
    def setUp(self):
        self.table = load_pricing()

    def test_split_usage_prices_prompt_at_input_rate(self):
        cost = estimate_cost(model="gpt-5.5", prompt_tokens=1_000_000,
                             completion_tokens=0, tokens=1_000_000, table=self.table)
        self.assertEqual(cost, self.table["gpt-5.5"]["in"])

    def test_missing_split_charges_all_tokens_at_output_rate(self):
        cost = estimate_cost(model="gpt-5.5", prompt_tokens=0,
                             completion_tokens=0, tokens=1_000_000, table=self.table)
        self.assertEqual(cost, self.table["gpt-5.5"]["out"])

    def test_exact_split_returns_plain_split_math(self):
        # tokens == prompt + completion: no residual, exactly the old split math.
        cost = estimate_cost(model="gpt-5.5", prompt_tokens=1_000_000,
                             completion_tokens=0, tokens=1_000_000, table=self.table)
        self.assertEqual(cost, 1.25)

    def test_partial_split_charges_residual_at_output_rate(self):
        # Residual conservatism: unattributed tokens are charged at the OUT
        # rate on top of the split — never undercharged at $0.
        cost = estimate_cost(model="gpt-5.5", prompt_tokens=0,
                             completion_tokens=50, tokens=1_000_000, table=self.table)
        expected = round((50 * 10.0 + (1_000_000 - 50) * 10.0) / 1_000_000, 6)
        self.assertEqual(cost, expected)

    def test_estimate_rounds_to_six_decimals(self):
        # (1 * 1.25 + 1 * 10.0) / 1e6 = 0.00001125 -> rounded to 6 decimals.
        cost = estimate_cost(model="gpt-5.5", prompt_tokens=1,
                             completion_tokens=1, tokens=2, table=self.table)
        self.assertEqual(cost, 0.000011)

    def test_negative_token_inputs_clamp_to_zero(self):
        cost = estimate_cost(model="gpt-5.5", prompt_tokens=-100,
                             completion_tokens=-100, tokens=-100, table=self.table)
        self.assertEqual(cost, 0.0)

    def test_unknown_model_estimates_to_none(self):
        cost = estimate_cost(model="nonexistent-model", prompt_tokens=100,
                             completion_tokens=100, tokens=200, table=self.table)
        self.assertIsNone(cost)

    def test_local_model_rows_price_to_zero(self):
        for model in ("qwen2.5", "kimi-k2.7-code:cloud", "llama3.1"):
            cost = estimate_cost(model=model, prompt_tokens=1_000_000,
                                 completion_tokens=1_000_000, tokens=2_000_000,
                                 table=self.table)
            self.assertEqual(cost, 0.0, f"expected {model} to be free")


class OverlayTests(unittest.TestCase):
    def test_path_overlay_wins_over_bundled(self):
        with tempfile.TemporaryDirectory() as tmp:
            override = os.path.join(tmp, "pricing.yaml")
            with open(override, "w", encoding="utf-8") as handle:
                handle.write("gpt-5.5: {in: 99.0, out: 999.0, context_window: 1234}\n")
            table = load_pricing(path=override)
        self.assertEqual(table["gpt-5.5"]["in"], 99.0)
        self.assertEqual(table["gpt-5.5"]["out"], 999.0)
        self.assertIn("claude-", table)  # bundled rows survive the overlay

    def test_inline_overlay_wins_over_path_and_bundled(self):
        with tempfile.TemporaryDirectory() as tmp:
            override = os.path.join(tmp, "pricing.yaml")
            with open(override, "w", encoding="utf-8") as handle:
                handle.write("gpt-5.5: {in: 99.0, out: 999.0}\n")
            table = load_pricing(path=override,
                                 inline={"gpt-5.5": {"in": 1.0, "out": 2.0,
                                                     "context_window": 42}})
        self.assertEqual(table["gpt-5.5"]["in"], 1.0)
        self.assertEqual(table["gpt-5.5"]["out"], 2.0)

    def test_overlay_keys_are_lowercased(self):
        table = load_pricing(inline={"GPT-5.5": {"in": 7.0, "out": 8.0}})
        self.assertEqual(table["gpt-5.5"]["in"], 7.0)


class ContextWindowTests(unittest.TestCase):
    def setUp(self):
        self.table = load_pricing()

    def test_known_model_returns_row_value(self):
        self.assertEqual(context_window("gpt-5.5", self.table),
                         self.table["gpt-5.5"]["context_window"])

    def test_unknown_model_returns_zero(self):
        self.assertEqual(context_window("nonexistent-model", self.table), 0)

    def test_row_without_context_window_returns_zero(self):
        table = load_pricing(inline={"bare-model": {"in": 1.0, "out": 2.0}})
        self.assertEqual(context_window("bare-model", table), 0)


if __name__ == "__main__":
    unittest.main()
