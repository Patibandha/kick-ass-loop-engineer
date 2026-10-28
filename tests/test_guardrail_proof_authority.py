"""Proof-authority tests: can code the builder controls, running as the gate, forge what the engine trusts?

The attacker is a test module in the candidate: the gate runs it, so it runs with the gate's full authority.
Each attack goes through the real path in a temporary git repo: a real ``WorktreeManager`` worktree, a real
pytest gate under ``run_gates`` with proof-of-test, ``select_winner`` and ``promote``. Nothing is scripted.

Attacks (one row each in docs/acceptance/proof-authority.md, which every test checks against):
    A1  in the proof's reverted run, write the reverted source back so the gate stays green;
    A2  in the proof's reverted run, fail on purpose so ``red_when_reverted`` is forged for wrong code;
    B   write a success cursor into the main workspace's ``.loop-engineer/pipeline.json``;
    C   leave a background process that rewrites the candidate after its digest is taken;
    D   write outside the worktree (the main workspace and an engine-source stand-in).

An ACCEPTED attack is a recorded critical finding (PA-1, PA-2 in decisions.md); its test asserts today's
outcome so the result is visible, not hidden. The fix belongs to its own scoped project.
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from kickass_loop_engineer.cursor import PipelineCursor
from kickass_loop_engineer.ensemble import Attempt, select_winner
from kickass_loop_engineer.gates import GateSpec, default_python
from kickass_loop_engineer.next import emit_next
from kickass_loop_engineer.verifier import run_gates
from kickass_loop_engineer.worktree import WorktreeError, WorktreeManager

pytestmark = pytest.mark.guardrail_proof_authority

GATE = f"{default_python()} -m pytest -q"
SPECS = [GateSpec(name="unit", command=GATE, prove=True)]
DOC = Path(__file__).resolve().parent.parent / "docs" / "acceptance" / "proof-authority.md"
DECISIONS = DOC.parent / "decisions.md"
ATTACKS = {"A1", "A2", "B", "C", "D"}
OUTCOMES = {"REFUSED", "ACCEPTED"}

RIGHT = "def answer():\n    return 42\n"
WRONG = "def answer():\n    return 41\n"

#: Attacker preamble: the main workspace, found the way any gate process can, from the worktree's ``.git`` file.
FIND_MAIN = '''
import os


def main_workspace():
    with open(".git", encoding="utf-8") as handle:
        gitdir = handle.read().split("gitdir:", 1)[1].strip()
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(gitdir))))
'''


# --------------------------------------------------------------------------- helpers


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return path


def _attempt(tmp_path, impl, attacker):
    """A main repo plus one real attempt worktree holding the candidate ``impl.py`` and the attacker test."""
    main = str(tmp_path / "main")
    os.makedirs(main)
    _git(main, "init", "-q")
    _git(main, "config", "core.autocrlf", "false")
    _write(main, ".gitignore", "__pycache__/\n*.pyc\n.pytest_cache/\n.loop-engineer/\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "baseline")
    manager = WorktreeManager(main, base_dir=str(tmp_path / "worktrees"))
    worktree = manager.create("attempt")
    _write(worktree, "impl.py", impl)
    _write(worktree, "test_attack.py", attacker)
    return main, manager, worktree


def _select_and_promote(manager, worktree, result, dest):
    """Run the engine's selection and promotion on one gated attempt; return (winner, promoted or None)."""
    winner = select_winner([Attempt(id="0", workspace=worktree, evidence=result, file_count=2)])
    if winner is None:
        return None, None
    os.makedirs(dest, exist_ok=True)
    try:
        return winner, manager.promote(worktree, dest, expected_digest=result.candidate_digest)
    except WorktreeError:
        return winner, None


def _recorded(attack):
    """The outcome docs/acceptance/proof-authority.md records for *attack* (raises when the doc is missing)."""
    rows = {}
    for line in DOC.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) >= 3 and cells[1] in OUTCOMES:
            rows[cells[0]] = cells[1]
    return rows.get(attack)


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# --------------------------------------------------------------------------- A1: write the source back


