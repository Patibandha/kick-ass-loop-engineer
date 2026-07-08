"""Protocol conformance: a scripted fake session drives the full next loop.

This is the accepted M1-review addition: protocol regressions must be caught
by pytest, not discovered in a live session. ``FakeSession`` plays the
session's role exactly as ``.claude/skills/loop-engineer/SKILL.md`` instructs
a real session to: it loops on ``emit_next`` and executes envelopes from a
script — for ``invoke_skill``/``invoke_agent`` it writes ``expects.artifact``
(valid or invalid per script), for ``verify_citations`` it writes
``.loop-engineer/citations.md`` (fetched text per ``args.citations``), for
``run_engine`` it stamps the cursor terminal (success or failure per script)
— and it records every envelope so the conformance tests can assert on stage
order, retry bookkeeping, and terminal bookkeeping (``skipped_stages`` /
``failed_verdicts``).

2.0-M4 adds a leading, mandatory ``research`` stage ahead of ``plan``: a
RESEARCH.md coverage/citation gate that fails like a test, deterministically,
engine-side. A **citation-free** RESEARCH.md (no ``(source: …)`` clause on its
claim) clears the gate with no citable claims, so ``select_citations`` is
empty and no ``verify_citations`` round fires — that keeps the happy-path
stage sequence exactly as before, just with ``research`` leading it. The
fabricated-citation test below exercises the ``verify_citations`` round and
the ``RESEARCH_BLOCKED`` terminal explicitly.
"""
import os

from kickass_loop_engineer.cursor import PipelineCursor
from kickass_loop_engineer.next import emit_next

VALID = {
    "plan_v1": "- [ ] Task 1: build it\n",
    "review_v1": "NO FINDINGS\n",
    "qa_v1": "VERDICT: PASS\n",
    "security_v1": "VERDICT: PASS\n",
    "ship_v1": "SHIPPED: done\n",
}

# Coverage-passing, citation-free: a `[VERIFIED]` Decisions claim with no
# `(source: URL "quote")` clause. `select_citations` finds nothing citable, so
# research clears in one round with no `verify_citations` follow-up.
CITATION_FREE_RESEARCH = "# RESEARCH\n## Decisions\n- [VERIFIED] no external deps\n"


class FakeSession:
    """Executes envelopes like SKILL.md instructs a real session to."""

    def __init__(self, workspace, available, artifact_writer):
        self.ws = str(workspace)
        self.available = available
        self.write = artifact_writer  # (workspace, envelope) -> None
        self.envelopes = []

    def run(self, max_steps=20):
        for _ in range(max_steps):
            env = emit_next(self.ws, available=self.available)
            self.envelopes.append(env)
            action = env["next_action"]["type"]
            if action in ("terminal", "ask_user"):
                return env
            if action == "run_engine":
                cursor = PipelineCursor(self.ws)
                if cursor.load() is None:
                    cursor.start(run_id="conf-1", objective_goal="g")
                cursor.set_stage("terminal", "success",
                                 {"outcome": {"state": "success", "reason": "ok",
                                              "evidence": [{"gate": "unit", "passed": True}]}})
            else:
                self.write(self.ws, env)
        raise AssertionError("session did not reach a terminal envelope")


def _write_research(ws, artifact_rel, text):
    path = os.path.join(ws, artifact_rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _write_citations(ws, citations, *, include_quote):
    """Write ``.loop-engineer/citations.md`` — one ``## <url>`` section per entry.

    ``include_quote=True`` puts the claimed quote in the fetched body (spot-check
    passes); ``include_quote=False`` writes unrelated text (a fabricated
    citation — spot-check fails).
    """
    path = os.path.join(ws, ".loop-engineer", "citations.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for citation in citations:
            body = citation["quote"] if include_quote else "unrelated fetched text"
            fh.write(f"## {citation['url']}\n{body}\n")


def _write_valid(ws, env):
    action = env["next_action"]
    if action["type"] == "verify_citations":
        _write_citations(ws, action["args"]["citations"], include_quote=True)
        return
    schema = action["expects"]["schema"]
    text = CITATION_FREE_RESEARCH if schema == "research_v1" else VALID[schema]
    _write_research(ws, action["expects"]["artifact"], text)


ALL = {"gsd-planner", "gsd-code-reviewer", "gstack", "superpowers", "security-auditor"}


def test_happy_path_reaches_terminal_in_order(tmp_path):
    session = FakeSession(tmp_path, ALL, _write_valid)
    final = session.run()
    stages = [e["stage"] for e in session.envelopes]
    assert stages == ["research", "plan", "engine", "review", "qa", "security",
                       "ship", "terminal"]
    assert final["next_action"]["type"] == "terminal"
    assert final["status"] == "complete"
    assert final.get("skipped_stages", []) == []
    assert final["evidence"], "terminal envelope carries engine evidence"


def test_stubborn_invalid_artifact_ends_in_ask_user(tmp_path):
    def write_bad_reviews(ws, env):
        if env["next_action"]["expects"]["schema"] == "review_v1":
            _write_research(ws, env["next_action"]["expects"]["artifact"],
                            "looks good to me\n")  # never valid
        else:
            _write_valid(ws, env)

    session = FakeSession(tmp_path, ALL, write_bad_reviews)
    final = session.run()
    assert final["next_action"]["type"] == "ask_user"
    review_envs = [e for e in session.envelopes if e["stage"] == "review"]
    retries = [e.get("retry") for e in review_envs]
    assert retries[:3] == [None, 1, 2], "exactly two retries before ask_user"
    assert "marker" in review_envs[1]["validation_error"]


def test_minimal_toolkit_skips_optional_stages(tmp_path):
    minimal = {"superpowers", "gsd-code-reviewer"}  # no gstack, no security-auditor
    session = FakeSession(tmp_path, minimal, _write_valid)
    final = session.run()
    assert final["next_action"]["type"] == "terminal"
    assert set(final["skipped_stages"]) == {"qa", "security", "ship"}


def test_fabricated_citation_reaches_research_blocked(tmp_path):
    url = "https://x.example"
    quote = "a real quote"
    cited_research = (
        "# RESEARCH\n"
        "## Decisions\n"
        f'- [VERIFIED] Use X (source: {url} "{quote}")\n'
    )

    def write_fabricated(ws, env):
        action = env["next_action"]
        if action["type"] == "verify_citations":
            # Fabricated: the "fetched" page never actually contains the
            # claimed quote — the honesty gate must catch this.
            _write_citations(ws, action["args"]["citations"], include_quote=False)
            return
        if action["expects"]["schema"] == "research_v1":
            _write_research(ws, action["expects"]["artifact"], cited_research)
        else:
            _write_valid(ws, env)

    session = FakeSession(tmp_path, ALL, write_fabricated)
    final = session.run(max_steps=30)
    assert final["next_action"]["type"] == "terminal"
    assert final["status"] == "research_blocked"
    assert url in final["reason"] or quote in final["reason"]
