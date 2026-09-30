"""End-to-end pipeline proofs on a toy repo.

Covers the 2.0-M1 exit criterion (full pipeline with faked providers), the
3.0-M1 one (the same pipeline wired by ``build_orchestrator`` with builder AND
reviewer on the ``openai_compat`` provider, served by local HTTP stubs — the
real provider code path, fully offline), and the 3.1 one (an AGENTIC builder
that edits the attempt worktree in place, with the engine harvesting what
changed).
"""
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from kickass_loop_engineer.agents import Builder, Reviewer
from kickass_loop_engineer.cursor import PipelineCursor
from kickass_loop_engineer.ledger import CostLedger
from kickass_loop_engineer.memory import Memory
from kickass_loop_engineer.notify import Notifier
from kickass_loop_engineer.objective import Objective
from kickass_loop_engineer.orchestrator import GateSpec, Orchestrator
from kickass_loop_engineer.playbook import Playbook
from kickass_loop_engineer.providers.base import Provider, ProviderResult
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
        gates=[GateSpec(name="unit", command="python3 -m pytest -q", prove=True)],
        ensemble_n=2, run_id="e2e-1",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS, outcome.reason
    assert outcome.evidence, "SUCCESS must carry gate evidence"
    assert all(e["passed"] for e in outcome.evidence), "every gates result is green"
    assert any(g.get("proof", {}).get("proven")
               for e in outcome.evidence for g in e["gates"]), "proof-of-test must hold"
    assert (repo / "feature.py").exists(), "winner was promoted into the workspace"
    cursor = json.loads((repo / ".loop-engineer" / "pipeline.json").read_text())
    assert cursor["stage"] == "terminal" and cursor["status"] == "success"
    assert Memory(str(tmp_path / "mem.db")).get("verifier.proven") == "true"
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert ledger["months"]["2026-07"] > 0, "ledger fired engine-side"
    assert subprocess.run(["git", "-C", ws, "worktree", "list", "--porcelain"],
                          capture_output=True, text=True).stdout.count("worktree ") == 1, \
        "all attempt worktrees were cleaned up"


