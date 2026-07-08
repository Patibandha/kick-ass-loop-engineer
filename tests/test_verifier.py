"""Tests for proof-of-test and gate evidence."""
import os
import subprocess
import tempfile
import unittest

from kickass_loop_engineer.verifier import prove, ProofRecord, run_gate, EvidenceRecord


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _init_repo():
    """A git repo whose committed baseline gitignores Python caches."""
    repo = tempfile.mkdtemp()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    with open(os.path.join(repo, ".gitignore"), "w") as f:
        f.write("__pycache__/\n*.pyc\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


class ProofOfTestTests(unittest.TestCase):
    def test_proof_succeeds_when_change_is_real(self):
        repo = _init_repo()
        with open(os.path.join(repo, "test_feature.py"), "w") as f:
            f.write("from impl import answer\n\n"
                    "def test_answer():\n    assert answer() == 42\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "baseline")
        with open(os.path.join(repo, "impl.py"), "w") as f:
            f.write("def answer():\n    return 42\n")
        record = prove(gate_cmd="pytest -q", workspace=repo)
        self.assertIsInstance(record, ProofRecord)
        self.assertTrue(record.green_before, record.error)
        self.assertTrue(record.red_when_reverted, record.error)
        self.assertTrue(record.green_after, record.error)
        self.assertTrue(record.proven)

    def test_clean_tree_reports_needs_uncommitted_change(self):
        repo = _init_repo()
        with open(os.path.join(repo, "test_trivial.py"), "w") as f:
            f.write("def test_trivial():\n    assert True\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "all committed")   # nothing uncommitted
        record = prove(gate_cmd="pytest -q", workspace=repo)
        self.assertFalse(record.proven)
        self.assertIn("uncommitted change", record.error)
        self.assertFalse(record.aborted)              # clean, not a stash failure

    def test_proof_fails_when_gate_passes_without_the_change(self):
        repo = _init_repo()
        with open(os.path.join(repo, "test_trivial.py"), "w") as f:
            f.write("def test_trivial():\n    assert True\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "green baseline")
        with open(os.path.join(repo, "note.txt"), "w") as f:
            f.write("unrelated\n")
        record = prove(gate_cmd="pytest -q", workspace=repo)
        self.assertTrue(record.green_before)
        self.assertFalse(record.red_when_reverted)
        self.assertFalse(record.proven)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.repo = _init_repo()
        with open(os.path.join(self.repo, "test_ok.py"), "w") as f:
            f.write("def test_ok():\n    assert True\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "ok")

    def test_run_gate_returns_evidence_not_opinion(self):
        ev = run_gate("unit", "pytest -q", workspace=self.repo)
        self.assertIsInstance(ev, EvidenceRecord)
        self.assertEqual(ev.gate, "unit")
        self.assertTrue(ev.passed)
        self.assertIn("exit", ev.evidence.lower())

    def test_errored_gate_never_reports_passed(self):
        ev = run_gate("unit", "pytest /no/such/path", workspace=self.repo)
        self.assertFalse(ev.passed)

    def test_run_gate_rejects_unknown_gate_name(self):
        ev = run_gate("not_a_gate", "pytest -q", workspace=self.repo)
        self.assertFalse(ev.passed)
        self.assertIn("unknown gate", ev.evidence)
        self.assertIn("unit", ev.evidence)  # names the known gates


class ProofRobustnessTests(unittest.TestCase):
    def test_rejected_command_reports_real_cause_not_test_failure(self):
        repo = _init_repo()
        open(os.path.join(repo, "x.txt"), "w").write("hi")  # dirty tree
        # 'echo' is not in the verify allowlist -> rejection, not a red test
        record = prove(gate_cmd="echo hi", workspace=repo)
        self.assertFalse(record.green_before)
        self.assertIn("could not run", record.error)
        self.assertNotIn("not green", record.error)

    def test_pop_failure_preserves_change_and_flags_aborted(self):
        import kickass_loop_engineer.verifier as v
        repo = _init_repo()
        with open(os.path.join(repo, "test_feature.py"), "w") as f:
            f.write("from impl import answer\n\ndef test_answer():\n    assert answer()==42\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "baseline")
        with open(os.path.join(repo, "impl.py"), "w") as f:
            f.write("def answer():\n    return 42\n")
        real_git = v._git
        def fake_git(ws, *args, **kw):
            if args[:2] == ("stash", "pop"):
                raise subprocess.CalledProcessError(1, "git stash pop", stderr="CONFLICT")
            return real_git(ws, *args, **kw)
        v._git = fake_git
        try:
            record = prove(gate_cmd="pytest -q", workspace=repo)
        finally:
            v._git = real_git
            # clean up the dangling stash the fake created
            subprocess.run(["git", "stash", "drop"], cwd=repo,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertTrue(record.aborted)
        self.assertIn("stash", record.error)
        self.assertFalse(record.proven)


if __name__ == "__main__":
    unittest.main(verbosity=2)
