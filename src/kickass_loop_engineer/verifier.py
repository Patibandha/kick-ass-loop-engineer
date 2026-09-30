"""The verifier: gates return evidence, and a passing gate is proven real.

Proof-of-test runs a gate three times around the working-tree change:
    green_before  — gate passes WITH the change in place
    red_when_reverted — gate FAILS once the change's NON-test files are stashed
    green_after   — gate passes again after restoring them
A gate counts as proven only when all three hold. This defeats "fake done":
a test that passes regardless of the change is not evidence of anything.

The revert is scoped to non-test files on purpose. An additive slice adds new
tests together with the source they exercise; stashing the tests as well would
leave only the repository's existing (already green) suite behind, nothing would
turn red, and honest work would be rejected. Reverting source alone keeps the
demand exact: the new tests must fail without the code they cover. A change with
no non-test file to revert — a tests-only change — has no proof to give, so
``prove`` records a ``skipped_reason`` and the gate's own exit code stands (see
``run_gate``).

Verdicts come from real artifacts in the workspace (process exit codes), never
from a model's transcript:
    "Use the worktree and external state as authoritative. Do not rely on intent,
     memory of earlier work, or a plausible final answer as proof of completion."
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field, replace
from typing import Optional

from .gates import GATES
from .guardrails import VerificationPolicy

logger = logging.getLogger(__name__)

OBSERVER_TIMEOUT_SECONDS = 600.0  # was 30.0: a grown test suite (7k+ statements,
                                  # ~2 min under coverage, longer under load)
                                  # must finish or stalls carry no retry feedback
OBSERVER_CAP = 8192

#: Directory components whose entire contents count as test files.
TEST_DIR_NAMES = frozenset({"tests", "test"})

ARTIFACTS_NOT_TRANSCRIPT = (
    "Use the worktree and external state as authoritative. Do not rely on intent, "
    "partial progress, memory of earlier work, or a plausible final answer as proof "
    "of completion."
)


@dataclass
class ProofRecord:
    """Evidence that a gate's pass is real (revert -> red -> green).

    Attributes:
        gate_cmd: The gate command the proof ran.
        green_before: The gate passed with the whole change in place.
        red_when_reverted: The gate failed once the non-test files were stashed.
        green_after: The gate passed again after the change was restored.
        error: Why the proof could not reach a verdict, when it could not.
        aborted: The change could not be auto-restored and waits in the stash.
        skipped_reason: Why proof-of-test did not APPLY to this change (no
            non-test file to revert). Set only for that case, and it is what
            tells ``run_gate`` to keep the gate's own verdict; ``proven`` stays
            False because nothing was proven.
    """

    gate_cmd: str
    green_before: bool = False
    red_when_reverted: bool = False
    green_after: bool = False
    error: str = ""
    aborted: bool = False
    skipped_reason: str = ""

    @property
    def proven(self) -> bool:
        """True only when the gate is green, goes red on revert, and green again."""
        return self.green_before and self.red_when_reverted and self.green_after


def _gate_passes(gate_cmd: str, workspace: str, policy: VerificationPolicy) -> bool:
    """Run an allowlisted gate; return True iff it exits zero."""
    return policy.run(gate_cmd, cwd=workspace).passed


def _git(workspace: str, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    """Run a git subcommand in the workspace, raising on failure or timeout."""
    return subprocess.run(["git", *args], cwd=workspace, capture_output=True,
                          text=True, check=True, timeout=timeout)


def _is_test_path(path: str) -> bool:
    """Say whether a repo-relative path belongs to the test suite.

    A path is a test path when any of its DIRECTORY components is ``tests`` or
    ``test``, or when its basename starts with ``test_``, ends with
    ``_test.py``, or is ``conftest.py``. Everything else — source, config,
    docs, data — is a non-test path, and non-test paths are what the proof
    reverts.

    Args:
        path: A repo-relative, forward-slashed path, exactly as git reports it.

    Returns:
        True for test paths, False for everything else.
    """
    components = path.split("/")
    basename = components[-1]
    if any(component in TEST_DIR_NAMES for component in components[:-1]):
        return True
    return (basename.startswith("test_") or basename.endswith("_test.py")
            or basename == "conftest.py")


def _changed_entries(workspace: str) -> list[tuple[str, str]]:
    """List every changed path in the workspace as ``(status_code, path)``.

    Reads ``git status --porcelain -z --untracked-files=all``. NUL separation
    keeps paths with spaces or non-ASCII bytes intact (the plain format
    C-quotes them), and ``--untracked-files=all`` expands untracked directories
    into their files so each one can be classified on its own. A rename or copy
    contributes BOTH of its paths: the destination and the source git records
    in the following field.

    Args:
        workspace: The git working tree to inspect.

    Returns:
        One ``(status_code, path)`` pair per changed path, in git's order; an
        empty list when the tree is clean.

    Raises:
        subprocess.CalledProcessError: When the git command fails.
        subprocess.TimeoutExpired: When the git command does not finish in time.
    """
    fields = _git(workspace, "status", "--porcelain", "-z",
                  "--untracked-files=all").stdout.split("\0")
    entries: list[tuple[str, str]] = []
    position = 0
    while position < len(fields):
        field_value = fields[position]
        position += 1
        if len(field_value) < 4:  # the trailing empty field, or a malformed entry
            continue
        code, changed = field_value[:2], field_value[3:]
        entries.append((code, changed))
        if code[0] in ("R", "C") and position < len(fields):
            entries.append((code, fields[position]))  # the rename/copy source
            position += 1
    return entries


def _unstashable(workspace: str, entries: list[tuple[str, str]],
                 paths: list[str]) -> list[str]:
    """Return the paths git cannot revert with a pathspec-scoped stash.

    ``git stash push -- <pathspec>`` saves the entry FIRST and then resets the
    named paths, so a tracked path that no longer exists in the index — a
    staged deletion, or the source side of a staged rename — aborts the reset
    with "did not match any files" and leaves a stash entry behind on an
    unreverted tree. Untracked paths are exempt: ``--include-untracked``
    carries them. Callers fall back to a whole-tree stash when this is
    non-empty.

    Args:
        workspace: The git working tree to inspect.
        entries: Every ``(status_code, path)`` pair from ``_changed_entries``.
        paths: The paths the caller intends to stash.

    Returns:
        The subset of *paths* git cannot match; empty when the stash is safe.

    Raises:
        subprocess.CalledProcessError: When the git command fails.
        subprocess.TimeoutExpired: When the git command does not finish in time.
    """
    wanted = set(paths)
    tracked = [changed for code, changed in entries
               if code != "??" and changed in wanted]
    if not tracked:
        return []
    listed = _git(workspace, "ls-files", "-z", "--", *tracked).stdout
    in_index = {name for name in listed.split("\0") if name}
    return [changed for changed in tracked if changed not in in_index]


def _discard_gate_side_effects(workspace: str,
                               stashed: Optional[list[str]]) -> None:
    """Undo tracked-file edits the reverted gate run made, so the pop is clean.

    A gate can rewrite tracked files while it runs (formatters, generated
    fixtures, recorded snapshots) and those edits collide with ``git stash
    pop``. Discarding them is scoped to exactly what was stashed and never
    wider: the test files the proof deliberately LEFT in the tree are not in
    the stash, so clobbering them would destroy the builder's work outright.

    Never raises — a failed discard only means the pop reports the collision,
    which the caller already handles by preserving the change in the stash.

    Args:
        workspace: The git working tree to clean.
        stashed: The paths that were stashed, or ``None`` after a whole-tree
            stash (then every tracked edit is discarded, as it always was).
    """
    try:
        if stashed is None:
            _git(workspace, "checkout", "--", ".")
            return
        listed = _git(workspace, "ls-tree", "-r", "-z", "--name-only", "HEAD",
                      "--", *stashed).stdout
        in_head = [name for name in listed.split("\0") if name]
        if in_head:
            _git(workspace, "checkout", "--", *in_head)
    except subprocess.CalledProcessError:
        pass
    except subprocess.TimeoutExpired:
        logger.warning("git timed out discarding gate side effects in %s", workspace)


def _summarize(paths: list[str], limit: int = 5) -> str:
    """Render a path list for a log line or an error, bounded to *limit* names.

    Args:
        paths: The paths to name.
        limit: How many to show before summarizing the remainder.

    Returns:
        A comma-separated string, with ``+N more`` when the list was truncated.
    """
    shown = ", ".join(paths[:limit])
    remainder = len(paths) - limit
    return f"{shown}, +{remainder} more" if remainder > 0 else shown


def prove(gate_cmd: str, workspace: str, policy: Optional[VerificationPolicy] = None) -> ProofRecord:
    """Prove a gate's pass is real by reverting the change's non-test files.

    Runs the gate green->red->green around a ``git stash`` of every changed
    NON-test file. Test files stay in the working tree deliberately: an
    additive slice adds new tests together with the source they exercise, and
    stashing the tests too would leave only the repository's existing (already
    green) suite to run — nothing would go red and a real pass would be
    rejected. With the source alone reverted, the new tests fail exactly as
    they must, and a test that passes without its source still proves nothing.

    Three outcomes are distinct:
      * proven — green, red without the source, green again;
      * not proven — the gate stayed green without the source (``proven``
        False, no ``skipped_reason``): the change is not what makes it pass;
      * not applicable — the change touches no non-test file at all, so there
        is nothing to revert that could turn the gate red. The record carries
        ``skipped_reason``, ``proven`` stays False, and ``run_gate`` keeps the
        gate's own verdict instead of failing the attempt.

    A clean tree is still an error, not a skip: with nothing uncommitted there
    is no change to verify in the first place.

    On a pop failure the change is NOT lost — it remains in the stash, and the
    record says so explicitly (``aborted=True``) so the caller can recover
    rather than silently proceeding on a corrupted tree.

    Args:
        gate_cmd: An allowlisted gate command (e.g. ``"pytest -q"``).
        workspace: A git working tree containing the (uncommitted) change. Build
            caches must be gitignored so the stash scopes to the real change.
        policy: Verification policy; a default is used when omitted.

    Returns:
        A ``ProofRecord``. ``record.proven`` is the verdict.
    """
    policy = policy or VerificationPolicy()
    record = ProofRecord(gate_cmd=gate_cmd)
    try:
        first = policy.run(gate_cmd, cwd=workspace)
        if first.error:
            record.error = f"gate could not run: {first.error}"
            return record
        record.green_before = first.passed
        if not record.green_before:
            record.error = "gate is not green with the change in place"
            return record
        # Proof-of-test needs an uncommitted change to revert. With a clean tree
        # there is nothing to stash, so report that plainly instead of a confusing
        # "no stash entries" error after a no-op stash.
        entries = _changed_entries(workspace)
        if not entries:
            record.error = "proof-of-test requires an uncommitted change to revert"
            return record
        # dict.fromkeys: de-duplicate while keeping git's order for the message.
        non_test = list(dict.fromkeys(changed for _code, changed in entries
                                      if not _is_test_path(changed)))
        if not non_test:
            changed_tests = sorted({changed for _code, changed in entries})
            record.skipped_reason = (
                "proof-of-test does not apply: the change touches only test "
                f"files ({_summarize(changed_tests)})"
            )
            return record
        unstashable = _unstashable(workspace, entries, non_test)
        stashed: Optional[list[str]] = None  # None: the whole tree was stashed
        if unstashable:
            # git cannot scope a stash to a staged deletion or a rename source,
            # and a half-saved stash on a live worktree is far worse than a
            # coarser revert — fall back to stashing everything.
            logger.warning(
                "proof-of-test reverting the whole tree in %s: git cannot scope "
                "a stash to %s", workspace, _summarize(unstashable))
            _git(workspace, "stash", "push", "--include-untracked")
        else:
            stashed = non_test
            _git(workspace, "stash", "push", "--include-untracked", "--", *non_test)
        try:
            record.red_when_reverted = not _gate_passes(gate_cmd, workspace, policy)
        finally:
            # Discard anything the reverted run modified in the stashed files so
            # the stash restores cleanly, then pop. If pop still fails, the change
            # is safe in the stash and we say so loudly instead of corrupting the
            # tree.
            _discard_gate_side_effects(workspace, stashed)
            try:
                _git(workspace, "stash", "pop")
            except subprocess.CalledProcessError as exc:
                record.aborted = True
                record.error = (
                    "could not auto-restore the change; it is preserved in the git "
                    f"stash (stash@{{0}}) — run 'git stash pop' in {workspace} to "
                    f"recover. ({(exc.stderr or '').strip()})"
                )
        if record.aborted:
            return record
        record.green_after = _gate_passes(gate_cmd, workspace, policy)
    except subprocess.CalledProcessError as exc:
        record.error = f"git operation failed: {(exc.stderr or exc)}"
    except subprocess.TimeoutExpired as exc:
        record.error = f"git operation timed out: {exc}"
    return record


@dataclass
class EvidenceRecord:
    """The artifact-grounded result of running one gate."""

    gate: str
    command: str
    passed: bool
    evidence: str = ""
    proof: Optional[ProofRecord] = None
    observed: str = ""

    def to_dict(self) -> dict:
        """Return a JSON-serializable representation."""
        d = {"gate": self.gate, "command": self.command, "passed": self.passed,
             "evidence": self.evidence}
        if self.observed:
            d["observed"] = self.observed
        if self.proof is not None:
            d["proof"] = {"proven": self.proof.proven, "green_before": self.proof.green_before,
                          "red_when_reverted": self.proof.red_when_reverted,
                          "green_after": self.proof.green_after, "aborted": self.proof.aborted,
                          "error": self.proof.error,
                          "skipped_reason": self.proof.skipped_reason}
        return d


def run_gate(name: str, command: str, workspace: str, with_proof: bool = False,
             policy: Optional[VerificationPolicy] = None, expect: str = "",
             min_count: Optional[int] = None) -> EvidenceRecord:
    """Run a named gate and return an evidence record built from real output.

    A gate is ``passed`` only if the command exits zero (and, when ``with_proof``,
    proof-of-test holds). An errored or rejected command is never ``passed``.

    The single exception is a proof that does not APPLY — a change with no
    non-test file to revert (see ``prove``). Nothing could have turned the gate
    red, so there is no proof to fail: the gate's own exit code stands. The
    record still carries the proof with ``proven`` False and the
    ``skipped_reason``, so no journal ever claims proof that was not run.
    """
    if name not in GATES:
        return EvidenceRecord(
            gate=name, command=command, passed=False,
            evidence=f"error: unknown gate {name!r}; known gates: {sorted(GATES)}",
        )
    policy = policy or VerificationPolicy()
    expectation = {"expect": expect, "min_count": min_count} if expect else {}
    result = policy.run(command, cwd=workspace, **expectation)
    evidence = f"exit={result.returncode}; {(result.stdout_tail or result.stderr_tail or '').strip()[-200:]}"
    if result.error:
        evidence = f"error: {result.error}"
    record = EvidenceRecord(gate=name, command=command, passed=result.passed, evidence=evidence)
    if with_proof and result.passed:
        record.proof = prove(command, workspace, policy)
        if record.proof.skipped_reason:
            logger.info("gate %r keeps its own verdict: %s", name,
                        record.proof.skipped_reason)
        else:
            record.passed = record.proof.proven
    return record


@dataclass
class GatesResult:
    """Ordered evidence for a gate list; passed only when every gate ran green.

    ``records`` holds one ``EvidenceRecord`` per gate that actually ran, in spec
    order. Because the runner is fail-fast, a failing run carries records up to
    and including the first failure. An empty ``records`` is never a pass: no
    gates ran, so there is no evidence of anything.
    """

    records: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True only when at least one gate ran and every record passed."""
        return bool(self.records) and all(r.passed for r in self.records)

    def to_dict(self) -> dict:
        """Return a JSON-serializable ``{"passed": ..., "gates": [...]}``."""
        return {"passed": self.passed, "gates": [r.to_dict() for r in self.records]}