class _OpenAIStubHandler(BaseHTTPRequestHandler):
    """Answers every POST with the server's fixed openai-shaped reply."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.server.requests.append(self.rfile.read(length).decode("utf-8"))
        body = json.dumps({
            "choices": [{"message": {"content": self.server.reply}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7},
        })
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    log_message = lambda *a: None


def _openai_stub(reply):
    """Start a local chat-completions stub; return ``(server, base_url)``.

    The server records every request body on ``server.requests`` and replies
    to all of them with *reply* wrapped in an OpenAI-shaped JSON envelope.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIStubHandler)
    server.reply = reply
    server.requests = []
    threading.Thread(target=server.serve_forever,
                     kwargs={"poll_interval": 0.01}, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.mark.slow
def test_full_pipeline_via_openai_compat_stubs(tmp_path):
    """3.0-M1 exit proof: config-built pipeline, both agents on openai_compat.

    The builder stub answers every call with a FILE block (the decompose call
    sees non-JSON and falls back to a single slice); the reviewer stub always
    answers ``NO FINDINGS``. Models are unknown to the family detector, so the
    config declares families — the same setup a real third-party endpoint
    needs. Everything runs offline against local stubs.
    """
    from kickass_loop_engineer.config import build_orchestrator
    repo = _toy_repo(tmp_path)
    ws = str(repo)
    builder_server, builder_url = _openai_stub(FEATURE)
    reviewer_server, reviewer_url = _openai_stub("NO FINDINGS")
    cfg = {
        "builder": {"provider": "openai_compat", "model": "stub-coder",
                    "base_url": builder_url, "api_key_env": "LOOP_E2E_UNSET_KEY",
                    "family": "alpha"},
        "reviewer": {"provider": "openai_compat", "model": "stub-critic",
                     "base_url": reviewer_url, "api_key_env": "LOOP_E2E_UNSET_KEY",
                     "family": "beta"},
        "ensemble": {"n": 1},
        "ledger": {"path": str(tmp_path / "ledger.json")},
    }
    try:
        orch = build_orchestrator(
            cfg, workspace=ws, month="2026-07",
            objective=Objective(goal="implement double()",
                                done_when="python3 -m pytest -q passes"),
            run_id="e2e-openai-compat")
        outcome = orch.run()
    finally:
        builder_server.shutdown()
        builder_server.server_close()
        reviewer_server.shutdown()
        reviewer_server.server_close()

    assert outcome.state is TerminalState.SUCCESS, outcome.reason
    assert (repo / "feature.py").exists(), "winner was promoted into the workspace"
    assert len(builder_server.requests) >= 2, \
        "builder stub must serve the decompose call and the build round"
    assert len(reviewer_server.requests) >= 1, \
        "reviewer stub must serve the cross-model review"


class _InPlaceStubProvider(Provider):
    """Agentic stub: ``complete`` answers prose, ``edit`` writes files into cwd.

    Stands in for ``claude_code``/``gemini``, which drive their own tools: the
    pipeline must learn what was built by diffing the worktree, not by parsing
    the reply.
    """

    name = "stub-agentic"
    edits_in_place = True

    def __init__(self, files, model="claude-opus-4-8"):
        """Store the ``{relative path: contents}`` every edit turn writes."""
        self.files = dict(files)
        self.model = model
        self.completions = []
        self.edits = []

    def complete(self, system, user):
        # The decompose call lands here; non-JSON prose exercises the
        # single-slice fallback, exactly as a real prose reply would.
        self.completions.append((system, user))
        return ProviderResult(text="I would split this in two, roughly.",
                              tokens=5, model=self.model)

    def edit(self, system, user, *, cwd):
        self.edits.append(cwd)
        for rel, body in self.files.items():
            path = os.path.join(cwd, rel)
            os.makedirs(os.path.dirname(path) or cwd, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(body)
        return ProviderResult(text=f"Edited: {', '.join(sorted(self.files))}",
                              tokens=9, cost_usd=0.01, model=self.model)


@pytest.mark.slow
def test_full_pipeline_with_an_agentic_in_place_builder(tmp_path):
    """3.1 exit proof: an in-place builder converges through the whole pipeline.

    The builder never emits a FILE block — it writes ``feature.py`` into the
    attempt worktree with its own "tools" and answers in prose. The engine must
    harvest that file, gate it (with proof-of-test), review, and promote it.
    """
    repo = _toy_repo(tmp_path)
    ws = str(repo)
    provider = _InPlaceStubProvider(
        {"feature.py": "def double(x):\n    return x * 2\n",
         # A guardrail violation the agent "helpfully" writes: it must be
         # reverted, never promoted.
         ".env": "OPENAI_API_KEY=leaked\n"})
    orch = Orchestrator(
        objective=Objective(goal="implement double()",
                            done_when="python3 -m pytest -q passes"),
        workspace=ws, builder=Builder(provider),
        reviewer=CrossModelReviewer(
            "claude-opus-4-8", "qwen2.5", Reviewer(FakeProvider(["NO FINDINGS"]))),
        worktrees=WorktreeManager(ws, base_dir=str(tmp_path / "wts")),
        ledger=CostLedger(path=str(tmp_path / "ledger.json"), month="2026-07"),
        notifier=Notifier(), memory=Memory(str(tmp_path / "mem.db")),
        playbook=Playbook(str(tmp_path / "playbook.json")),
        cursor=PipelineCursor(ws),
        gates=[GateSpec(name="unit", command="python3 -m pytest -q", prove=True)],
        ensemble_n=1, run_id="e2e-agentic",
    )
    outcome = orch.run()

    assert outcome.state is TerminalState.SUCCESS, outcome.reason
    assert (repo / "feature.py").exists(), "harvested winner was promoted"
    assert not (repo / ".env").exists(), "a protected path must never be promoted"
    assert provider.edits, "the builder was driven through edit(), not complete()"
    assert provider.edits[0] != ws, "the builder edits the ATTEMPT worktree"
    assert provider.completions, "decompose still runs through complete()"
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert ledger["months"]["2026-07"] > 0, "the agentic round was ledgered"
