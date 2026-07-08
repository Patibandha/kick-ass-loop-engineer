"""Pipeline orchestrator: every 1.0 module wired into one verified loop.

refine(assess, 1.0 gate — done upstream by `refine` CLI) → decompose →
per slice (toposorted): escalation → ensemble N in worktrees → gates+proof per
attempt → select_winner → cross-model review (findings only) → promote →
memory.record(contract) → final proof gate → RunOutcome + playbook/auditor →
memory.consolidate → notify(terminal).

Approval comes from gates. The reviewer contributes findings (fed to
oscillation and the next slice's feedback), never a verdict.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from .auditor import classify_run
from .autonomy import AutonomyLevel, ReadinessChecklist, resolve_level
from .decompose import generate_slices
from .ensemble import run_ensemble, select_winner
from .escalation import should_escalate
from .notify import build_event
from .objective import Objective
from .oscillation import OscillationDetector
from .providers.base import ProviderError
from .session import BuildSession
from .terminal import RunOutcome, TerminalState
from .verifier import run_gate
from .workspace import Workspace
from .worktree import WorktreeError


class _BudgetStop(Exception):
    """Raised inside an ensemble build when the run cost cap is reached.

    Caught in :meth:`Orchestrator.run` and turned into a ``BUDGET_EXCEEDED``
    outcome so the cap stops the run at a clean terminal state.
    """


def _is_git_repo(workspace: str) -> bool:
    """Return True iff *workspace* is inside a git work tree.

    Uses ``git -C <workspace> rev-parse --is-inside-work-tree`` and treats any
    failure (non-zero exit, missing git, timeout, OS error) as "not a repo".
    """
    try:
        result = subprocess.run(
            ["git", "-C", workspace, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@dataclass
class GateSpec:
    """One executable gate: a registry name plus the concrete command.

    Attributes:
        name: A gate name registered in ``gates.GATES`` (e.g. ``"unit"``).
        command: The allowlisted command the gate runs.
    """

    name: str = "unit"
    command: str = "python3 -m pytest -q"


class Orchestrator:
    """Drives the full verified pipeline for one objective to a single outcome.

    The orchestrator wires every 1.0 module together: it decomposes the
    objective into role slices, builds an ensemble of attempts per slice in
    isolated worktrees, gates each attempt (optionally with proof-of-test),
    selects the verified winner, collects reviewer findings (never a verdict),
    promotes the winner, and finishes with a final gate plus governance
    (playbook, auditor, memory, notify). Every terminal path funnels through
    :meth:`_finish`.

    All dependencies are keyword-only and required; ``config.build_orchestrator``
    (a later task) supplies concrete instances.
    """

    def __init__(self, *, objective, workspace, builder, reviewer, worktrees,
                 ledger, notifier, memory, playbook, cursor, gate: GateSpec,
                 ensemble_n: int = 2, run_id: str = "",
                 autonomy_requested=AutonomyLevel.L3, oscillation_window: int = 2,
                 prove: bool = True) -> None:
        """Wire the pipeline dependencies.

        Args:
            objective: The run ``Objective`` (goal, done_when, constraints).
            workspace: Path to the main git workspace the winner promotes into.
            builder: Provider-backed ``agents.Builder`` used for decomposition
                and every ensemble attempt.
            reviewer: A ``Reviewer``/``CrossModelReviewer`` exposing
                ``review(objective, snapshot) -> Review`` (findings only).
            worktrees: A ``WorktreeManager`` providing ``create``/``promote``/
                ``remove``.
            ledger: A ``CostLedger`` enforcing the hard per-run cap.
            notifier: A ``Notifier`` receiving ``build_event`` payloads.
            memory: A ``Memory`` store for contracts and the verifier-proven fact.
            playbook: A ``Playbook`` recording this objective's attempt outcome.
            cursor: A ``PipelineCursor`` persisting resumable stage state.
            gate: The ``GateSpec`` (name + command) run per attempt and finally.
            ensemble_n: Number of attempts to build per slice.
            run_id: Stable identifier for the run (used for resume and memory).
            autonomy_requested: Requested ``AutonomyLevel`` (resolved against a
                readiness checklist; M1 records the level and proceeds — behavioral
                L2 checkpoints arrive with the M2 ``next`` protocol).
            oscillation_window: Consecutive identical-finding rounds that mean
                the loop is stuck.
            prove: When True, gates run with proof-of-test (revert → red → green).
        """
        self.objective = objective
        self.workspace = workspace
        self.builder = builder
        self.reviewer = reviewer
        self.worktrees = worktrees
        self.ledger = ledger
        self.notifier = notifier
        self.memory = memory
        self.playbook = playbook
        self.cursor = cursor
        self.gate = gate
        self.ensemble_n = ensemble_n
        self.run_id = run_id
        self.autonomy_requested = autonomy_requested
        self.oscillation = OscillationDetector(window=oscillation_window)
        self.prove = prove

        self._feedback = ""
        self._slice_evidence: list = []  # winner EvidenceRecord per completed slice
        self._total_slices = 0
        self._completed_slices = 0
        self._total_attempts = 0
        self._losing_attempts = 0

    def run(self) -> RunOutcome:
        """Execute the pipeline and return exactly one ``RunOutcome``.

        Returns:
            The terminal ``RunOutcome``; ``SUCCESS`` always carries evidence.
        """
        skip = self._begin_run()

        slices = generate_slices(self.objective, self.builder)
        # The one generate_slices provider call is deliberately not ledger-recorded
        # — $0 on local models; wire per-call accounting in M2.
        self._total_slices = len(slices)
        self.cursor.set_stage("decompose", "done", {"slices": [s.role for s in slices]})

        try:
            outcome = self._process_slices(slices, skip)
        except _BudgetStop as stop:
            outcome = RunOutcome(TerminalState.BUDGET_EXCEEDED, str(stop))
        return self._finish(outcome)

    def _begin_run(self) -> set:
        """Start or resume the cursor, then record the resolved autonomy level.

        Returns:
            The set of slice roles to skip (already completed on a resumed run).
        """
        skip: set = set()
        existing = self.cursor.load()
        if (existing and existing.get("run_id") == self.run_id
                and existing.get("objective_goal") == self.objective.goal):
            # Resume: keep the persisted cursor (do NOT restart) and skip done slices.
            skip = set(existing.get("completed_slices", []))
        else:
            self.cursor.start(self.run_id, self.objective.goal)

        checklist = ReadinessChecklist(
            verifier_proven=bool(self.memory.get("verifier.proven")),
            budget_set=self.ledger is not None,
            denylist_active=True,
            workspace_isolated=_is_git_repo(self.workspace),
        )
        effective = resolve_level(self.autonomy_requested, checklist)
        # M1 records the level and proceeds; behavioral L2 checkpoints arrive with
        # the M2 `next` protocol.
        self.cursor.set_stage("autonomy", effective.value, {"gaps": checklist.gaps()})
        return skip

    def _process_slices(self, slices: list, skip: set) -> RunOutcome:
        """Run every slice in order; return early on any non-success terminal.

        Args:
            slices: Topologically ordered ``RoleSlice`` list.
            skip: Roles already completed on a resumed run.

        Returns:
            A ``RunOutcome`` — an early terminal for a failing slice, or the
            final-gate outcome once every slice promotes.
        """
        for slice_ in slices:
            if slice_.role in skip:
                self._completed_slices += 1
                continue

            outcome = self._run_slice(slice_)
            if outcome is not None:
                return outcome

        final = run_gate(self.gate.name, self.gate.command, self.workspace,
                         with_proof=self.prove)
        if not final.passed:
            return RunOutcome(TerminalState.BLOCKED, final.evidence)
        evidence = [final.to_dict()] + [e.to_dict() for e in self._slice_evidence]
        return RunOutcome(TerminalState.SUCCESS, reason="all gates green", evidence=evidence)

    def _run_slice(self, slice_) -> RunOutcome | None:
        """Build, gate, review, and promote one slice.

        Returns:
            ``None`` when the slice promoted cleanly (continue to the next), or a
            terminal ``RunOutcome`` (escalation, stall, oscillation, or a
            provider/promotion failure mapped to ``BLOCKED``) that ends the run.
        """
        # 4. Pre-build escalation on the slice's planned work.
        escalate, reason = should_escalate(action=slice_.objective)
        if escalate:
            self.notifier.notify(build_event(
                self.run_id, "escalation", "escalation_required", "escalate",
                {"slice": slice_.role, "reason": reason}))
            return RunOutcome(TerminalState.APPROVAL_REQUIRED, reason)

        # 5. Ensemble: N attempts in isolated worktrees, each gated.
        slice_objective = Objective(goal=slice_.objective,
                                    done_when=self.objective.done_when,
                                    constraints=self.objective.constraints)
        feedback = self._feedback
        attempt_files: dict = {}  # worktree path -> files_written this attempt
        created: list = []  # every worktree created for this slice (crash cleanup)

        def build_fn(i):
            # Budget check FIRST: stop before the NEXT attempt once the cap is
            # reached (the overshoot-by-one is accepted for $0 local models;
            # per-call accounting for paid providers arrives in M2).
            if self.ledger.run_total() >= self.ledger.run_cap_usd:
                raise _BudgetStop(
                    f"run cost ${self.ledger.run_total()} reached cap "
                    f"${self.ledger.run_cap_usd}")
            self._total_attempts += 1
            wt_path = self.worktrees.create(f"{slice_.role}-{i}")
            created.append(wt_path)
            workspace = Workspace(root=wt_path)  # BuildSession takes a Workspace object
            round_ = BuildSession(builder=self.builder, workspace=workspace,
                                  objective=slice_objective).build_round(1, feedback)
            self.ledger.record(round_.cost_usd)
            attempt_files[wt_path] = list(round_.files_written)
            return (wt_path, len(round_.files_written))

        def gate_fn(wt):
            return run_gate(self.gate.name, self.gate.command, wt, with_proof=self.prove)

        try:
            attempts = run_ensemble(self.ensemble_n, build_fn, gate_fn)

            # 7. No verified winner: nothing passed its gate.
            winner = select_winner(attempts)
            if winner is None:
                self._losing_attempts += len(attempts)
                self._remove_attempts(attempts)
                return RunOutcome(
                    TerminalState.STALLED,
                    f"no attempt passed its gate for slice '{slice_.role}'")

            self._losing_attempts += len(attempts) - 1
            winner_files = attempt_files.get(winner.workspace, [])

            # 6. Post-build escalation on the winner's actual footprint.
            escalate, reason = should_escalate(paths=winner_files,
                                               files_touched=winner.file_count)
            if escalate:
                self._remove_attempts(attempts)
                self.notifier.notify(build_event(
                    self.run_id, "escalation", "escalation_required", "escalate",
                    {"slice": slice_.role, "reason": reason}))
                return RunOutcome(TerminalState.APPROVAL_REQUIRED, reason)

            # 8. Review (findings only) → oscillation + next-slice feedback.
            snapshot = self._snapshot(winner.workspace, winner_files)
            review = self.reviewer.review(slice_objective, snapshot)
            if self.oscillation.observe(review.findings):
                self._remove_attempts(attempts)
                return RunOutcome(
                    TerminalState.OSCILLATION,
                    f"repeating findings for slice '{slice_.role}'")
            self._feedback = "\n".join(review.findings) if review.findings else ""
        except ProviderError as exc:
            # Provider blips (local Ollama restarts, network) are routine: end
            # in a clean terminal outcome instead of escaping run(), and never
            # leak this slice's worktrees.
            self._cleanup(created)
            return RunOutcome(TerminalState.BLOCKED, f"provider failure: {exc}")
        except _BudgetStop:
            # The budget outcome is produced in run(); clean this slice's
            # worktrees here where they are still known.
            self._cleanup(created)
            raise

        # 9. Promote the winner; clean up every attempt worktree either way.
        try:
            self.worktrees.promote(winner.workspace, self.workspace)
        except WorktreeError as exc:
            return RunOutcome(TerminalState.BLOCKED, f"promotion failed: {exc}")
        finally:
            self._remove_attempts(attempts)
        self.memory.record(f"contract.{slice_.role}",
                           slice_.contract or slice_.objective,
                           run_id=self.run_id, kind="contract")
        self.cursor.complete_slice(slice_.role)
        self.notifier.notify(build_event(
            self.run_id, "slice", "slice_completed", "info", {"slice": slice_.role}))
        self._completed_slices += 1
        self._slice_evidence.append(winner.evidence)
        return None

    def _snapshot(self, worktree_path: str, files: list) -> str:
        """Build the review snapshot from the winner's changed files.

        A fresh ``Workspace`` has an empty ``written`` set, so the winner's files
        are read directly from its worktree and formatted for the reviewer.

        Args:
            worktree_path: The winning attempt's worktree root.
            files: Workspace-relative paths the winner wrote.

        Returns:
            ``"--- {rel} ---\\n{content}"`` blocks joined by blank lines.
        """
        parts: list = []
        for rel in files:
            path = os.path.join(worktree_path, rel)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    content = handle.read()
            except OSError as exc:
                content = f"(unreadable: {exc})"
            parts.append(f"--- {rel} ---\n{content}")
        return "\n\n".join(parts)

    def _remove_attempts(self, attempts: list) -> None:
        """Remove every attempt's worktree (winner and losers alike)."""
        for attempt in attempts:
            self.worktrees.remove(attempt.workspace)

    def _cleanup(self, worktree_paths: list) -> None:
        """Remove worktrees created for a slice that crashed mid-flight."""
        for path in worktree_paths:
            self.worktrees.remove(path)

    def _finish(self, outcome: RunOutcome) -> RunOutcome:
        """Record governance for *outcome* and return it unchanged.

        Every terminal path funnels through here: playbook attempt, auditor
        verdict, verifier-proven fact + memory consolidation on success, the
        terminal cursor stage, and the terminal notification.

        Args:
            outcome: The terminal outcome the pipeline reached.

        Returns:
            The same ``outcome`` object.
        """
        success = outcome.state is TerminalState.SUCCESS
        self.playbook.record_attempt(self.objective.goal, success=success)

        hit_rate = (self._completed_slices / self._total_slices
                    if self._total_slices else 0.0)
        waste_ratio = self._losing_attempts / max(self._total_attempts, 1)
        verdict = classify_run(hit_rate, waste_ratio)

        if success:
            self.memory.record("verifier.proven", "true", run_id=self.run_id, kind="fact")
            self.memory.consolidate(self.run_id)

        self.cursor.set_stage("terminal", outcome.state.value,
                              {"auditor": verdict, "outcome": outcome.to_dict()})
        level = ("escalate" if outcome.state in (TerminalState.APPROVAL_REQUIRED,
                                                 TerminalState.BUDGET_EXCEEDED)
                 else "info")
        self.notifier.notify(build_event(
            self.run_id, "terminal", outcome.state.value, level, outcome.to_dict()))
        return outcome
