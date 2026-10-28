"""Proof-transition tests: a verdict is about the exact bytes it saw, and a torn write is never success.

Policy (Meet, decisions.md 2026-10-22): fail closed; no earlier pass is ever reused.

Three transitions are checked against every place that consumes a gate verdict or a proof:

1. The candidate changes between its gate and its use, including a change written back to identical bytes
   (A -> B -> A with a new write time). ``run_gates`` binds its verdict to a sha256 of the candidate's paths,
   bytes and write times; ``ensemble.select_winner``, ``worktree.promote`` and the orchestrator's pre-promote
   check all refuse a binding that no longer matches.
2. A process killed partway through writing run state (pipeline cursor, journal, STATE.md, review record,
   ``next`` ledger). Each file is truncated or corrupted at every write point, and each writer is killed
   between writing its temp file and replacing the target; no reader may ever see success.
3. A launch failure or timeout of the gate after a valid proof already exists. The rerun ends red, the
   earlier proof is never consulted, and the post-promote gate and a resume both stay red.

Gate processes are scripted (``guardrails.subprocess`` is replaced) so every verdict is definite on every
host; git is real, because the bindings and proofs are about real git state.
"""

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from kickass_loop_engineer import confine, guardrails, orchestrator
from kickass_loop_engineer import verifier as verifier_mod
from kickass_loop_engineer.agents import Builder, Reviewer
from kickass_loop_engineer.cursor import PipelineCursor
from kickass_loop_engineer.ensemble import Attempt, select_winner
from kickass_loop_engineer.gates import GateSpec, default_python
from kickass_loop_engineer.guardrails import VerificationPolicy
from kickass_loop_engineer.next import _load_ledger, _save_ledger, emit_next
from kickass_loop_engineer.objective import Objective
from kickass_loop_engineer.review import write_slice_review
from kickass_loop_engineer.state import RunState
from kickass_loop_engineer.terminal import TerminalState
from kickass_loop_engineer.verifier import GatesResult, run_gate, run_gates, still_fresh
from kickass_loop_engineer.workspace import candidate_digest
from kickass_loop_engineer.worktree import WorktreeError, WorktreeManager

from helpers import FakePolicy, FakeProvider
from test_orchestrator import FILE_BLOCK, TWO_SLICE_JSON, FakeWorktrees, _make, _pass_gates

pytestmark = pytest.mark.guardrail_proof_transition

GATE = f"{default_python()} -m pytest -q"
SPECS = [GateSpec(name="unit", command=GATE, prove=False)]
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
TWO_SECONDS_NS = 2_000_000_000  # a later write time on every filesystem, FAT's 2 s ticks included


# --------------------------------------------------------------------------- helpers


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _write(root, rel, data):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


def _repo(tmp_path, name="repo"):
    """A committed baseline with one test file; the candidate is an uncommitted ``impl.py``."""
    repo = str(tmp_path / name)
    os.makedirs(repo)
    _git(repo, "init", "-q")
    _git(repo, "config", "core.autocrlf", "false")  # the proof's stash and pop must restore the exact bytes
    _write(repo, ".gitignore", "__pycache__/\n*.pyc\n.pytest_cache/\n")
    _write(repo, "test_feature.py", "from impl import answer\n\n\ndef test_answer():\n    assert answer() == 42\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    _write(repo, "impl.py", A)
    return repo


A = b"def answer():\n    return 42\n"
B = b"def answer():\n    return 41\n"


def _rewrite_back(path):
    """A -> B -> A: the same bytes as before, written again, with a definitely later write time."""
    before = os.stat(path)
    with open(path, "rb") as handle:
        original = handle.read()
    _write(os.path.dirname(path), os.path.basename(path), B)
    _write(os.path.dirname(path), os.path.basename(path), original)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + TWO_SECONDS_NS))
    with open(path, "rb") as handle:
        assert handle.read() == original


def _change(path):
    """A -> B."""
    _write(os.path.dirname(path), os.path.basename(path), B)


TRANSITIONS = {"A-to-B": _change, "A-to-B-to-A": _rewrite_back}


