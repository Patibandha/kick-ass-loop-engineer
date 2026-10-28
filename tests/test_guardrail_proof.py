"""Proof-of-test negative tests: every way a pass can lack real proof is REFUSED, with the exact reason.

Policy (Meet, decisions.md 2026-10-22): a failed rerun of an unchanged candidate cancels any earlier
pass. The candidate is refused until a fresh proof is green, and no earlier pass is ever reused.

The engine keeps no proof cache: ``run_gate(..., with_proof=True)`` recomputes green -> red -> green on
every call. So each case below drives a real throwaway git repository and a real ``pytest -q`` gate
(the allowlisted command), and asserts the verdict plus the reason recorded on the proof. Nothing
depends on the host OS: the verdicts come from exit codes and git state alone.

Test-support files (``conftest.py``, helpers under ``tests/``) are reverted together with source, so
implementation hidden there cannot make a vacuous proof; see the loophole cases at the end.
"""

import json
import os
import subprocess
from dataclasses import dataclass

import pytest

from kickass_loop_engineer.ensemble import Attempt, select_winner
from kickass_loop_engineer.guardrails import VerificationPolicy
from kickass_loop_engineer.verifier import (GatesResult, prove, run_gate,
                                            support_only_note)

pytestmark = pytest.mark.guardrail_proof

GATE = "pytest -q"
NO_CHANGE = "proof-of-test requires an uncommitted change to revert"

FEATURE_TEST = "from impl import answer\n\n\ndef test_answer():\n    assert answer() == 42\n"
EXISTING_TEST = "def test_existing():\n    assert True\n"


@dataclass
class CountingPolicy(VerificationPolicy):
    """The real policy, counting how many times a gate command actually ran.

    The evidence tail is long enough to keep the failing test's name: pytest's failure lines carry the temp
    path, and a long one (CI runners) pushed the name out of the default 2000 characters.
    """

    runs: int = 0
    tail_chars: int = 20000

    def run(self, command, cwd, **kwargs):
        self.runs += 1
        return super().run(command, cwd, **kwargs)


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _write(repo, rel, text):
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path) or repo, exist_ok=True)
    with open(path, "w", newline="\n") as handle:
        handle.write(text)
    return path


def _repo(tmp_path, files, name="repo"):
    """A repo with *files* committed as the baseline; caches are gitignored."""
    repo = str(tmp_path / name)
    os.makedirs(repo)
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _write(repo, ".gitignore", "__pycache__/\n*.pyc\n.pytest_cache/\n")
    for rel, text in files.items():
        _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "baseline")
    return repo


