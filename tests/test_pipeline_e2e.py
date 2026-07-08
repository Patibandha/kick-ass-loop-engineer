"""End-to-end pipeline proof on a toy repo — the 2.0-M1 exit criterion."""
import json
import subprocess

import pytest

from kickass_loop_engineer.agents import Builder, Reviewer
from kickass_loop_engineer.cursor import PipelineCursor
from kickass_loop_engineer.ledger import CostLedger
from kickass_loop_engineer.memory import Memory
from kickass_loop_engineer.notify import Notifier
from kickass_loop_engineer.objective import Objective
from kickass_loop_engineer.orchestrator import GateSpec, Orchestrator
from kickass_loop_engineer.playbook import Playbook
from kickass_loop_engineer.review import CrossModelReviewer
from kickass_loop_engineer.terminal import TerminalState
from kickass_loop_engineer.worktree import WorktreeManager

from helpers import FakeProvider

FEATURE = "=== FILE: feature.py ===\ndef double(x):\n    return x * 2\n=== END FILE ===\n"


def _toy_repo(tmp_path):
    repo = tmp_path / "toy"
    repo.mkdir()
    (repo / "test_feature.py").write_text(
        "from feature import double\n\n\ndef test_double():\n    assert double(2) == 4\n"
    )
    # Build caches must be gitignored (documented precondition of verifier.prove):
    # otherwise the proof's `stash --include-untracked` captures pytest's
    # __pycache__/.pyc, the red-phase run regenerates them, and `stash pop`
    # fails to restore ("already exists, no checkout"), aborting the proof.
    (repo / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.pytest_cache/\n.loop-engineer/\n"
    )
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "red baseline"]):
        subprocess.run(cmd, cwd=repo, check=True)
    return repo


@pytest.mark.slow
def test_full_pipeline_on_toy_repo(tmp_path):
    repo = _toy_repo(tmp_path)
    ws = str(repo)
    # builder responses: 1 for slice generation (garbage -> single-slice fallback),
    # then 1 file-block answer per ensemble attempt (n=2)
    builder = Builder(FakeProvider(["not json", FEATURE, FEATURE], cost_usd=0.01))
    # Cross-model review is part of the exit criterion: wrap the reviewer in
    # CrossModelReviewer (different-family models) so the live run exercises
    # the same review path build_orchestrator wires.
    reviewer = CrossModelReviewer(
        "kimi-k2.7-code:cloud", "qwen2.5", Reviewer(FakeProvider(["NO FINDINGS"]))
    )
    orch = Orchestrator(
        objective=Objective(goal="implement double()", done_when="python3 -m pytest -q passes"),
        workspace=ws, builder=builder, reviewer=reviewer,
        worktrees=WorktreeManager(ws, base_dir=str(tmp_path / "wts")),
        ledger=CostLedger(path=str(tmp_path / "ledger.json"), month="2026-07"),
        notifier=Notifier(), memory=Memory(str(tmp_path / "mem.db")),
        playbook=Playbook(str(tmp_path / "playbook.json")),
        cursor=PipelineCursor(ws),
        gate=GateSpec(name="unit", command="python3 -m pytest -q"),
        ensemble_n=2, run_id="e2e-1", prove=True,
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS, outcome.reason
    assert outcome.evidence, "SUCCESS must carry gate evidence"
    assert any(e.get("proof", {}).get("proven") for e in outcome.evidence), "proof-of-test must hold"
    assert (repo / "feature.py").exists(), "winner was promoted into the workspace"
    cursor = json.loads((repo / ".loop-engineer" / "pipeline.json").read_text())
    assert cursor["stage"] == "terminal" and cursor["status"] == "success"
    assert Memory(str(tmp_path / "mem.db")).get("verifier.proven") == "true"
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert ledger["months"]["2026-07"] > 0, "ledger fired engine-side"
    assert subprocess.run(["git", "-C", ws, "worktree", "list", "--porcelain"],
                          capture_output=True, text=True).stdout.count("worktree ") == 1, \
        "all attempt worktrees were cleaned up"