def _bound(repo):
    """A passing ``run_gates`` verdict bound to *repo*'s current candidate."""
    result = run_gates(SPECS, repo, policy=FakePolicy(passed=True))
    assert result.passed is True
    assert result.candidate_digest == candidate_digest(repo)
    return result


class ScriptedGate:
    """Stands in for ``subprocess`` inside ``guardrails``: each gate launch takes the next scripted outcome.

    An int is the exit code of a completed process; an exception is raised as the launch outcome. The last
    outcome repeats. git, run by the verifier and the worktree code, is untouched and real.
    """

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.launches = 0

    def run(self, argv, **_kwargs):
        self.launches += 1
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return subprocess.CompletedProcess(argv, outcome, stdout=f"scripted exit {outcome}", stderr="")


def _script(monkeypatch, outcomes):
    gate = ScriptedGate(outcomes)
    scripted = SimpleNamespace(run=gate.run, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(guardrails, "subprocess", scripted)
    monkeypatch.setattr(confine, "subprocess", scripted)  # the gate is launched here
    monkeypatch.setattr(confine, "CONFINEMENT_AVAILABLE", False)
    return gate


LAUNCH_FAILURES = {
    "timeout": (subprocess.TimeoutExpired(cmd="pytest", timeout=300), "timed out after 300s"),
    "not-found": (FileNotFoundError("pytest"), "verification tool not found"),
    "denied": (PermissionError(13, "Permission denied"), "failed to run"),
    "bad-image": (OSError(8, "Exec format error"), "failed to run"),
}


class GitWorktrees(FakeWorktrees):
    """Attempt worktrees that are real git checkouts, promoted by the real ``WorktreeManager.promote``."""

    def __init__(self, base):
        super().__init__(base)
        self.promote_calls = []

    def create(self, name):
        path = super().create(name)
        _git(path, "init", "-q")
        _git(path, "config", "core.autocrlf", "false")
        _git(path, "commit", "-q", "--allow-empty", "-m", "base")
        return path

    def promote(self, src, dest, **kwargs):
        self.promote_calls.append(kwargs)
        promoted = WorktreeManager(self.base, base_dir=self.base).promote(src, dest, **kwargs)
        self.promoted.append((src, dest))
        return promoted


def _attempt_gates(final_policy_factory=lambda: FakePolicy(passed=True)):
    """``run_gates`` for the orchestrator: real gates under a fake policy per attempt, a chosen one at the end."""
    def dispatch(specs, workspace, policy=None):
        main = dispatch.main
        chosen = final_policy_factory() if workspace == main else FakePolicy(passed=True)
        return verifier_mod.run_gates(specs, workspace, policy=chosen)
    dispatch.main = ""
    return dispatch


def _orchestrate(tmp_path, monkeypatch, *, between_gate_and_promote=None, final_policy=None,
                 workspace=None, builder_responses=("not json", FILE_BLOCK), worktrees=None):
    """One single-slice run with real git attempt worktrees; *between_gate_and_promote* runs after review."""
    workspace = workspace or str(tmp_path / "ws")
    os.makedirs(workspace, exist_ok=True)
    dispatch = _attempt_gates(final_policy or (lambda: FakePolicy(passed=True)))
    dispatch.main = workspace
    monkeypatch.setattr(orchestrator, "run_gates", dispatch)
    worktrees = worktrees or GitWorktrees(str(tmp_path / "wts"))
    if between_gate_and_promote is not None:
        def review_then_tamper(*args, **kwargs):
            path = write_slice_review(*args, **kwargs)
            between_gate_and_promote(os.path.join(worktrees.created[-1], "out.py"))
            return path
        monkeypatch.setattr(orchestrator, "write_slice_review", review_then_tamper)
    orch = _make(tmp_path, builder=Builder(FakeProvider(list(builder_responses))),
                 reviewer=Reviewer(FakeProvider(["NO FINDINGS"])), workspace=workspace,
                 worktrees=worktrees, cursor=PipelineCursor(workspace))
    return orch, orch.run()


def _persisted_status(workspace):
    with open(os.path.join(workspace, ".loop-engineer", "pipeline.json"), encoding="utf-8") as handle:
        return json.load(handle)["status"]


# --------------------------------------------------------------------------- 1. run_gates evidence


def test_run_gates_binds_its_verdict_to_the_gated_candidate_as_the_control(tmp_path):
    repo = _repo(tmp_path)
    result = _bound(repo)

    assert len(result.candidate_digest) == 64
    assert still_fresh(result, repo) is True


@pytest.mark.parametrize("transition", sorted(TRANSITIONS))
def test_a_verdict_goes_stale_when_the_candidate_changes_after_its_gate(tmp_path, transition):
    repo = _repo(tmp_path)
    result = _bound(repo)

    TRANSITIONS[transition](os.path.join(repo, "impl.py"))

    assert still_fresh(result, repo) is False
    assert candidate_digest(repo) != result.candidate_digest


def test_a_file_added_after_the_gate_makes_the_verdict_stale(tmp_path):
    repo = _repo(tmp_path)
    result = _bound(repo)

    _write(repo, "extra.py", "SNEAKED_IN = True\n")

    assert still_fresh(result, repo) is False


def test_a_failing_run_binds_nothing_and_an_unreadable_candidate_binds_empty(tmp_path):
    repo = _repo(tmp_path)
    assert run_gates(SPECS, repo, policy=FakePolicy(passed=False)).candidate_digest is None

    not_git = str(tmp_path / "plain")
    os.makedirs(not_git)
    unbindable = run_gates(SPECS, not_git, policy=FakePolicy(passed=True))
    assert unbindable.candidate_digest == ""
    assert still_fresh(unbindable, not_git) is False


# --------------------------------------------------------------------------- 1. ensemble.select_winner


def test_select_winner_takes_an_unchanged_bound_candidate_as_the_control(tmp_path):
    repo = _repo(tmp_path)
    attempt = Attempt(id="0", workspace=repo, evidence=_bound(repo), file_count=1)

    assert select_winner([attempt]) is attempt


@pytest.mark.parametrize("transition", sorted(TRANSITIONS))
def test_select_winner_never_selects_a_candidate_changed_after_its_gate(tmp_path, transition):
    stale_repo, fresh_repo = _repo(tmp_path, "stale"), _repo(tmp_path, "fresh")
    stale = Attempt(id="stale", workspace=stale_repo, evidence=_bound(stale_repo), file_count=1)
    fresh = Attempt(id="fresh", workspace=fresh_repo, evidence=_bound(fresh_repo), file_count=5)

    TRANSITIONS[transition](os.path.join(stale_repo, "impl.py"))

    assert select_winner([stale]) is None
    # The simpler attempt would win on file count; its stale pass is never what decides.
    assert select_winner([stale, fresh]) is fresh


def test_select_winner_never_selects_an_unbindable_pass(tmp_path):
    repo = _repo(tmp_path)
    evidence = GatesResult(records=_bound(repo).records, candidate_digest="")

    assert select_winner([Attempt(id="0", workspace=repo, evidence=evidence, file_count=1)]) is None


# --------------------------------------------------------------------------- 1. worktree.promote


def test_promote_copies_exactly_the_gated_bytes_as_the_control(tmp_path):
    repo = _repo(tmp_path)
    dest = str(tmp_path / "dest")
    os.makedirs(dest)
    result = _bound(repo)

    promoted = WorktreeManager(repo).promote(repo, dest, expected_digest=result.candidate_digest)

    assert promoted == ["impl.py"]
    with open(os.path.join(dest, "impl.py"), "rb") as handle:
        assert handle.read() == A


@pytest.mark.parametrize("transition", sorted(TRANSITIONS))
def test_promote_refuses_a_candidate_changed_after_its_gate(tmp_path, transition):
    repo = _repo(tmp_path)
    dest = str(tmp_path / "dest")
    os.makedirs(dest)
    result = _bound(repo)

    TRANSITIONS[transition](os.path.join(repo, "impl.py"))

    with pytest.raises(WorktreeError, match="changed after its gate passed"):
        WorktreeManager(repo).promote(repo, dest, expected_digest=result.candidate_digest)
    assert os.listdir(dest) == []


def test_promote_refuses_an_empty_binding(tmp_path):
    repo = _repo(tmp_path)
    dest = str(tmp_path / "dest")
    os.makedirs(dest)

    with pytest.raises(WorktreeError, match="changed after its gate passed"):
        WorktreeManager(repo).promote(repo, dest, expected_digest="")
    assert os.listdir(dest) == []


# --------------------------------------------------------------------------- 1. orchestrator pre-promote


def test_orchestrator_promotes_an_untouched_winner_as_the_control(tmp_path, monkeypatch):
    orch, outcome = _orchestrate(tmp_path, monkeypatch)

    assert outcome.state is TerminalState.SUCCESS
    assert len(orch.worktrees.promote_calls) == 1
    assert len(orch.worktrees.promote_calls[0]["expected_digest"]) == 64
    assert os.path.isfile(os.path.join(orch.workspace, "out.py"))


@pytest.mark.parametrize("transition", sorted(TRANSITIONS))
def test_orchestrator_never_promotes_a_winner_changed_between_gate_and_promote(tmp_path, monkeypatch,
                                                                              transition):
    orch, outcome = _orchestrate(tmp_path, monkeypatch, between_gate_and_promote=TRANSITIONS[transition])

    assert outcome.state is TerminalState.BLOCKED
    assert "changed after its gate passed" in outcome.reason
    assert orch.worktrees.promoted == []
    assert not os.path.exists(os.path.join(orch.workspace, "out.py"))
    assert _persisted_status(orch.workspace) == "blocked"
    assert PipelineCursor(orch.workspace).load()["completed_slices"] == []


# --------------------------------------------------------------------------- 3. launch failure after a proof


def _proven(repo, monkeypatch):
    """A real green -> red -> green proof on *repo*: the gate, then the three proof runs."""
    gate = _script(monkeypatch, [0, 0, 1, 0])
    evidence = run_gate("unit", GATE, workspace=repo, with_proof=True, policy=VerificationPolicy())
    assert evidence.passed is True and evidence.proof.proven is True
    assert gate.launches == 4
    return evidence


def _stash_list(repo):
    return subprocess.run(["git", "stash", "list"], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.mark.parametrize("failure", sorted(LAUNCH_FAILURES))
def test_a_gate_that_cannot_launch_after_a_valid_proof_ends_red(tmp_path, monkeypatch, failure):
    repo = _repo(tmp_path)
    _proven(repo, monkeypatch)
    error, message = LAUNCH_FAILURES[failure]
    gate = _script(monkeypatch, [error])

    rerun = run_gate("unit", GATE, workspace=repo, with_proof=True, policy=VerificationPolicy())

    assert gate.launches == 1  # the gate really re-ran; the earlier proof was not consulted
    assert rerun.passed is False
    assert rerun.proof is None
    assert rerun.evidence.startswith("error:") and message in rerun.evidence


@pytest.mark.parametrize("failure", sorted(LAUNCH_FAILURES))
def test_run_gates_and_select_winner_stay_red_after_a_launch_failure(tmp_path, monkeypatch, failure):
    repo = _repo(tmp_path)
    spec = GateSpec(name="unit", command=GATE, prove=True)
    _script(monkeypatch, [0, 0, 1, 0])
    earlier = run_gates([spec], repo, policy=VerificationPolicy())
    assert earlier.passed is True and earlier.records[0].proof.proven is True
    _script(monkeypatch, [LAUNCH_FAILURES[failure][0]])

    rerun = run_gates([spec], repo, policy=VerificationPolicy())

    assert rerun.passed is False
    assert rerun.candidate_digest is None
    assert rerun.records[0].proof is None
    assert select_winner([Attempt(id="rerun", workspace=repo, evidence=rerun, file_count=1)]) is None


@pytest.mark.parametrize("step", ["reverted", "restored"])
@pytest.mark.parametrize("failure", sorted(LAUNCH_FAILURES))
def test_a_proof_run_that_cannot_complete_is_never_counted_as_proof(tmp_path, monkeypatch, failure, step):
    # A timeout with the source reverted is not the gate turning red, and one after restoring is not green.
    repo = _repo(tmp_path)
    error = LAUNCH_FAILURES[failure][0]
    _script(monkeypatch, [0, 0, error, 0] if step == "reverted" else [0, 0, 1, error])

    evidence = run_gate("unit", GATE, workspace=repo, with_proof=True, policy=VerificationPolicy())

    assert evidence.passed is False
    assert evidence.proof.proven is False
    assert evidence.proof.aborted is False
    assert evidence.proof.error.startswith("gate could not run during the proof:")
    assert _stash_list(repo) == ""
    with open(os.path.join(repo, "impl.py"), "rb") as handle:
        assert handle.read() == A


@pytest.mark.parametrize("failure", ["timeout", "not-found"])
def test_the_post_promote_gate_failing_to_launch_ends_red_and_resume_stays_red(tmp_path, monkeypatch, failure):
    error, message = LAUNCH_FAILURES[failure]
    _script(monkeypatch, [error])
    orch, outcome = _orchestrate(tmp_path, monkeypatch, final_policy=VerificationPolicy)

    assert orch.worktrees.promoted, "the slice's own gate passed and it was promoted"
    assert outcome.state is TerminalState.BLOCKED
    assert message in outcome.reason
    assert _persisted_status(orch.workspace) == "blocked"

    # Resume the same run: the promoted slice is skipped, the final gate runs again and is still red.
    _, resumed = _orchestrate(tmp_path, monkeypatch, final_policy=VerificationPolicy, workspace=orch.workspace,
                              builder_responses=("not json",), worktrees=orch.worktrees)
    assert resumed.state is TerminalState.BLOCKED
    assert message in resumed.reason
    assert _persisted_status(orch.workspace) == "blocked"

    # Only a fresh green gate lifts it.
    _script(monkeypatch, [0])
    _, fresh = _orchestrate(tmp_path, monkeypatch, final_policy=VerificationPolicy, workspace=orch.workspace,
                            builder_responses=("not json",), worktrees=orch.worktrees)
    assert fresh.state is TerminalState.SUCCESS


# --------------------------------------------------------------------------- 2. torn and corrupt run state


def _success_cursor(ws):
    cursor = PipelineCursor(ws)
    cursor.start(run_id="r1", objective_goal="build a thing")
    cursor.complete_slice("core")
    cursor.set_stage("terminal", "success", {"outcome": {"state": "success", "reason": "all gates green",
                                                          "evidence": [{"gate": "unit", "passed": True}]}})
    with open(cursor.path, "rb") as handle:
        return cursor.path, handle.read()


def _seed_plan(ws):
    _write(ws, ".loop-engineer/RESEARCH.md", "# RESEARCH\n## Decisions\n- [VERIFIED] no external deps\n")
    _write(ws, ".loop-engineer/plan.md", "# Plan\n- [ ] Task 1: do the thing\n")


def _corruptions(raw):
    """Proper prefixes of *raw* (every 7th byte, and each of the last 16), byte damage, and JSON of the wrong shape.

    The last 16 cuts are the ones that come closest to a whole record; the sampled ones cover the rest.
    """
    cuts = sorted(set(range(0, len(raw), 7)) | set(range(max(0, len(raw) - 16), len(raw))))
    cases = [raw[:cut] for cut in cuts]
    for at in (0, len(raw) // 3, len(raw) // 2, len(raw) - 1):
        cases.append(raw[:at] + b"\xff" + raw[at + 1:])
    cases += [b"[]", b"null", b'"success"', b"42",
              json.dumps({"stage": "terminal", "status": "success", "completed_slices": "core"}).encode(),
              json.dumps({"stage": "terminal", "status": "success", "detail": []}).encode(),
              json.dumps({"stage": ["terminal"], "status": "success"}).encode()]
    return cases


def test_a_torn_or_corrupt_cursor_never_loads(tmp_path):
    path, raw = _success_cursor(str(tmp_path))
    assert PipelineCursor(str(tmp_path)).load()["status"] == "success"  # the control

    for damaged in _corruptions(raw):
        with open(path, "wb") as handle:
            handle.write(damaged)
        assert PipelineCursor(str(tmp_path)).load() is None, damaged[-40:]


def test_next_never_reads_a_torn_or_corrupt_cursor_as_success(tmp_path):
    ws = str(tmp_path)
    _seed_plan(ws)
    path, raw = _success_cursor(ws)
    assert emit_next(ws)["stage"] == "review"  # the control: an intact success cursor moves on

    damaged_cases = _corruptions(raw)
    for damaged in damaged_cases[::4] + damaged_cases[-12:]:
        with open(path, "wb") as handle:
            handle.write(damaged)
        envelope = emit_next(ws)
        assert envelope["next_action"]["type"] == "run_engine", damaged[-40:]
        assert envelope["stage"] == "engine"


@pytest.mark.parametrize("damage", ["truncated-early", "truncated-late", "bad-byte", "wrong-shape"])
def test_a_resume_on_a_torn_cursor_redoes_every_slice(tmp_path, monkeypatch, damage):
    monkeypatch.setattr(orchestrator, "run_gates", _pass_gates)
    workspace = str(tmp_path / "ws")
    os.makedirs(workspace)
    seed = PipelineCursor(workspace)
    seed.start(run_id="r1", objective_goal="build a thing")
    seed.complete_slice("core")
    with open(seed.path, "rb") as handle:
        raw = handle.read()
    damaged = {"truncated-early": raw[: len(raw) // 3], "truncated-late": raw[:-1],
               "bad-byte": raw[:5] + b"\xff" + raw[6:],
               "wrong-shape": json.dumps({"run_id": "r1", "objective_goal": "build a thing",
                                          "completed_slices": "core"}).encode()}[damage]
    with open(seed.path, "wb") as handle:
        handle.write(damaged)

    builder = Builder(FakeProvider([TWO_SLICE_JSON, FILE_BLOCK, FILE_BLOCK]))
    orch = _make(tmp_path, builder=builder, reviewer=Reviewer(FakeProvider(["NO FINDINGS", "NO FINDINGS"])),
                 objective=Objective(goal="build a thing", done_when="pytest passes"),
                 cursor=PipelineCursor(workspace), workspace=workspace)
    outcome = orch.run()

    prompts = [user for (_system, user) in builder.provider.calls]
    assert any("core objective oc" in p for p in prompts), "the torn record must not skip the core slice"
    assert any("api objective oa" in p for p in prompts)
    assert outcome.state is TerminalState.SUCCESS
    assert PipelineCursor(workspace).load()["completed_slices"] == ["core", "api"]


class _Killed(BaseException):
    """The process dies here: nothing after this point runs, and no ``except Exception`` catches it."""


def _die(*_args, **_kwargs):
    raise _Killed()


def _cursor_writer(ws):
    cursor = PipelineCursor(ws)
    cursor.start(run_id="r1", objective_goal="g")
    cursor.set_stage("build", "running")
    return cursor.path, lambda: cursor.set_stage("terminal", "success", {"outcome": {"state": "success"}})


def _state_writer(ws):
    state = RunState(ws, {"goal": "g", "done_when": "d", "builder": "b"})
    state.record_round({"round_no": 1, "files_written": ["a.py"], "verified": False, "decision": "retry"})
    return state.state_md, lambda: state.record_round({"round_no": 2, "files_written": ["a.py"],
                                                      "verified": True, "decision": "done"})


def _review_writer(ws):
    path = write_slice_review(ws, "core", [], reviewer="r", skipped_reason="review did not run")
    return path, lambda: write_slice_review(ws, "core", ["FINDING: x"], reviewer="r")


def _ledger_writer(ws):
    _save_ledger(ws, {"stage": "engine", "attempts": 1, "skipped": [], "research_rounds": 0})
    return (os.path.join(ws, ".loop-engineer", "next.json"),
            lambda: _save_ledger(ws, {"stage": "review", "attempts": 2, "skipped": ["qa"], "research_rounds": 0}))


WRITERS = {"pipeline-cursor": _cursor_writer, "state-md": _state_writer, "review-record": _review_writer,
           "next-ledger": _ledger_writer}


@pytest.mark.parametrize("kill_at", ["fsync", "replace"])
@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_a_writer_killed_mid_write_leaves_the_previous_record_whole(tmp_path, monkeypatch, writer, kill_at):
    ws = str(tmp_path)
    path, write_next = WRITERS[writer](ws)
    with open(path, "rb") as handle:
        before = handle.read()

    with monkeypatch.context() as patch:
        patch.setattr(os, kill_at, _die)
        with pytest.raises(_Killed):
            write_next()

    with open(path, "rb") as handle:
        assert handle.read() == before
    if writer == "pipeline-cursor":
        assert PipelineCursor(ws).load()["stage"] == "build"
    elif writer == "next-ledger":
        assert _load_ledger(ws)["stage"] == "engine"
    elif writer == "review-record":
        assert json.loads(before)["skipped_reason"] == "review did not run"
    write_next()  # once the process lives, the same write lands whole
    with open(path, "rb") as handle:
        assert handle.read() != before


def test_a_real_process_killed_while_writing_the_cursor_never_leaves_success(tmp_path):
    ws = str(tmp_path / "ws")
    script = ("import os, sys\n"
              f"sys.path.insert(0, {SRC!r})\n"
              "from kickass_loop_engineer.cursor import PipelineCursor\n"
              f"cursor = PipelineCursor({ws!r})\n"
              "cursor.start(run_id='r1', objective_goal='g')\n"
              "os.fsync = lambda fd: os._exit(137)\n"
              "cursor.set_stage('terminal', 'success', {'outcome': {'state': 'success'}})\n")
    completed = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True,
                               timeout=60, check=False)

    assert completed.returncode == 137, completed.stderr
    loaded = PipelineCursor(ws).load()
    assert (loaded["stage"], loaded["status"]) == ("start", "started")


def test_a_torn_journal_line_never_swallows_the_next_event(tmp_path):
    ws = str(tmp_path)
    cursor = PipelineCursor(ws)
    cursor.start(run_id="r1", objective_goal="g")
    cursor.set_stage("terminal", "success")
    with open(cursor.events_path, "rb") as handle:
        raw = handle.read()
    with open(cursor.events_path, "wb") as handle:
        handle.write(raw[:-10])  # killed partway through the terminal event

    cursor.emit("attempt_started", {"attempt": 0})

    with open(cursor.events_path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    parsed = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            parsed.append(None)
    assert parsed[-1]["event"] == "attempt_started"
    assert parsed[-2] is None  # the torn line stays one unreadable line
    assert not any(p and p["event"] == "stage" and p["payload"].get("status") == "success" for p in parsed)


def test_a_torn_round_record_never_reads_as_verified(tmp_path):
    ws = str(tmp_path)
    state = RunState(ws, {"goal": "g"})
    state.record_round({"round_no": 1, "verified": True, "decision": "done"})
    with open(state.rounds_path, "rb") as handle:
        raw = handle.read()
    with open(state.rounds_path, "wb") as handle:
        handle.write(raw[:-5])

    resumed = RunState(ws, {"goal": "g"})
    assert resumed._rounds == []
    resumed.record_round({"round_no": 2, "verified": False, "decision": "retry"})
    assert [r["round_no"] for r in RunState(ws, {"goal": "g"})._rounds] == [2]


@pytest.mark.parametrize("damage", [b"", b"{\"stage\": \"rev", b"[]", b"\xff\xfe", b"null"])
def test_a_torn_or_corrupt_next_ledger_starts_fresh(tmp_path, damage):
    ws = str(tmp_path)
    _write(ws, ".loop-engineer/next.json", damage)

    assert _load_ledger(ws) == {"stage": "", "attempts": 0, "skipped": [], "research_rounds": 0}
    assert emit_next(ws)["next_action"]["type"] != "terminal"
