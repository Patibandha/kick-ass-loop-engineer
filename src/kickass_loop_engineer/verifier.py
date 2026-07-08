"""The verifier: gates return evidence, and a passing gate is proven real.

Proof-of-test runs a gate three times around the working-tree change:
    green_before  — gate passes WITH the change in place
    red_when_reverted — gate FAILS once the change is stashed away
    green_after   — gate passes again after restoring the change
A gate counts as proven only when all three hold. This defeats "fake done":
a test that passes regardless of the change is not evidence of anything.

Verdicts come from real artifacts in the workspace (process exit codes), never
from a model's transcript:
    "Use the worktree and external state as authoritative. Do not rely on intent,
     memory of earlier work, or a plausible final answer as proof of completion."
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Optional

from .gates import GATES
from .guardrails import VerificationPolicy

ARTIFACTS_NOT_TRANSCRIPT = (
    "Use the worktree and external state as authoritative. Do not rely on intent, "
    "partial progress, memory of earlier work, or a plausible final answer as proof "
    "of completion."
)


@dataclass
class ProofRecord:
    """Evidence that a gate's pass is real (revert -> red -> green)."""

    gate_cmd: str
    green_before: bool = False
    red_when_reverted: bool = False
    green_after: bool = False
    error: str = ""
    aborted: bool = False

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


def prove(gate_cmd: str, workspace: str, policy: Optional[VerificationPolicy] = None) -> ProofRecord:
    """Prove a gate's pass is real by stashing the working-tree change.

    Runs the gate green->red->green around a ``git stash`` of the change. On a
    pop failure the change is NOT lost — it remains in the stash, and the record
    says so explicitly (``aborted=True``) so the caller can recover rather than
    silently proceeding on a corrupted tree.

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
        if not _git(workspace, "status", "--porcelain").stdout.strip():
            record.error = "proof-of-test requires an uncommitted change to revert"
            return record
        _git(workspace, "stash", "push", "--include-untracked")
        try:
            record.red_when_reverted = not _gate_passes(gate_cmd, workspace, policy)
        finally:
            # Discard anything the reverted run modified in tracked files so the
            # stash restores cleanly, then pop. If pop still fails, the change is
            # safe in the stash and we say so loudly instead of corrupting the tree.
            try:
                _git(workspace, "checkout", "--", ".")
            except subprocess.CalledProcessError:
                pass
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

    def to_dict(self) -> dict:
        """Return a JSON-serializable representation."""
        d = {"gate": self.gate, "command": self.command, "passed": self.passed,
             "evidence": self.evidence}
        if self.proof is not None:
            d["proof"] = {"proven": self.proof.proven, "green_before": self.proof.green_before,
                          "red_when_reverted": self.proof.red_when_reverted,
                          "green_after": self.proof.green_after, "aborted": self.proof.aborted,
                          "error": self.proof.error}
        return d


def run_gate(name: str, command: str, workspace: str, with_proof: bool = False,
             policy: Optional[VerificationPolicy] = None) -> EvidenceRecord:
    """Run a named gate and return an evidence record built from real output.

    A gate is ``passed`` only if the command exits zero (and, when ``with_proof``,
    proof-of-test holds). An errored or rejected command is never ``passed``.
    """
    if name not in GATES:
        return EvidenceRecord(
            gate=name, command=command, passed=False,
            evidence=f"error: unknown gate {name!r}; known gates: {sorted(GATES)}",
        )
    policy = policy or VerificationPolicy()
    result = policy.run(command, cwd=workspace)
    evidence = f"exit={result.returncode}; {(result.stdout_tail or result.stderr_tail or '').strip()[-200:]}"
    if result.error:
        evidence = f"error: {result.error}"
    record = EvidenceRecord(gate=name, command=command, passed=result.passed, evidence=evidence)
    if with_proof and result.passed:
        record.proof = prove(command, workspace, policy)
        record.passed = record.proof.proven
    return record