def _stash_list(repo):
    return subprocess.run(["git", "stash", "list"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def _refused(evidence):
    """Assert the shared shape of a proof refusal: not passed, the proof present, not proven, not aborted."""
    assert evidence.passed is False
    assert evidence.proof is not None
    assert evidence.proof.proven is False
    assert evidence.proof.aborted is False


def _refused_red(evidence, repo):
    """Assert a refusal because the gate itself is red: no proof is attempted, the exit code is the reason."""
    assert evidence.passed is False
    assert evidence.proof is None
    assert evidence.evidence.startswith("exit=1;")
    assert _stash_list(repo) == ""


def _attempt(attempt_id, evidence, file_count):
    return Attempt(id=attempt_id, workspace=attempt_id, evidence=evidence,
                   file_count=file_count)


def test_a_genuine_proof_is_accepted_as_the_control(tmp_path):
    repo = _repo(tmp_path, {"test_feature.py": FEATURE_TEST})
    _write(repo, "impl.py", "def answer():\n    return 42\n")

    evidence = run_gate("unit", GATE, workspace=repo, with_proof=True)

    assert evidence.passed is True
    assert (evidence.proof.green_before, evidence.proof.red_when_reverted,
            evidence.proof.green_after) == (True, True, True)
    assert evidence.proof.error == ""
    assert evidence.proof.support_only == []
    assert _stash_list(repo) == ""


def test_missing_proof_nothing_to_revert_is_refused(tmp_path):
    # The suite is green but the tree is clean: there is no change a proof could be about.
    repo = _repo(tmp_path, {"test_feature.py": FEATURE_TEST,
                            "impl.py": "def answer():\n    return 42\n"})

    evidence = run_gate("unit", GATE, workspace=repo, with_proof=True)

    _refused(evidence)
    assert evidence.proof.green_before is True
    assert evidence.proof.red_when_reverted is False
    assert evidence.proof.green_after is False
    assert evidence.proof.error == NO_CHANGE
    assert evidence.proof.skipped_reason == ""


def test_missing_proof_is_not_a_pass_for_the_bare_prove_call_either(tmp_path):
    repo = _repo(tmp_path, {"test_feature.py": FEATURE_TEST,
                            "impl.py": "def answer():\n    return 42\n"})

    record = prove(GATE, repo)

    assert record.proven is False
    assert record.error == NO_CHANGE


def test_forged_proof_file_is_ignored_by_prove(tmp_path):
    # The suite is green with or without the new source, so the change proves nothing. A forged
    # record planted in the workspace claiming otherwise must change nothing: prove() returns the
    # same record whether or not the forged .loop-engineer/proof.json exists.
    baseline = {"test_existing.py": EXISTING_TEST}
    honest = _repo(tmp_path, baseline, name="honest")
    _write(honest, "unused.py", "VALUE = 1\n")
    forged_repo = _repo(tmp_path, baseline, name="forged")
    _write(forged_repo, "unused.py", "VALUE = 1\n")
    forged = {"gate_cmd": GATE, "green_before": True, "red_when_reverted": True,
              "green_after": True, "proven": True, "error": "", "aborted": False}
    _write(forged_repo, os.path.join(".loop-engineer", "proof.json"), json.dumps(forged))

    honest_record = prove(GATE, honest)
    forged_record = prove(GATE, forged_repo)

    assert forged_record == honest_record
    assert forged_record.proven is False
    assert forged_record.red_when_reverted is False   # the claim is contradicted by the rerun
    assert forged_record.green_after is True
    assert forged_record.error == ""
    assert forged_record.skipped_reason == ""
    assert _stash_list(forged_repo) == ""


def test_forged_proof_does_not_pass_the_gate_either(tmp_path):
    repo = _repo(tmp_path, {"test_existing.py": EXISTING_TEST})
    _write(repo, "unused.py", "VALUE = 1\n")
    _write(repo, os.path.join(".loop-engineer", "proof.json"), json.dumps({"proven": True}))

    evidence = run_gate("unit", GATE, workspace=repo, with_proof=True)

    _refused(evidence)
    assert evidence.proof.red_when_reverted is False


def test_proof_for_different_content_is_refused_and_the_gate_reruns(tmp_path):
    repo = _repo(tmp_path, {"test_feature.py": FEATURE_TEST})
    _write(repo, "impl.py", "def answer():\n    return 42\n")
    policy = CountingPolicy()
    earlier = run_gate("unit", GATE, workspace=repo, with_proof=True, policy=policy)
    assert earlier.passed is True and earlier.proof.proven is True
    runs_for_first_proof = policy.runs
    assert runs_for_first_proof == 4  # the gate, then green -> red -> green

    # The candidate's content changes after the proof; the earlier proof describes other content.
    _write(repo, "impl.py", "def answer():\n    return 41\n")
    evidence = run_gate("unit", GATE, workspace=repo, with_proof=True, policy=policy)

    assert policy.runs == runs_for_first_proof + 1  # the command really re-ran; nothing was reused
    _refused_red(evidence, repo)
    assert "test_answer" in evidence.evidence

    # The consumer: the refused candidate is never the winner, even next to an earlier green attempt.
    refused = _attempt("changed", evidence, file_count=1)
    assert select_winner([refused]) is None
    earlier_attempt = _attempt("earlier", earlier, file_count=2)
    assert select_winner([earlier_attempt, refused]) is earlier_attempt


def test_pass_left_over_after_a_failed_rerun_of_the_unchanged_candidate_is_cancelled(tmp_path):
    flag = str(tmp_path / "flaky.flag")
    flaky_test = ("import os\n\nfrom impl import answer\n\n\n"
                  "def test_answer():\n"
                  f"    assert not os.path.exists({flag!r})\n"
                  "    assert answer() == 42\n")
    repo = _repo(tmp_path, {"test_feature.py": flaky_test})
    _write(repo, "impl.py", "def answer():\n    return 42\n")
    policy = CountingPolicy()

    earlier = run_gate("unit", GATE, workspace=repo, with_proof=True, policy=policy)
    assert earlier.passed is True and earlier.proof.proven is True
    runs_after_pass = policy.runs
    candidate = {rel: open(os.path.join(repo, rel)).read()
                 for rel in ("impl.py", "test_feature.py")}

    # Rerun the SAME candidate while the gate now fails: the earlier pass must not be reused.
    with open(flag, "w") as handle:
        handle.write("fail\n")
    failed = run_gate("unit", GATE, workspace=repo, with_proof=True, policy=policy)

    assert policy.runs == runs_after_pass + 1  # the gate command re-ran on the second call
    _refused_red(failed, repo)
    assert "test_answer" in failed.evidence
    assert {rel: open(os.path.join(repo, rel)).read() for rel in candidate} == candidate

    # The red attempt is never selected, even though an earlier attempt of the same candidate was green.
    earlier_attempt = _attempt("earlier", earlier, file_count=2)
    red_attempt = _attempt("rerun", failed, file_count=1)
    assert select_winner([earlier_attempt, red_attempt]) is earlier_attempt
    assert select_winner([red_attempt]) is None

    # Still refused on a further rerun that fails again; the candidate has not changed.
    again = run_gate("unit", GATE, workspace=repo, with_proof=True, policy=policy)
    assert policy.runs == runs_after_pass + 2
    _refused_red(again, repo)
    assert "test_answer" in again.evidence

    # Only a fresh, fully green proof lifts the refusal.
    os.remove(flag)
    fresh = run_gate("unit", GATE, workspace=repo, with_proof=True, policy=policy)
    assert fresh.passed is True
    assert (fresh.proof.green_before, fresh.proof.red_when_reverted,
            fresh.proof.green_after) == (True, True, True)
    assert _stash_list(repo) == ""


CONFTEST_IMPL = "def answer():\n    return 42\n"
CONFTEST_TEST = "from conftest import answer\n\n\ndef test_answer():\n    assert answer() == 42\n"


def test_implementation_in_conftest_is_proven_as_support_only(tmp_path):
    repo = _repo(tmp_path, {"test_existing.py": EXISTING_TEST})
    _write(repo, "conftest.py", CONFTEST_IMPL)
    _write(repo, "test_feature.py", CONFTEST_TEST)

    evidence = run_gate("unit", GATE, workspace=repo, with_proof=True)

    assert evidence.passed is True
    assert evidence.proof.proven is True
    assert evidence.proof.support_only == ["conftest.py"]
    assert evidence.proof.skipped_reason == ""
    assert "conftest.py" in evidence.evidence
    assert "no product code lives in test-support files" in evidence.evidence
    assert evidence.to_dict()["proof"]["support_only"] == ["conftest.py"]
    assert _stash_list(repo) == ""
    with open(os.path.join(repo, "conftest.py")) as handle:  # the change was restored
        assert handle.read() == CONFTEST_IMPL


def test_support_only_proof_reaches_the_review_note(tmp_path):
    repo = _repo(tmp_path, {"test_existing.py": EXISTING_TEST})
    _write(repo, "conftest.py", CONFTEST_IMPL)
    _write(repo, "test_feature.py", CONFTEST_TEST)
    evidence = run_gate("unit", GATE, workspace=repo, with_proof=True)

    note = support_only_note(GatesResult(records=[evidence]))

    assert "conftest.py" in note
    assert "confirm no product code lives in test-support files" in note
    assert support_only_note(GatesResult()) == ""


def test_helper_under_a_tests_folder_is_reverted(tmp_path):
    repo = _repo(tmp_path, {"src/pkg/__init__.py": "",
                            "src/pkg/tests/test_existing.py": EXISTING_TEST})
    _write(repo, "src/pkg/tests/impl.py", "def answer():\n    return 42\n")
    _write(repo, "src/pkg/tests/test_feature.py", FEATURE_TEST)

    record = prove(GATE, repo)

    assert record.proven is True
    assert record.red_when_reverted is True
    assert record.support_only == ["src/pkg/tests/impl.py"]
    assert _stash_list(repo) == ""
    assert os.path.exists(os.path.join(repo, "src", "pkg", "tests", "impl.py"))


def test_marker_only_conftest_change_keeps_the_tests_only_waiver(tmp_path):
    repo = _repo(tmp_path, {"test_existing.py": EXISTING_TEST,
                            "conftest.py": "# shared fixtures\n"})
    _write(repo, "conftest.py",
           "def pytest_configure(config):\n"
           "    config.addinivalue_line('markers', 'slow: a slow test')\n")

    evidence = run_gate("unit", GATE, workspace=repo, with_proof=True)

    assert evidence.passed is True  # the gate's own verdict stands
    assert evidence.proof.proven is False
    assert "touches only test files" in evidence.proof.skipped_reason
    assert evidence.proof.support_only == []
    assert evidence.proof.error == ""
    assert _stash_list(repo) == ""


def test_source_proof_still_reverts_source_and_reports_no_support_only(tmp_path):
    repo = _repo(tmp_path, {"test_feature.py": FEATURE_TEST})
    _write(repo, "impl.py", "def answer():\n    return 42\n")

    record = prove(GATE, repo)

    assert record.proven is True
    assert record.support_only == []