def test_a1_writing_the_reverted_source_back_during_the_proof_is_refused(tmp_path):
    attacker = f'''
import os


def test_answer():
    if not os.path.exists("impl.py"):  # the proof's reverted run: put the source back so the gate stays green
        with open("impl.py", "w") as handle:
            handle.write({RIGHT!r})
    from impl import answer
    assert answer() == 42
'''
    _main, manager, worktree = _attempt(tmp_path, RIGHT, attacker)

    result = run_gates(SPECS, worktree)
    winner, promoted = _select_and_promote(manager, worktree, result, str(tmp_path / "dest"))

    proof = result.records[0].proof
    assert proof is not None and proof.proven is False
    assert proof.red_when_reverted is False
    assert result.passed is False and winner is None and promoted is None
    outcome = "REFUSED"
    assert _recorded("A1") == outcome


# --------------------------------------------------------------------------- A2: forge red_when_reverted


def test_a2_a_gate_that_fails_only_when_the_source_is_absent_proves_wrong_code(tmp_path):
    # CRITICAL FINDING PA-1 (docs/acceptance/proof-authority.md, decisions.md): this asserts today's outcome.
    attacker = '''
import os


def test_answer():
    assert os.path.exists("impl.py")  # red only in the proof's reverted run; never calls the code
'''
    _main, manager, worktree = _attempt(tmp_path, WRONG, attacker)

    result = run_gates(SPECS, worktree)
    winner, promoted = _select_and_promote(manager, worktree, result, str(tmp_path / "dest"))

    proof = result.records[0].proof
    assert proof.proven is True and proof.red_when_reverted is True
    assert winner is not None and promoted == ["impl.py", "test_attack.py"]
    assert _read(str(tmp_path / "dest" / "impl.py")) == WRONG  # the wrong answer reached the workspace
    outcome = "ACCEPTED"
    assert _recorded("A2") == outcome


# --------------------------------------------------------------------------- B: forge run state


def _forged_cursor(tmp_path):
    scratch = str(tmp_path / "scratch")
    cursor = PipelineCursor(scratch)
    cursor.start(run_id="forged", objective_goal="anything")
    cursor.set_stage("terminal", "success", {"outcome": {"state": "success", "reason": "all gates green",
                                                          "evidence": [{"gate": "unit", "passed": True}]}})
    return _read(cursor.path)


def test_b_a_success_cursor_written_by_the_gate_into_the_main_workspace_is_trusted(tmp_path):
    # CRITICAL FINDING PA-2 (docs/acceptance/proof-authority.md, decisions.md): this asserts today's outcome.
    forged = _forged_cursor(tmp_path)
    attacker = FIND_MAIN + f'''

def test_answer():
    state = os.path.join(main_workspace(), ".loop-engineer")
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, "pipeline.json"), "w", encoding="utf-8") as handle:
        handle.write({forged!r})
    from impl import answer
    assert answer() == 42
'''
    main, _manager, worktree = _attempt(tmp_path, RIGHT, attacker)
    _write(main, ".loop-engineer/RESEARCH.md", "# RESEARCH\n## Decisions\n- [VERIFIED] no external deps\n")
    _write(main, ".loop-engineer/plan.md", "# Plan\n- [ ] Task 1: do the thing\n")
    assert emit_next(main)["stage"] != "review"  # the control: no cursor, the engine has not run

    run_gates(SPECS, worktree)

    assert json.loads(_read(os.path.join(main, ".loop-engineer", "pipeline.json")))["status"] == "success"
    assert PipelineCursor(main).load()["status"] == "success"
    assert emit_next(main)["stage"] == "review"  # the engine moves on as though the run succeeded
    outcome = "ACCEPTED"
    assert _recorded("B") == outcome


# --------------------------------------------------------------------------- C: change after the digest


BACKGROUND = '''
import os, sys, time
control, target, wrong = sys.argv[1], sys.argv[2], sys.argv[3]
deadline = time.time() + 120
while not os.path.exists(os.path.join(control, "go")) and time.time() < deadline:
    time.sleep(0.05)
with open(target, "w") as handle:
    handle.write(wrong)
open(os.path.join(control, "done"), "w").close()
'''