def _run_observer(command: str, workspace: str,
                  policy: Optional[VerificationPolicy]) -> str:
    """Run a failing gate's observe command under gate authority; never raises.

    The observer is diagnostic only — the eyes for the retry loop, never a
    verdict. It borrows the gate policy's allowlist with a shorter leash
    (``OBSERVER_TIMEOUT_SECONDS`` timeout, output capped at ``OBSERVER_CAP``).
    Any problem — rejection, timeout, missing tool, even a broken policy —
    logs a warning and contributes nothing; the gate failure stands untouched.
    """
    try:
        observer_policy = replace(policy or VerificationPolicy(),
                                  timeout_seconds=OBSERVER_TIMEOUT_SECONDS,
                                  tail_chars=OBSERVER_CAP)
        result = observer_policy.run(command, cwd=workspace)
        if result.error:
            logger.warning("observer could not run: %s (%s)", command, result.error)
            return ""
        observed = result.stdout_tail or ""
        stderr = (result.stderr_tail or "").strip()
        if stderr:
            observed = f"{observed}\n[stderr] {stderr}" if observed else f"[stderr] {stderr}"
        return observed[:OBSERVER_CAP]
    except Exception as exc:  # noqa: BLE001 - the observer must never fail the run
        logger.warning("observer failed (gate verdict unchanged): %s: %s", command, exc)
        return ""


