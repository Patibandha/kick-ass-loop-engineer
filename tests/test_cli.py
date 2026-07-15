"""Tests for the loop-engineer CLI surface."""
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from kickass_loop_engineer.cli import build_parser, main
from kickass_loop_engineer.terminal import RunOutcome, TerminalState



class CliTests(unittest.TestCase):
    def test_parser_prog_is_loop_engineer(self):
        self.assertEqual(build_parser().prog, "loop-engineer")

    def test_init_writes_loop_engineer_yaml(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "loop-engineer.yaml")
        rc = main(["init", "--path", path])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(path))
        text = open(path).read()
        self.assertIn("kimi-k2.7-code", text)
        # 1.0 config sections are surfaced (commented) in the starter template
        for section in ("ensemble:", "models:", "ledger:", "notify:"):
            self.assertIn(section, text)

    def test_verify_emits_evidence_record(self):
        repo = tempfile.mkdtemp()
        subprocess.run(["git", "init"], cwd=repo, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        open(os.path.join(repo, "test_ok.py"), "w").write("def test_ok():\n    assert True\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["verify", "--gate", "unit", "--cmd", "pytest -q", "--workspace", repo])
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["gate"], "unit")
        self.assertIn("passed", payload)
        self.assertEqual(rc, 0 if payload["passed"] else 2)


class InitUiTests(unittest.TestCase):
    def _init(self, tmp, extra=()):
        path = os.path.join(tmp, "loop-engineer.yaml")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = main(["init", "--path", path, *extra])
        return rc, path, err.getvalue()

    def test_plain_init_output_is_byte_identical_to_example_config(self):
        from kickass_loop_engineer.config import EXAMPLE_CONFIG
        tmp = tempfile.mkdtemp()
        rc, path, _ = self._init(tmp)
        self.assertEqual(rc, 0)
        self.assertEqual(open(path, encoding="utf-8").read(), EXAMPLE_CONFIG)
        self.assertFalse(os.path.exists(os.path.join(tmp, "playwright.config.ts")))
        self.assertFalse(os.path.exists(os.path.join(tmp, "tests-ui")))

    def test_plain_init_refuses_existing_file(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "loop-engineer.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("sentinel")
        rc, _, err = self._init(tmp)
        self.assertEqual(rc, 1)
        self.assertEqual(open(path, encoding="utf-8").read(), "sentinel")
        self.assertIn("refusing", err)

    def test_init_ui_writes_playwright_scaffold_files(self):
        tmp = tempfile.mkdtemp()
        rc, path, _ = self._init(tmp, ["--ui"])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(path))
        self.assertTrue(os.path.exists(os.path.join(tmp, "playwright.config.ts")))
        self.assertTrue(os.path.exists(os.path.join(tmp, "tests-ui", "smoke.spec.ts")))

    def test_init_ui_yaml_parses_to_unit_plus_ui_gates(self):
        import yaml
        from kickass_loop_engineer.config import parse_gates
        tmp = tempfile.mkdtemp()
        rc, path, _ = self._init(tmp, ["--ui"])
        self.assertEqual(rc, 0)
        specs = parse_gates(yaml.safe_load(open(path, encoding="utf-8").read()))
        self.assertEqual([s.name for s in specs], ["unit", "ui"])
        self.assertEqual(specs[1].command, "npx playwright test")
        self.assertEqual(specs[1].observe,
                         "npx playwright-cli snapshot http://localhost:3000")

    def test_smoke_spec_covers_load_key_element_console_and_axe(self):
        tmp = tempfile.mkdtemp()
        self._init(tmp, ["--ui"])
        spec = open(os.path.join(tmp, "tests-ui", "smoke.spec.ts"),
                    encoding="utf-8").read()
        self.assertIn("KEY_ELEMENT", spec)          # documented placeholder selector
        self.assertIn("edit", spec.lower())          # ...the user is told to edit
        self.assertIn("toBeVisible", spec)           # key-element visibility assertion
        self.assertIn("console", spec)               # no-console-errors assertion
        self.assertIn("AxeBuilder", spec)            # axe-core a11y check
        self.assertIn("violations", spec)

    def test_playwright_config_targets_tests_ui_and_localhost_3000(self):
        tmp = tempfile.mkdtemp()
        self._init(tmp, ["--ui"])
        config = open(os.path.join(tmp, "playwright.config.ts"),
                      encoding="utf-8").read()
        self.assertIn("tests-ui", config)
        self.assertIn("http://localhost:3000", config)

    def test_init_ui_skips_existing_scaffold_file_with_warning(self):
        tmp = tempfile.mkdtemp()
        sentinel_path = os.path.join(tmp, "playwright.config.ts")
        with open(sentinel_path, "w", encoding="utf-8") as handle:
            handle.write("sentinel")
        rc, path, err = self._init(tmp, ["--ui"])
        self.assertEqual(rc, 0)
        self.assertEqual(open(sentinel_path, encoding="utf-8").read(), "sentinel")
        self.assertIn("playwright.config.ts", err)
        # the other files are still written
        self.assertTrue(os.path.exists(path))
        self.assertTrue(os.path.exists(os.path.join(tmp, "tests-ui", "smoke.spec.ts")))

    def test_init_ui_skips_existing_config_with_warning(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "loop-engineer.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("sentinel")
        rc, _, err = self._init(tmp, ["--ui"])
        self.assertEqual(rc, 0)
        self.assertEqual(open(path, encoding="utf-8").read(), "sentinel")
        self.assertIn("loop-engineer.yaml", err)
        self.assertTrue(os.path.exists(os.path.join(tmp, "tests-ui", "smoke.spec.ts")))


class RunCliTests(unittest.TestCase):
    def _run_with_stub(self, outcome):
        tmp = tempfile.mkdtemp()
        stub = mock.Mock()
        stub.run.return_value = outcome
        buf = io.StringIO()
        with mock.patch("kickass_loop_engineer.cli.build_orchestrator", return_value=stub):
            with redirect_stdout(buf):
                rc = main(["run", "--objective", "g", "--done-when", "d", "--workspace", tmp])
        return rc, buf.getvalue()

    def test_run_success_prints_outcome_json(self):
        rc, out = self._run_with_stub(
            RunOutcome(TerminalState.SUCCESS, "ok", evidence=[{"gate": "unit"}]))
        self.assertEqual(rc, 0)
        self.assertIn('"state": "success"', out)

    def test_run_failure_returns_two(self):
        rc, _ = self._run_with_stub(RunOutcome(TerminalState.STALLED, "stuck"))
        self.assertEqual(rc, 2)

    def test_run_reports_cross_model_error_as_json(self):
        tmp = tempfile.mkdtemp()
        cfg_path = os.path.join(tmp, "loop-engineer.yaml")
        with open(cfg_path, "w", encoding="utf-8") as handle:
            handle.write(
                "builder:\n  provider: ollama\n  model: kimi-k2.7-code:cloud\n"
                "reviewer:\n  provider: ollama\n  model: kimi-k2-thinking:cloud\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["run", "--objective", "g", "--done-when", "d",
                       "--workspace", tmp, "--config", cfg_path])
        self.assertEqual(rc, 1)
        payload = json.loads(buf.getvalue())
        self.assertIn("families", payload["error"])


class RefineCliTests(unittest.TestCase):
    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(argv)
        return rc, buf.getvalue()

    def test_refine_reports_full_gap_list(self):
        tmp = tempfile.mkdtemp()
        rc, out = self._run(["refine", "--build", "x", "--done-when", "feels fast",
                             "--workspace", tmp])
        self.assertEqual(rc, 3)
        payload = json.loads(out)
        self.assertFalse(payload["ready"])
        self.assertIsInstance(payload["gaps"], list)
        self.assertGreaterEqual(len(payload["gaps"]), 6)
        self.assertNotIn("questions", payload)

    def test_refine_interview_emits_fixed_questions(self):
        tmp = tempfile.mkdtemp()
        rc, out = self._run(["refine", "--interview", "--build", "x",
                             "--done-when", "feels fast", "--workspace", tmp])
        self.assertEqual(rc, 3)
        payload = json.loads(out)
        self.assertFalse(payload["ready"])
        slots = [q["slot"] for q in payload["questions"]]
        self.assertEqual(slots[0], "done_when")
        self.assertIn("purpose", slots)
        self.assertIn("risks", slots)
        for q in payload["questions"]:
            self.assertTrue({"slot", "question", "hint"} <= set(q))

    def test_refine_ready_with_slots_and_defer(self):
        tmp = tempfile.mkdtemp()
        rc, out = self._run(["refine", "--build", "a todo CLI",
                             "--done-when", "python3 -m pytest -q passes",
                             "--purpose", "track tasks", "--users", "just me",
                             "--success-metric", "python3 -m pytest -q passes",
                             "--defer", "constraints,anti_goals,risks",
                             "--workspace", tmp])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ready"])
        self.assertTrue(os.path.exists(payload["spec"]))
        self.assertTrue(os.path.exists(payload["goal"]))

    def test_refine_exclude_alias_maps_to_anti_goals(self):
        tmp = tempfile.mkdtemp()
        rc, out = self._run(["refine", "--build", "a todo CLI",
                             "--done-when", "python3 -m pytest -q passes",
                             "--purpose", "track tasks", "--users", "just me",
                             "--success-metric", "python3 -m pytest -q passes",
                             "--exclude", "no web UI",
                             "--defer", "constraints,risks",
                             "--workspace", tmp])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ready"])
        text = open(payload["spec"], encoding="utf-8").read()
        anti_goals_section = text.split("## Anti-goals")[1].split("##")[0]
        self.assertIn("no web UI", anti_goals_section)

    def test_refine_unknown_defer_slot_returns_error(self):
        tmp = tempfile.mkdtemp()
        rc, out = self._run(["refine", "--build", "x", "--done-when", "y",
                             "--defer", "nonsense_slot", "--workspace", tmp])
        self.assertEqual(rc, 1)
        payload = json.loads(out)
        self.assertIn("error", payload)


class NextCliTests(unittest.TestCase):
    def test_next_emits_envelope_json(self):
        tmp = tempfile.mkdtemp()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["next", "--workspace", tmp, "--available", "gsd-planner,superpowers"])
        self.assertEqual(rc, 0)
        envelope = json.loads(buf.getvalue())
        self.assertEqual(envelope["protocol"], 1)
        self.assertIn(envelope["next_action"]["type"], {"invoke_skill", "invoke_agent"})

    def test_next_without_available_assumes_owners(self):
        tmp = tempfile.mkdtemp()
        # Research runs first now; seed a citation-free, coverage-passing
        # RESEARCH.md so the machine advances to the plan owner.
        research_dir = os.path.join(tmp, ".loop-engineer")
        os.makedirs(research_dir, exist_ok=True)
        with open(os.path.join(research_dir, "RESEARCH.md"), "w", encoding="utf-8") as handle:
            handle.write("# RESEARCH\n## Decisions\n- [VERIFIED] no external deps\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["next", "--workspace", tmp])
        self.assertEqual(rc, 0)
        envelope = json.loads(buf.getvalue())
        self.assertEqual(envelope["next_action"]["name"], "gsd-planner")


class ResearchIngestCliTests(unittest.TestCase):
    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(argv)
        return rc, buf.getvalue()

    def test_research_ingest_records_claims(self):
        tmp = tempfile.mkdtemp()
        research_dir = os.path.join(tmp, ".loop-engineer")
        os.makedirs(research_dir, exist_ok=True)
        with open(os.path.join(research_dir, "RESEARCH.md"), "w", encoding="utf-8") as handle:
            handle.write(
                "# RESEARCH\n\n"
                "## Decisions\n"
                '- [VERIFIED] Use Textual >= 0.60 (source: https://textual.io "RAD framework for Python")\n'
                '- [CITED] Piper runs on CPU (source: https://github.com/rhasspy/piper "does not require a GPU")\n\n'
                "## Notes\n"
                "- [ASSUMED] User prefers dark theme\n"
            )
        rc, out = self._run(["research-ingest", "--workspace", tmp])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertGreaterEqual(payload["ingested"], 1)

    def test_research_ingest_rejects_ungated_research(self):
        tmp = tempfile.mkdtemp()
        research_dir = os.path.join(tmp, ".loop-engineer")
        os.makedirs(research_dir, exist_ok=True)
        with open(os.path.join(research_dir, "RESEARCH.md"), "w", encoding="utf-8") as handle:
            handle.write("## Decisions\n- [ASSUMED] Use Postgres 16\n")
        rc, out = self._run(["research-ingest", "--workspace", tmp])
        self.assertEqual(rc, 2)
        payload = json.loads(out)
        self.assertIn("load-bearing", payload["error"])


class VersionConsistencyTests(unittest.TestCase):
    def test_version_matches_pyproject(self):
        import pathlib
        import re
        from kickass_loop_engineer import __version__
        pyproj = pathlib.Path("pyproject.toml").read_text()
        match = re.search(r'(?m)^version = "([^"]+)"', pyproj)
        self.assertIsNotNone(match, "pyproject.toml is missing a top-level version field")
        self.assertEqual(__version__, match.group(1))


class _RunConfigCaptureMixin:
    """Runs the CLI `run` command with a stub pipeline; returns the config it got."""

    def _run_capture_config(self, extra_args, config_body=""):
        tmp = tempfile.mkdtemp()
        args = ["run", "--objective", "g", "--done-when", "d", "--workspace", tmp]
        if config_body:
            cfg_path = os.path.join(tmp, "cfg.yaml")
            with open(cfg_path, "w", encoding="utf-8") as handle:
                handle.write(config_body)
            args += ["--config", cfg_path]
        stub = mock.Mock()
        stub.run.return_value = RunOutcome(TerminalState.SUCCESS, "ok", evidence=[{}])
        with mock.patch("kickass_loop_engineer.cli.build_orchestrator",
                        return_value=stub) as factory:
            with redirect_stdout(io.StringIO()):
                rc = main(args + extra_args)
        self.assertEqual(rc, 0)
        return factory.call_args[0][0]


class ArchitectFlagTests(_RunConfigCaptureMixin, unittest.TestCase):
    def test_architect_flag_sets_config_on(self):
        config = self._run_capture_config(["--architect"])
        self.assertEqual(config.get("architect"), "on")

    def test_no_architect_flag_sets_config_off(self):
        config = self._run_capture_config(["--no-architect"])
        self.assertEqual(config.get("architect"), "off")

    def test_architect_flag_overrides_config_file_value(self):
        config = self._run_capture_config(["--architect"],
                                          config_body='architect: "off"\n')
        self.assertEqual(config.get("architect"), "on")

    def test_no_architect_flag_overrides_config_file_value(self):
        config = self._run_capture_config(["--no-architect"],
                                          config_body='architect: "on"\n')
        self.assertEqual(config.get("architect"), "off")

    def test_absent_flags_leave_config_untouched(self):
        config = self._run_capture_config([])
        self.assertNotIn("architect", config)

    def test_architect_flags_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit) as ctx:
            build_parser().parse_args(
                ["run", "--objective", "g", "--done-when", "d",
                 "--architect", "--no-architect"])
        self.assertEqual(ctx.exception.code, 2)


class ModeFlagTests(_RunConfigCaptureMixin, unittest.TestCase):
    """`run --mode` mirrors the --architect override semantics."""

    def test_mode_flag_sets_config_mode(self):
        for mode in ("auto", "build", "enhance", "fix", "audit"):
            config = self._run_capture_config(["--mode", mode])
            self.assertEqual(config.get("mode"), mode)

    def test_mode_flag_overrides_config_file_value(self):
        config = self._run_capture_config(["--mode", "build"],
                                          config_body='mode: "enhance"\n')
        self.assertEqual(config.get("mode"), "build")

    def test_absent_mode_flag_leaves_config_untouched(self):
        config = self._run_capture_config([])
        self.assertNotIn("mode", config)

    def test_mode_flag_rejects_unknown_choice(self):
        with self.assertRaises(SystemExit) as ctx:
            build_parser().parse_args(
                ["run", "--objective", "g", "--done-when", "d",
                 "--mode", "refactor"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