def test_c_a_background_process_changing_the_candidate_after_its_digest_is_refused(tmp_path):
    control = str(tmp_path / "control")
    os.makedirs(control)
    attacker = f'''
import os
import subprocess
import sys

CONTROL = {control!r}


def test_answer():
    from impl import answer
    assert answer() == 42
    spawned = os.path.join(CONTROL, "spawned")
    if not os.path.exists(spawned):
        open(spawned, "w").close()
        subprocess.Popen([sys.executable, "-c", {BACKGROUND!r}, CONTROL, os.path.abspath("impl.py"), {WRONG!r}],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
'''
    _main, manager, worktree = _attempt(tmp_path, RIGHT, attacker)

    result = run_gates(SPECS, worktree)
    assert result.passed is True and result.records[0].proof.proven is True
    assert select_winner([Attempt(id="0", workspace=worktree, evidence=result)]) is not None  # the control

    open(os.path.join(control, "go"), "w").close()  # the background process left by the gate now strikes
    deadline = time.time() + 60
    while not os.path.exists(os.path.join(control, "done")) and time.time() < deadline:
        time.sleep(0.05)
    assert _read(os.path.join(worktree, "impl.py")) == WRONG

    winner, promoted = _select_and_promote(manager, worktree, result, str(tmp_path / "dest"))
    assert winner is None and promoted is None
    with pytest.raises(WorktreeError, match="changed after its gate passed"):
        manager.promote(worktree, str(tmp_path / "dest"), expected_digest=result.candidate_digest)
    assert not os.path.exists(str(tmp_path / "dest" / "impl.py"))
    outcome = "REFUSED"
    assert _recorded("C") == outcome


# --------------------------------------------------------------------------- D: write outside the worktree


def test_d_a_gate_writes_into_the_main_workspace_and_the_engine_source(tmp_path):
    # CRITICAL FINDING PA-2 (docs/acceptance/proof-authority.md, decisions.md): this asserts today's outcome.
    # The engine source is a stand-in folder: the gate runs as the engine's own user with no write confinement,
    # so the real one is reachable the same way, but this test never writes to it.
    engine = str(tmp_path / "engine-src" / "kickass_loop_engineer")
    os.makedirs(engine)
    attacker = FIND_MAIN + f'''

def test_answer():
    with open(os.path.join(main_workspace(), "planted.py"), "w") as handle:
        handle.write("PLANTED = True\\n")
    with open(os.path.join({engine!r}, "verifier.py"), "w") as handle:
        handle.write("def prove(*args, **kwargs):\\n    return True\\n")
    from impl import answer
    assert answer() == 42
'''
    main, manager, worktree = _attempt(tmp_path, RIGHT, attacker)

    result = run_gates(SPECS, worktree)
    winner, promoted = _select_and_promote(manager, worktree, result, str(tmp_path / "dest"))

    assert result.passed is True and winner is not None and promoted == ["impl.py", "test_attack.py"]
    assert _read(os.path.join(main, "planted.py")) == "PLANTED = True\n"
    assert "return True" in _read(os.path.join(engine, "verifier.py"))
    outcome = "ACCEPTED"
    assert _recorded("D") == outcome


# --------------------------------------------------------------------------- the recorded table


def test_the_doc_records_every_attack_once_and_names_the_code_that_decides_it():
    rows = [line for line in DOC.read_text(encoding="utf-8").splitlines()
            if line.startswith("|") and re.match(r"\|\s*(A1|A2|B|C|D)\s*\|", line)]
    ids = [line.strip("|").split("|")[0].strip() for line in rows]
    assert sorted(ids) == sorted(ATTACKS)
    for line in rows:
        assert re.search(r"src/kickass_loop_engineer/\w+\.py::\w+", line), line


def test_every_accepted_attack_is_a_critical_finding_in_both_docs():
    doc, decisions = DOC.read_text(encoding="utf-8"), DECISIONS.read_text(encoding="utf-8")
    accepted = sorted(attack for attack in ATTACKS if _recorded(attack) == "ACCEPTED")
    assert accepted == ["A2", "B", "D"]
    for finding in ("PA-1", "PA-2"):
        assert f"CRITICAL FINDING {finding}" in doc
        assert f"CRITICAL FINDING {finding}" in decisions
