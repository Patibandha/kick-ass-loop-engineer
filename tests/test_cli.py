"""Tests for the loop-engineer CLI surface."""
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
