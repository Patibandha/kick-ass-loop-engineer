"""Pipeline orchestrator: every 1.0 module wired into one verified loop.

refine(assess, 1.0 gate — done upstream by `refine` CLI) → decompose →
architect (smart-triggered: 2+ slices → validated design.md, re-decompose
against it, conformance gate when layering is declared) → per slice
(toposorted): escalation → ensemble N in worktrees → gates+proof per
attempt → select_winner → cross-model review (findings only) → promote
(+ archmap regen) → memory.record(contract) → final run of the full gate
list → RunOutcome + playbook/auditor → memory.consolidate → notify(terminal).

Approval comes from gates. The reviewer contributes findings (fed to
oscillation and the next slice's feedback), never a verdict.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections import deque

from .architect import Design, generate_design, render_importlinter
from .archmap import write_archmap
from .audit import AUDIT_SELECT_BUDGET_BYTES, fallback_files, run_audit
from .auditor import classify_run
from .autonomy import AutonomyLevel, ReadinessChecklist, resolve_level
from .decompose import generate_slices
from .ensemble import run_ensemble, select_winner
from .escalation import should_escalate
from .gates import GateSpec
from .modes import detect_mode
from .notify import build_event
from .objective import Objective
from .oscillation import OscillationDetector
from .pricing import estimate_cost
from .providers.base import ProviderError
from .repomap import assemble_context, repo_map, select_files
from .reproduce import FIX_MODE_RULES, generate_repro, repro_check_command
from .review import CrossModelReviewError
from .session import BuildSession
from .terminal import RunOutcome, TerminalState
from .verifier import OBSERVER_CAP, EvidenceRecord, GatesResult, run_gates
from .visual import visual_critique
from .workspace import Workspace
from .worktree import WorktreeError

logger = logging.getLogger("kickass_loop_engineer.orchestrator")

#: Bounded next-slice feedback history (the oscillation-deque precedent): a
#: fresh review resets it; each stall APPENDS its observations, so repeated
#: stalls accumulate context without growing without bound.
_FEEDBACK_MAX_ENTRIES = 3


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


def _detect_python_package(workspace: str) -> str | None:
    """Return the workspace's top-level Python package name, if any.

    Looks for the first top-level directory carrying an ``__init__.py``
    (alphabetical, hidden directories skipped), then falls back to the
    ``src/<pkg>`` layout.
    """
    for base in (workspace, os.path.join(workspace, "src")):
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue
        for entry in entries:
            if entry.startswith(".") or entry == "src":
                continue
            if os.path.isfile(os.path.join(base, entry, "__init__.py")):
                return entry
    return None


class Orchestrator:
    """Drives the full verified pipeline for one objective to a single outcome.

    The orchestrator wires every 1.0 module together: it decomposes the
    objective into role slices, builds an ensemble of attempts per slice in
    isolated worktrees, runs the full gate list against each attempt
    (fail-fast, per-gate proof-of-test), selects the verified winner, collects
    reviewer findings (never a verdict), promotes the winner, and finishes
    with a final run of the same gate list plus governance (playbook, auditor,
    memory, notify). Every terminal path funnels through :meth:`_finish`.

    All dependencies are keyword-only and required; ``config.build_orchestrator``
    (a later task) supplies concrete instances.
    """

    def __init__(self, *, objective, workspace, builder, reviewer, worktrees,
                 ledger, notifier, memory, playbook, cursor, gates: list,
                 ensemble_n: int = 2, run_id: str = "",
                 autonomy_requested=AutonomyLevel.L3, oscillation_window: int = 2,
                 policy=None, pricing=None, format_retries: int = 1,
                 architect_mode: str = "auto", diagram_validator=None,
                 visual_cfg=None, visual_provider=None,
                 mode: str = "auto",
                 attempt_specs=None, builder_factory=None) -> None:
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
            gates: Ordered ``GateSpec`` list run fail-fast per attempt and
                finally; each spec's own ``prove`` flag decides whether that
                gate gets proof-of-test (revert → red → green).
            ensemble_n: Number of attempts to build per slice.
            run_id: Stable identifier for the run (used for resume and memory).
            autonomy_requested: Requested ``AutonomyLevel`` (resolved against a
                readiness checklist; M1 records the level and proceeds — behavioral
                L2 checkpoints arrive with the M2 ``next`` protocol).
            oscillation_window: Consecutive identical-finding rounds that mean
                the loop is stuck.
            policy: Optional ``VerificationPolicy`` threaded to every gate run;
                each gate uses its own default when ``None``.
            pricing: Optional pricing table (``pricing.load_pricing``) used to
                estimate per-call spend when a provider reports ``cost_usd=0``;
                ``None`` keeps reported-cost-only accounting.
            format_retries: Corrective builder calls allowed per build round
                when a non-empty response contains no valid FILE blocks
                (threaded to every ``BuildSession``); ``0`` disables the retry.
            architect_mode: Smart-trigger mode for the architect stage —
                ``"auto"`` (run only when decomposition yields 2+ slices),
                ``"on"`` (always run), or ``"off"`` (never run).
            diagram_validator: Mermaid validation runner threaded to
                ``architect.generate_design`` (``None`` selects the real
                maid-backed default; tests inject a fake).
            visual_cfg: Optional mapping with ``screenshot_cmd`` — the
                allowlisted command whose LAST whitespace token names the
                screenshot it writes. Together with ``visual_provider`` this
                arms the advisory VLM critique on each slice's winner path;
                ``None`` (the default) keeps the feature entirely off.
            visual_provider: Optional vision-capable provider that critiques
                the screenshot (findings only, ledgered, never a gate).
            mode: Requested task mode, resolved via ``modes.detect_mode`` at
                the start of :meth:`run`. ``"auto"`` (the default) detects
                ``"build"`` vs ``"enhance"`` from the workspace's tracked
                files; ``"build"`` is greenfield (the pre-M4 behavior, byte
                identical); ``"enhance"`` is brownfield (repo context is
                assembled once per run, carried on every slice objective, and
                the architect prompt receives the repo map); ``"fix"`` is
                explicit-only and reproduce-first (a RED repro test is
                engine-committed before decompose/architect, a proving gate
                pins it, and the repro is restored after every promote);
                ``"audit"`` is explicit-only and READ-ONLY (a chunked
                reviewer sweep plus the read-only configured gates render
                ``.loop-engineer/findings.md`` — no decompose, no ensemble,
                no worktrees, no promote, and no file outside
                ``.loop-engineer/`` is created or modified).
            attempt_specs: Optional per-attempt
                ``ensemble.AttemptSpec`` list (diverse ensembles); ``specs[i]``
                shapes attempt ``i``'s builder. ``None`` keeps every attempt
                on the shared ``builder``. Diversity changes CONSTRUCTION
                only — winner selection stays with the gates.
            builder_factory: Optional ``AttemptSpec -> Builder`` factory used
                inside the ensemble's ``build_fn`` to construct each
                attempt's builder from its spec (config knowledge stays in
                the factory, out of the orchestrator). ``None`` keeps the
                shared ``builder``.
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
        self.gates = gates
        self.ensemble_n = ensemble_n
        self.run_id = run_id
        self.autonomy_requested = autonomy_requested
        self.oscillation = OscillationDetector(window=oscillation_window)
        self.policy = policy
        self.pricing = pricing
        self.format_retries = format_retries
        self.architect_mode = architect_mode
        self.diagram_validator = diagram_validator
        self.visual_cfg = visual_cfg
        self.visual_provider = visual_provider
        self.mode = mode
        self.attempt_specs = attempt_specs
        self.builder_factory = builder_factory
        # Journal: reviewer-side corrective format retries. The retry loop
        # lives in ``agents.Reviewer.review`` and a ``CrossModelReviewer``
        # wraps the real Reviewer, so the hook is wired on the INNER agent.
        # Only an exposed-and-unclaimed hook (attribute present, still None)
        # is taken — fakes without the attribute and callers that already
        # wired their own callback are left alone. The ONE wired callback
        # serves every reviewer call site (slice winner reviews AND audit
        # sweeps), so it reads a mutable context that each call site sets
        # just before using the reviewer — the journaled event carries the
        # honest stage ("review" + slice, or "audit"), never a static label.
        self._review_retry_context: dict = {"stage": "review"}
        inner_reviewer = getattr(reviewer, "reviewer", reviewer)
        if getattr(inner_reviewer, "on_retry", False) is None:
            inner_reviewer.on_retry = lambda: self.cursor.emit(
                "format_retry", dict(self._review_retry_context))
        self._repo_map = ""  # enhance-mode orientation map, assembled once per run
        self._context = ""  # enhance-mode slice context, assembled once per run
        self._unpriced_warned: set = set()  # models already warned about this run
        self._importlinter_ini = ""  # engine-rendered conformance contract (authoritative)
        self._architect_done = False  # durable flag loaded from a resumed cursor
        self._repro_path = ""  # fix mode: engine-committed repro (durable cursor key)

        self._feedback = ""
        # Feedback entries behind ``_feedback``: review findings reset the
        # window, stall observations append to it (bounded, so a stall never
        # erases what the previous review taught the next slice).
        self._feedback_entries: deque = deque(maxlen=_FEEDBACK_MAX_ENTRIES)
        self._slice_evidence: list = []  # winner GatesResult per completed slice
        self._total_slices = 0
        self._completed_slices = 0
        self._total_attempts = 0
        self._losing_attempts = 0

    def run(self) -> RunOutcome:
        """Execute the pipeline and return exactly one ``RunOutcome``.

        Returns:
            The terminal ``RunOutcome``; ``SUCCESS`` always carries evidence.

        Raises:
            RuntimeError: When the requested task mode is invalid (raised by
                ``modes.detect_mode`` before any cursor or provider work).
        """
        self.mode = detect_mode(self.workspace, self.mode)
        skip = self._begin_run()
        self._record_mode()
        if self.mode == "enhance":
            # Re-assembled on resume BY DESIGN: the context must describe the
            # CURRENT workspace (already-promoted slices included), not a
            # snapshot from the interrupted run.
            self._assemble_enhance_context()

        try:
            if self.mode == "audit":
                # Audit short-circuits the build pipeline entirely: a
                # read-only reviewer sweep + read-only gates — no decompose,
                # no ensemble, no worktrees, no promote.
                outcome = self._audit_stage()
            else:
                # Fix mode is reproduce-first: no decompose, no architect, no
                # provider spend on a fix nobody can prove — until a RED repro
                # is pinned. A blocked repro ends the run right here.
                outcome = self._repro_stage() if self.mode == "fix" else None
                if outcome is None:
                    # A resumed architect stage reuses the persisted
                    # design.md, so decomposition runs once, already against
                    # the design.
                    design_md = self._resume_design_md()
                    slices = self._decompose(design_md)
                    if not design_md and self._should_architect(len(slices)):
                        design_md = self._architect_stage()
                        slices = self._decompose(design_md)
                    if design_md:
                        self._append_architecture_gate(design_md)
                    self._total_slices = len(slices)
                    outcome = self._process_slices(slices, skip)
        except _BudgetStop as stop:
            outcome = RunOutcome(TerminalState.BUDGET_EXCEEDED, str(stop))
        return self._finish(outcome)

    def _record_mode(self) -> None:
        """Record the RESOLVED task mode on the cursor and the event stream.

        The cursor carries it as a durable key (the ``architect_done``
        pattern: the live stage/detail fields are overwritten by later
        transitions, so resume/``next`` readers need a persisted key) and the
        notifier announces it as a ``mode_resolved`` event.
        """
        self.cursor.record_mode(self.mode)
        self.notifier.notify(build_event(
            self.run_id, "mode", "mode_resolved", "info", {"mode": self.mode}))

    def _assemble_enhance_context(self) -> None:
        """Assemble the brownfield repo context exactly ONCE for this run.

        Computes the orientation map and the objective-relevant existing
        files against the main workspace and caches both: the assembled
        context rides on every slice objective and the raw map goes to the
        architect prompt. Both primitives fail soft (empty results for a
        non-repo), so an empty workspace simply leaves the briefs unchanged.
        """
        self._repo_map = repo_map(self.workspace)
        files = select_files(self.workspace, self.objective.goal)
        self._context = assemble_context(self._repo_map, files)

    def _repro_stage(self) -> RunOutcome | None:
        """Fix mode: pin the bug with an engine-committed RED repro test.

        Runs BEFORE decompose/architect. The repro must be committed before
        any fix attempt because ``prove()`` stashes uncommitted files — an
        uncommitted repro would revert WITH the fix during proof-of-test, so
        red would mean file-missing, not bug-back. Committed first,
        revert→red genuinely proves the fix kills the bug.

        Idempotent on resume: a cursor carrying the durable ``repro_path``
        key skips regeneration/re-commit and just re-arms the proving gate.
        Every fix-mode builder brief carries the repo context (map + selected
        files, the same once-per-run assembly enhance uses) FOLLOWED by
        ``FIX_MODE_RULES`` — repo context first, the rules last so they are
        never lost (the repro is engine-pinned; modifying it is tampering).

        Returns:
            ``None`` when the repro is pinned and gated (the pipeline
            proceeds), or a terminal ``RunOutcome`` (``BLOCKED``) when no
            failing reproduction could be produced.

        Raises:
            _BudgetStop: When the run cost cap is already reached.
        """
        # Compose the brownfield repo context (assembled once for the run, no
        # provider spend), then append the fix-mode rules so every slice brief
        # carries BOTH — rules last so a truncation can never drop them.
        self._assemble_enhance_context()
        self._context = (f"{self._context}\n\n{FIX_MODE_RULES}"
                         if self._context else FIX_MODE_RULES)
        if not self._repro_path:
            if self.ledger.run_total() >= self.ledger.run_cap_usd:
                raise _BudgetStop(
                    f"run cost ${self.ledger.run_total()} reached cap "
                    f"${self.ledger.run_cap_usd}")
            try:
                result = generate_repro(
                    self.builder, self.objective, self.workspace,
                    policy=self.policy,
                    on_usage=lambda r: self._record_cost("reproduce", r))
            except ProviderError as exc:
                return RunOutcome(TerminalState.BLOCKED,
                                  f"provider failure: {exc}")
            if result.blocked:
                return RunOutcome(TerminalState.BLOCKED, result.reason)
            self._repro_path = result.path
            self.cursor.set_stage("reproduce", "done",
                                  {"repro_path": result.path})
            self.cursor.mark_repro_path(result.path)
        self._append_repro_gate()
        return None

    def _audit_stage(self) -> RunOutcome:
        """Audit mode: read-only reviewer sweep + read-only gates → findings.md.

        Selection happens here (the ``_assemble_enhance_context`` pattern,
        with the audit's larger budget): the repo map orients the reviewer
        and ``select_files`` scores tracked files against the run objective.
        A vague objective that selects nothing falls back to ALL tracked
        files up to the budget — an audit always sweeps. The sweep itself
        (chunking, one ledgered reviewer call per chunk, the category-
        filtered read-only gates, the report render) lives in
        ``audit.run_audit``; nothing outside ``.loop-engineer/`` is written.

        Returns:
            ``SUCCESS`` with ``reason="audit complete: N findings"`` and the
            report evidence, or ``BLOCKED`` on a provider failure.

        Raises:
            _BudgetStop: When the run cost cap is already reached.
        """
        if self.ledger.run_total() >= self.ledger.run_cap_usd:
            raise _BudgetStop(
                f"run cost ${self.ledger.run_total()} reached cap "
                f"${self.ledger.run_cap_usd}")
        repo_map_text = repo_map(self.workspace)
        files = select_files(self.workspace, self.objective.goal,
                             budget_bytes=AUDIT_SELECT_BUDGET_BYTES)
        if not files:
            files = fallback_files(self.workspace, AUDIT_SELECT_BUDGET_BYTES)
        # Honest journal context: a reviewer retry inside the sweep is audit
        # work, not a slice review — label it so before the reviewer runs.
        self._review_retry_context = {"stage": "audit"}
        # Per-chunk budget cap: a bool-returning closure (NO exception crosses
        # into audit.py — ``_BudgetStop`` stays orchestrator-private). It stops
        # the sweep before the NEXT reviewer call once the run cap is reached,
        # and records that a stop happened for the escalate-level event below.
        budget = {"stopped": False}

        def budget_check() -> bool:
            if self.ledger.run_total() >= self.ledger.run_cap_usd:
                budget["stopped"] = True
                return False
            return True

        try:
            path, findings = run_audit(
                self.reviewer, repo_map_text, files, self.gates,
                self.workspace, policy=self.policy,
                on_usage=lambda r: self._record_cost("audit", r),
                budget_check=budget_check)
        except ProviderError as exc:
            # A reviewer format-retry failure attaches the COMPLETED first
            # call's spend as ``partial_result`` — ledger it (M1 invariant).
            partial = getattr(exc, "partial_result", None)
            if partial is not None:
                self._record_cost("audit_partial", partial)
            return RunOutcome(TerminalState.BLOCKED,
                              f"provider failure: {exc}")
        self.cursor.set_stage("audit", "done",
                              {"findings": len(findings), "findings_md": path})
        if budget["stopped"]:
            # Audit is advisory: the completed findings are KEPT and the
            # outcome stays SUCCESS. But an unattended operator must learn the
            # cap bound — an escalate-level event, not a plain-info success.
            self.notifier.notify(build_event(
                self.run_id, "audit", "audit_budget_stop", "escalate",
                {"cap_usd": self.ledger.run_cap_usd,
                 "spent_usd": self.ledger.run_total(),
                 "findings": len(findings)}))
            reason = (f"audit complete (budget stop at "
                      f"${self.ledger.run_cap_usd} cap): "
                      f"{len(findings)} findings")
        else:
            reason = f"audit complete: {len(findings)} findings"
        return RunOutcome(
            TerminalState.SUCCESS,
            reason=reason,
            evidence=[{"findings_md": path, "count": len(findings)}])

    def _append_repro_gate(self) -> None:
        """Append the gate that makes a winner prove it turned the repro GREEN.

        ``prove=True``: proof-of-test reverts the (uncommitted) fix and the
        committed repro must go red again — bug-back, not file-missing. The
        command is the config-isolated ``repro_check_command`` (conftest and
        ini addopts disabled) so a builder-written pytest config cannot
        skip-mark the repro into a forged pass. Idempotent: an
        already-present repro gate (a resumed run) is never duplicated.
        """
        command = repro_check_command(self._repro_path)
        if any(g.command == command for g in self.gates):
            return
        self.gates.append(GateSpec(name="unit", command=command, prove=True))
        logger.info("repro proving gate appended: %s", command)

    def _ensure_pinned_repro(self, root: str) -> bool:
        """Re-assert the engine-pinned repro test at *root* (fix mode).

        Called BEFORE every gate run in an attempt worktree (so gates always
        execute the committed repro, and direct-rewrite tampering loses at
        the attempt level) and AFTER every promote in the main workspace
        (defense-in-depth against M2's promote-back tamper class — see
        ``_restore_conformance_contract``). The repro is committed, hence
        trivially restorable via ``git checkout -- <repro_path>``. A
        detected difference logs a tamper warning AND emits a
        ``tamper_detected`` event (mirroring ``mode_resolved``).

        Args:
            root: The git tree to pin the repro in (attempt worktree or the
                main workspace).

        Returns:
            True when the pinned repro is in place (or there is nothing to
            do: not fix mode, no repro yet, or *root* is not a git tree —
            test fakes). False when the restore FAILED: the caller must
            fail CLOSED, never gate against a possibly-tampered repro.
        """
        if self.mode != "fix" or not self._repro_path:
            return True
        if not _is_git_repo(root):
            return True
        try:
            status = subprocess.run(
                ["git", "status", "--porcelain", "--", self._repro_path],
                cwd=root, capture_output=True, text=True,
                check=True, timeout=60)
            if status.stdout.strip():
                logger.warning(
                    "%s differed from the engine-pinned reproduction in %s "
                    "(builder tampering?); restoring the committed version",
                    self._repro_path, root)
                self.notifier.notify(build_event(
                    self.run_id, "reproduce", "tamper_detected", "warn",
                    {"path": self._repro_path, "root": root}))
            subprocess.run(
                ["git", "checkout", "--", self._repro_path],
                cwd=root, capture_output=True, text=True,
                check=True, timeout=60)
            return True
        except (subprocess.SubprocessError, OSError) as exc:
            detail = (getattr(exc, "stderr", "") or "").strip() or str(exc)
            logger.error("could not restore repro %s in %s: %s",
                         self._repro_path, root, detail)
            return False

    def _decompose(self, design_md: str) -> list:
        """Run the ledgered decompose call (against *design_md* when non-empty)."""
        slices = generate_slices(
            self.objective, self.builder, design_md=design_md,
            on_usage=lambda r: self._record_cost("decompose", r))
        self.cursor.set_stage("decompose", "done", {"slices": [s.role for s in slices]})
        return slices

    def _should_architect(self, slice_count: int) -> bool:
        """Apply the smart trigger: off never, on always, auto on 2+ slices."""
        if self.architect_mode == "off":
            return False
        if self.architect_mode == "on":
            return True
        return slice_count >= 2

    def _architect_stage(self) -> str:
        """Run the architect: one ledgered design call, persisted as design.md.

        The budget cap is checked BEFORE the provider call (mirroring the
        ensemble's per-attempt check) so a spent-out run never starts the
        stage. The design is written to the workspace root, recorded in
        memory, and marked done on the cursor via the durable resume key.

        Returns:
            The design markdown for the re-decompose.

        Raises:
            _BudgetStop: When the run cost cap is already reached.
        """
        if self.ledger.run_total() >= self.ledger.run_cap_usd:
            raise _BudgetStop(
                f"run cost ${self.ledger.run_total()} reached cap "
                f"${self.ledger.run_cap_usd}")
        design, result = generate_design(
            self.builder, self.objective, validate=self.diagram_validator,
            repo_map=self._repo_map)
        self._record_cost("architect", result)
        path = os.path.join(self.workspace, "design.md")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(design.markdown)
        except OSError as exc:
            # Degraded, not fatal: the in-memory design still drives the
            # re-decompose and the conformance gate; a later resume simply
            # re-arms the architect (design.md missing -> re-run).
            logger.warning(
                "could not persist design.md at %s (%s); the run continues "
                "with the in-memory design", path, exc)
        self.memory.record("design", design.markdown, run_id=self.run_id, kind="design")
        self.cursor.set_stage("architect", "done", {"problems": design.problems})
        self.cursor.mark_architect_done()
        return design.markdown

    def _append_architecture_gate(self, design_md: str) -> None:
        """Make the design's declared layering executable as a gate.

        Renders ``.importlinter`` into the workspace root and appends the
        implicit ``architecture`` gate (``lint-imports``, ``prove=False`` —
        proof-of-test's revert semantics do not apply to a static-structure
        check) when ALL preconditions hold: the design declares layering,
        ``lint-imports`` is on PATH, and the workspace contains a detectable
        Python package. Any missing precondition logs a visible SKIPPED
        warning naming it. Idempotent: an already-present ``architecture``
        gate (configured or from a resumed run) is never duplicated.

        Args:
            design_md: The design markdown whose layering drives the gate.
        """
        if any(g.name == "architecture" for g in self.gates):
            return
        layering = Design.parse(design_md).layering
        if not layering:
            logger.warning(
                "architecture conformance gate SKIPPED: design declares no layering")
            return
        if not shutil.which("lint-imports"):
            logger.warning(
                "architecture conformance gate SKIPPED: lint-imports not on PATH")
            return
        package = _detect_python_package(self.workspace)
        if not package:
            logger.warning(
                "architecture conformance gate SKIPPED: no Python package "
                "detected in the workspace")
            return
        ini = render_importlinter(layering, package)
        if not ini:
            logger.warning(
                "architecture conformance gate SKIPPED: layering declares no "
                "usable layers or forbidden imports")
            return
        path = os.path.join(self.workspace, ".importlinter")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(ini)
        self._importlinter_ini = ini
        self.gates.append(GateSpec(name="architecture", command="lint-imports",
                                   prove=False))
        logger.info("architecture conformance gate appended (%s)", path)

    def _restore_conformance_contract(self) -> None:
        """Re-assert the engine-rendered ``.importlinter`` after a promote.

        ``promote`` brings the winner's untracked files back into the
        workspace, so a builder that rewrote ``.importlinter`` inside its
        worktree would otherwise get its weakened contract promoted over the
        engine render, propagated into later slices, and enforced at the
        final gate. The conformance contract must never be mutable by the
        code under review: the engine render (held on ``self`` since the
        gate armed) is authoritative and is rewritten after every promote,
        with a visible warning when the promoted content differed.
        """
        if not self._importlinter_ini:
            return
        path = os.path.join(self.workspace, ".importlinter")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                current = handle.read()
        except OSError:
            current = None
        if current == self._importlinter_ini:
            return
        logger.warning(
            "promoted .importlinter differed from the engine-rendered "
            "conformance contract (builder tampering?); restoring %s", path)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self._importlinter_ini)
        except OSError as exc:
            logger.error("could not restore %s: %s", path, exc)

    def _propagate_engine_artifacts(self, wt_path: str) -> None:
        """Copy engine-rendered config into a fresh attempt worktree.

        ``git worktree add`` materializes committed files only, while the
        conformance gate's ``.importlinter`` is rendered by the engine into
        the (uncommitted) workspace root — without this copy the
        ``architecture`` gate would error on the missing config in every
        attempt worktree and stall the slice. The gate must see the
        engine-rendered config in every gating context. Best-effort: a copy
        failure logs a warning and never blocks the attempt.

        Args:
            wt_path: The freshly created attempt worktree root.
        """
        src = os.path.join(self.workspace, ".importlinter")
        if not os.path.isfile(src):
            return
        try:
            shutil.copy2(src, os.path.join(wt_path, ".importlinter"))
        except OSError as exc:
            logger.warning(
                "could not propagate .importlinter into attempt worktree %s: %s",
                wt_path, exc)

    def _resume_design_md(self) -> str:
        """Return the persisted design for a resumed, already-done architect stage.

        Empty when this run's cursor does not carry the durable
        ``architect_done`` flag or ``design.md`` is missing — either way the
        architect trigger evaluates normally again.
        """
        if not self._architect_done:
            return ""
        path = os.path.join(self.workspace, "design.md")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            logger.warning(
                "cursor marks the architect done but %s is unreadable; "
                "the architect stage will re-run", path)
            return ""

    def _cost_of(self, result_like) -> float:
        """Return the dollar cost to ledger for one completed provider call.

        Works on anything carrying usage attributes (``ProviderResult`` and
        ``BuildRound`` alike, via ``getattr``). Precedence:

        1. A reported ``cost_usd > 0`` (e.g. claude_code) is used AS-IS.
        2. No pricing table: the reported cost (today's behavior).
        3. Otherwise ``pricing.estimate_cost`` from the token usage.
        4. Unknown model (no pricing row): warn ONCE per model per run and
           count $0 — never crash a run over a missing price.

        Args:
            result_like: Object with ``cost_usd``/``model``/``tokens``/
                ``prompt_tokens``/``completion_tokens`` attributes.

        Returns:
            The spend to record, in USD (never negative, never ``None``).
        """
        reported = float(getattr(result_like, "cost_usd", 0.0) or 0.0)
        if reported > 0:
            return reported
        if not self.pricing:
            return reported
        model = str(getattr(result_like, "model", "") or "")
        estimate = estimate_cost(
            model=model,
            prompt_tokens=int(getattr(result_like, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(result_like, "completion_tokens", 0) or 0),
            tokens=int(getattr(result_like, "tokens", 0) or 0),
            table=self.pricing,
        )
        if estimate is None:
            if model not in self._unpriced_warned:
                self._unpriced_warned.add(model)
                logger.warning(
                    "no pricing for model %r — its spend counts as $0, so the "
                    "budget cap cannot bind for this model", model)
            return 0.0
        return estimate

    def _record_cost(self, site: str, result_like) -> None:
        """Ledger one provider call's spend and journal it as ``cost_recorded``.

        The single funnel for every ledger-record site, so each records the
        SAME dollars it journals. The LEDGER remains the source of truth for
        money (the M1 invariant); the ``cost_recorded`` event is purely
        observational — reconstruction reconciles the journal against
        ``ledger.run_total()``, never the other way around.

        Args:
            site: The call site being recorded (e.g. ``"decompose"``,
                ``"build_attempt"``, ``"review"``).
            result_like: Object carrying usage attributes (``ProviderResult``
                or ``BuildRound``), priced via :meth:`_cost_of`.
        """
        usd = self._cost_of(result_like)
        self.ledger.record(usd)
        self.cursor.emit("cost_recorded", {
            "site": site,
            "model": str(getattr(result_like, "model", "") or ""),
            "usd": usd,
        })

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
            self._architect_done = bool(existing.get("architect_done"))
            self._repro_path = str(existing.get("repro_path") or "")
            # A resumed run keeps its recorded mode: re-resolving (e.g. auto
            # → enhance on a repo the fix run just committed a repro into)
            # would silently drop mode-specific stages like repro re-arming.
            persisted_mode = existing.get("mode")
            if persisted_mode and persisted_mode != self.mode:
                logger.warning(
                    "resumed run was recorded as mode %r but this invocation "
                    "resolved %r; keeping the persisted mode",
                    persisted_mode, self.mode)
                self.mode = persisted_mode
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

        final = run_gates(self.gates, self.workspace, policy=self.policy)
        if not final.passed:
            # Fail-fast means the LAST record is the failure; an empty record
            # list means nothing ran at all.
            reason = (final.records[-1].evidence if final.records
                      else "no gates configured")
            return RunOutcome(TerminalState.BLOCKED, reason)
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
                                    constraints=self.objective.constraints,
                                    context=self._context)
        feedback = self._feedback
        attempt_files: dict = {}  # worktree path -> files_written this attempt
        attempt_builders: dict = {}  # worktree path -> the Builder that built it
        created: list = []  # every worktree created for this slice (crash cleanup)
        # Observer output from FAILING attempts (gate_fn appends, build_fn reads).
        # run_ensemble is strictly sequential build(i) -> gate(i) -> build(i+1),
        # so attempt i+1's brief sees everything attempt i's failure observed.
        observations: list = []
        # Journal correlation: gate_fn only receives a worktree path, so the
        # sequential build(i) -> gate(i) contract above is what lets it stamp
        # gate events with the attempt index build_fn last recorded here.
        current_attempt = {"index": -1}

        def observation_text():
            # Total injected observation text is capped like a single observer run.
            return "\n\n".join(observations)[:OBSERVER_CAP]

        def build_fn(i):
            # Budget check FIRST: stop before the NEXT attempt once the cap is
            # reached. Every provider call is ledgered with pricing-backed
            # per-call accounting (_cost_of), so the cap binds for paid
            # providers too; the overshoot is at most one attempt.
            if self.ledger.run_total() >= self.ledger.run_cap_usd:
                raise _BudgetStop(
                    f"run cost ${self.ledger.run_total()} reached cap "
                    f"${self.ledger.run_cap_usd}")
            self._total_attempts += 1
            wt_path = self.worktrees.create(f"{slice_.role}-{i}")
            created.append(wt_path)
            self._propagate_engine_artifacts(wt_path)
            workspace = Workspace(root=wt_path)  # BuildSession takes a Workspace object
            brief_feedback = feedback
            if observations:
                obs = observation_text()
                brief_feedback = f"{feedback}\n\n{obs}" if feedback else obs
            # Diverse ensembles: attempt i gets a builder constructed from its
            # spec via the injected factory (temperature/model diversity lives
            # entirely in construction — the gates still pick the winner).
            builder = self.builder
            spec = (self.attempt_specs[i]
                    if self.attempt_specs and i < len(self.attempt_specs)
                    else None)
            if spec is not None and self.builder_factory is not None:
                builder = self.builder_factory(spec)
            attempt_builders[wt_path] = builder
            current_attempt["index"] = i
            self.cursor.emit("attempt_started", {
                "slice": slice_.role, "attempt": i,
                "model": ((spec.model if spec is not None else "")
                          or str(getattr(builder.provider, "model", "")
                                 or builder.provider.name)),
                "temperature": spec.temperature if spec is not None else None,
            })
            round_ = BuildSession(builder=builder, workspace=workspace,
                                  objective=slice_objective,
                                  format_retries=self.format_retries,
                                  on_retry=lambda: self.cursor.emit(
                                      "format_retry", {"stage": "build",
                                                       "slice": slice_.role,
                                                       "attempt": i}),
                                  ).build_round(1, brief_feedback)
            self._record_cost("build_attempt", round_)
            attempt_files[wt_path] = list(round_.files_written)
            return (wt_path, len(round_.files_written))

        def gate_fn(wt):
            # Fix mode: gates must always execute the engine-pinned repro —
            # restore it in THIS worktree before running anything, so a
            # direct rewrite loses honestly at the attempt level. A failed
            # restore fails the attempt (never gate a tampered repro).
            if not self._ensure_pinned_repro(wt):
                gates_result = GatesResult(records=[EvidenceRecord(
                    gate="unit", command=repro_check_command(self._repro_path),
                    passed=False,
                    evidence="engine could not restore the pinned repro in "
                             "this worktree; attempt rejected (fail closed)")])
                self._journal_gate_results(
                    slice_.role, current_attempt["index"], gates_result)
                return gates_result
            gates_result = run_gates(self.gates, wt, policy=self.policy)
            for rec in getattr(gates_result, "records", []):
                if not rec.passed and getattr(rec, "observed", ""):
                    observations.append(f"OBSERVED (gate: {rec.gate}):\n{rec.observed}")
            self._journal_gate_results(
                slice_.role, current_attempt["index"], gates_result)
            return gates_result

        try:
            attempts = run_ensemble(self.ensemble_n, build_fn, gate_fn,
                                    specs=self.attempt_specs)

            # 7. No verified winner: nothing passed its gate.
            winner = select_winner(attempts)
            if winner is None:
                self._losing_attempts += len(attempts)
                self._remove_attempts(attempts)
                reason = f"no attempt passed its gate for slice '{slice_.role}'"
                if observations:
                    # What the observer saw survives the stall: it lands on
                    # the outcome's evidence and APPENDS to the bounded
                    # feedback history (never replacing what the previous
                    # review taught) to seed a resumed run.
                    observed = observation_text()
                    self._feedback_entries.append(observed)
                    self._feedback = "\n\n".join(self._feedback_entries)
                    return RunOutcome(TerminalState.STALLED, reason,
                                      evidence=[{"observed": observed}])
                return RunOutcome(TerminalState.STALLED, reason)

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
            # GUARD LAYER 2 first: re-verify builder ≠ reviewer against the
            # winner's CONSTRUCTED provider object (a label is never identity)
            # before the reviewer sees a single token.
            self._check_winner_independence(
                attempt_builders.get(winner.workspace, self.builder),
                winner.spec)
            # The advisory VLM critique (gates already passed) appends to the
            # same findings stream — advisory input, never a verdict.
            snapshot = self._snapshot(winner.workspace, winner_files)
            # Honest journal context: a reviewer retry here belongs to THIS
            # slice's winner review — stamp the slice before the call.
            self._review_retry_context = {"stage": "review",
                                          "slice": slice_.role}
            review = self.reviewer.review(slice_objective, snapshot)
            self._record_cost("review", review.result)
            findings = list(review.findings)
            findings.extend(self._visual_findings(slice_objective, winner.workspace))
            if self.oscillation.observe(findings):
                self._remove_attempts(attempts)
                return RunOutcome(
                    TerminalState.OSCILLATION,
                    f"repeating findings for slice '{slice_.role}'")
            # A completed review RESETS the feedback window: its findings
            # describe the workspace as promoted, superseding older feedback.
            self._feedback_entries.clear()
            if findings:
                self._feedback_entries.append("\n".join(findings))
            if observations:
                # Losing attempts' observer output still teaches the next slice.
                self._feedback_entries.append(observation_text())
            self._feedback = "\n\n".join(self._feedback_entries)
        except CrossModelReviewError as exc:
            # Layer-2 violation: the winning attempt's ACTUAL provider shares
            # the reviewer's family. Not a ProviderError, so it gets its own
            # clean terminal mapping — and this slice's worktrees must never
            # leak on the way out.
            self._cleanup(created)
            return RunOutcome(TerminalState.BLOCKED,
                              f"cross-model review guard: {exc}")
        except ProviderError as exc:
            # Provider blips (local Ollama restarts, network) are routine: end
            # in a clean terminal outcome instead of escaping run(), and never
            # leak this slice's worktrees. A corrective-retry failure attaches
            # the round's COMPLETED first-call spend as ``partial_result`` —
            # ledger it so paid tokens are never lost to cost accounting.
            partial = getattr(exc, "partial_result", None)
            if partial is not None:
                self._record_cost("slice_partial", partial)
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
        self._restore_conformance_contract()
        if not self._ensure_pinned_repro(self.workspace):
            # Fail CLOSED: the final gates must never run against a repro the
            # engine could not re-pin after promote.
            return RunOutcome(
                TerminalState.BLOCKED,
                f"could not restore engine-pinned repro {self._repro_path} "
                "after promote; run halted (fail closed)")
        # Best-effort auto-diagram: regenerate the module-graph archmap from
        # the freshly promoted code; a failure here never fails the run.
        try:
            write_archmap(self.workspace)
        except Exception as exc:  # noqa: BLE001 - explicitly best-effort
            logger.warning("archmap generation failed (run continues): %s", exc)
        self.memory.record(f"contract.{slice_.role}",
                           slice_.contract or slice_.objective,
                           run_id=self.run_id, kind="contract")
        self.cursor.complete_slice(slice_.role)
        self.notifier.notify(build_event(
            self.run_id, "slice", "slice_completed", "info", {"slice": slice_.role}))
        self._completed_slices += 1
        self._slice_evidence.append(winner.evidence)
        return None

    def _journal_gate_results(self, slice_role: str, attempt: int,
                              gates_result) -> None:
        """Journal one attempt's gate run: ``gate_result`` per gate, plus
        ``observer_ran`` for failing gates that carry observer output.

        Observer note: the observer itself is invoked INSIDE
        ``verifier.run_gates`` (on a failing gate, before fail-fast), so its
        invocation is not directly visible at the orchestrator layer. The
        orchestrator-visible surface is the returned evidence record carrying
        a non-empty ``observed`` — ``observer_ran`` is emitted where that
        evidence is processed, which is exactly once per observer invocation
        that produced output. ``truncated`` flags output that filled the
        ``OBSERVER_CAP`` (the verifier caps, never marks).

        Args:
            slice_role: The slice whose attempt was gated.
            attempt: The ensemble attempt index (``build_fn``'s ``i``).
            gates_result: The ``GatesResult`` the attempt's gate run returned.
        """
        for rec in getattr(gates_result, "records", []):
            proof = getattr(rec, "proof", None)
            self.cursor.emit("gate_result", {
                "slice": slice_role, "attempt": attempt,
                "gate": rec.gate, "ok": rec.passed,
                "proven": proof.proven if proof is not None else None,
            })
            observed = getattr(rec, "observed", "")
            if not rec.passed and observed:
                self.cursor.emit("observer_ran", {
                    "slice": slice_role, "attempt": attempt,
                    "gate": rec.gate,
                    "truncated": len(observed) >= OBSERVER_CAP,
                })

    def _check_winner_independence(self, builder, spec) -> None:
        """Guard layer 2: re-verify builder ≠ reviewer for the WINNING attempt.

        Diverse ensembles construct a builder per attempt, so the identity
        the reviewer was validated against at config time is no longer the
        only one that can win. The winner's constructed provider OBJECT (never
        a roster label) is checked against the reviewer family via the
        reviewer's per-call ``check`` API just before the review call. A
        reviewer without that API (a plain ``Reviewer`` in single-builder
        wiring) skips the check — the constructor guard already covered its
        one builder.

        Args:
            builder: The ``agents.Builder`` that produced the winning attempt.
            spec: The winner's ``AttemptSpec`` (or ``None``); its ``family``
                declares independence for third-party models.

        Raises:
            CrossModelReviewError: When the winner's provider family matches
                the reviewer's (including two unprovable ``"unknown"``s).
        """
        check = getattr(self.reviewer, "check", None)
        if check is None:
            return
        check(builder.provider, family=getattr(spec, "family", "") or "")

    def _visual_findings(self, slice_objective, workspace: str) -> list:
        """Run the advisory VLM screenshot critique for a gated winner.

        Off (empty list) unless BOTH ``visual_cfg.screenshot_cmd`` and
        ``visual_provider`` are configured. The screenshot command runs under
        the run's gate policy inside the winner's worktree, the vision call is
        ledgered via :meth:`_cost_of`, and ``visual_critique`` guarantees no
        failure here ever raises or changes a verdict.

        Args:
            slice_objective: The slice ``Objective`` the screenshot is judged
                against.
            workspace: The winning attempt's worktree root.

        Returns:
            Advisory findings to append to the review stream (often empty).
        """
        command = (self.visual_cfg or {}).get("screenshot_cmd", "")
        if not command or self.visual_provider is None:
            return []
        return visual_critique(
            command, workspace, self.policy, self.visual_provider,
            slice_objective.goal,
            on_usage=lambda result: self._record_cost("visual", result))

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
            if self.mode != "audit":
                # An audit proves nothing — its gates run with prove=False, so
                # a SUCCESS audit must NOT assert the verifier-proven fact.
                # build/enhance/fix successes still record it.
                self.memory.record("verifier.proven", "true",
                                   run_id=self.run_id, kind="fact")
            self.memory.consolidate(self.run_id)

        self.cursor.set_stage("terminal", outcome.state.value,
                              {"auditor": verdict, "outcome": outcome.to_dict()})
        level = ("escalate" if outcome.state in (TerminalState.APPROVAL_REQUIRED,
                                                 TerminalState.BUDGET_EXCEEDED)
                 else "info")
        self.notifier.notify(build_event(
            self.run_id, "terminal", outcome.state.value, level, outcome.to_dict()))
        return outcome