def run_gates(specs, workspace: str, policy: Optional[VerificationPolicy] = None) -> GatesResult:
    """Run gates in order, fail-fast; per-gate proof comes from each spec.

    Gates execute in the order given and the run stops at the first failure —
    later gates do not run and leave no records. Each spec's ``prove`` flag
    decides whether that gate gets proof-of-test. An empty spec list yields a
    failed result (never a vacuous pass): the empty gates list in ``to_dict()``
    IS the explanation.

    A FAILING gate with an ``observe`` command additionally runs the observer
    (after the failure, before the fail-fast break) and stores its capped
    output on the failing record's ``observed`` — retry context for builders
    that cannot see the browser. Observer problems never change the verdict.

    Args:
        specs: Ordered ``GateSpec`` instances (name/command/prove).
        workspace: The git working tree the gates run in.
        policy: Verification policy threaded to every gate; a default is used
            by each gate run when omitted.

    Returns:
        A ``GatesResult`` with one record per gate that ran, in order.
    """
    result = GatesResult()
    for spec in specs:
        # The expectation is passed ONLY when the gate declares one, so a gate
        # without it calls exactly the signature every existing caller uses.
        expectation = ({"expect": spec.expect, "min_count": spec.min_count}
                       if getattr(spec, "expect", "") else {})
        record = run_gate(spec.name, spec.command, workspace,
                          with_proof=spec.prove, policy=policy, **expectation)
        result.records.append(record)
        if not record.passed:
            observe = getattr(spec, "observe", "")
            if observe:
                record.observed = _run_observer(observe, workspace, policy)
            break
    return result
