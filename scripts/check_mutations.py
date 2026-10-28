#!/usr/bin/env python3
"""Mutation check: prove the guardrail tests actually catch a weakened guardrail.

For every ``mutations/<family>-<name>.patch`` the script

1. creates a throwaway git worktree of the repository's ``HEAD``,
2. applies the patch (a patch that no longer applies is a FAILURE),
3. runs the guardrail marker that belongs to the patch family and requires the tests to FAIL
   (pytest exit code 1; a clean pass, a collection error or "no tests ran" is not a catch),
4. removes the worktree.

It then requires the unmutated ``HEAD`` to pass every marker it used. Exit status is 0 only when every
mutation was caught and the clean run is green; 1 otherwise; 2 on a usage error. Standard library only.

Families map to markers: ``gate-*`` -> ``guardrail_gate``, ``isolation-*`` -> ``guardrail_isolation``,
``proof-*`` -> ``guardrail_proof`` and ``proof-transition-*`` -> ``guardrail_proof_transition`` (the longest
matching family wins). The mutations are applied to the committed ``HEAD``, so commit (or
otherwise record) the code under test before running the check. Nothing from the environment is printed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

#: Patch-name prefix -> pytest marker expression that must fail under that mutation.
FAMILY_MARKERS = {
    "gate": "guardrail_gate",
    "isolation": "guardrail_isolation and not guardrail_isolation_capability",
    "proof": "guardrail_proof",
    "proof-transition": "guardrail_proof_transition",
}

CAUGHT = "caught"
SURVIVED = "survived"
NOT_APPLIED = "not-applied"
ERROR = "error"

#: pytest exit code for "some tests failed": the only outcome that counts as a mutation being caught.
PYTEST_TESTS_FAILED = 1

DEFAULT_TIMEOUT = 900.0

#: Line endings and filters must not depend on the host, or a patch applies on Linux and not on Windows.
_GIT = ("git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false")


class MutationCheckError(RuntimeError):
    """Raised when the check cannot be set up (no repository, no patches, an unknown patch family)."""


@dataclass
class MutationResult:
    """Verdict for one mutation patch.

    Attributes:
        patch: File name of the patch.
        marker: Pytest marker expression that was run.
        status: One of ``caught``, ``survived``, ``not-applied`` or ``error``.
        detail: Short human-readable explanation.
    """

    patch: str
    marker: str
    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        """True only when the mutation was caught."""
        return self.status == CAUGHT


@dataclass
class CleanResult:
    """Verdict for the unmutated tree under one marker expression."""

    marker: str
    passed: bool
    detail: str = ""


def _run(argv: Sequence[str], cwd: str, timeout: float,
         env: Optional[dict] = None) -> subprocess.CompletedProcess:
    """Run *argv* in *cwd*, capturing text output.

    Raises:
        subprocess.TimeoutExpired: When the command outlives *timeout*.
        OSError: When the program cannot be started.
    """
    return subprocess.run(list(argv), cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=timeout, check=False, stdin=subprocess.DEVNULL)


def _tail(completed: subprocess.CompletedProcess, limit: int = 300) -> str:
    """Last *limit* characters of a process's combined output, squeezed onto one line."""
    text = ((completed.stdout or "") + (completed.stderr or "")).strip()
    return " ".join(text[-limit:].split())


def _force_remove(func, path, _exc_info) -> None:
    """``shutil.rmtree`` error hook: clear a read-only bit (git object files on Windows) and retry."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


class Scratch:
    """A throwaway detached worktree of ``HEAD``; removed on exit even when the body raises."""

    def __init__(self, repo: str, timeout: float) -> None:
        self.repo = repo
        self.timeout = timeout
        self.parent = ""
        self.path = ""

    def __enter__(self) -> "Scratch":
        self.parent = tempfile.mkdtemp(prefix="mutcheck-")
        self.path = os.path.join(self.parent, "wt")
        try:
            added = _run([*_GIT, "worktree", "add", "--detach", "--quiet", self.path, "HEAD"],
                         self.repo, self.timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._cleanup()
            raise MutationCheckError(f"cannot create a worktree: {exc}") from exc
        if added.returncode != 0:
            detail = _tail(added)
            self._cleanup()
            raise MutationCheckError(f"cannot create a worktree: {detail}")
        return self

    def __exit__(self, *_exc) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        """Unregister the worktree from git and delete its folder."""
        if self.path and os.path.isdir(self.path):
            try:
                _run([*_GIT, "worktree", "remove", "--force", self.path], self.repo, self.timeout)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if self.parent:
            shutil.rmtree(self.parent, onerror=_force_remove)
        try:
            _run([*_GIT, "worktree", "prune"], self.repo, self.timeout)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _pytest_env() -> dict:
    """Environment for the test run: the host's, minus a ``PYTHONPATH`` that could shadow the worktree."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_marker(worktree: str, marker: str, timeout: float) -> subprocess.CompletedProcess:
    """Run the tests selected by *marker* inside *worktree*."""
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", marker]
    return _run(argv, worktree, timeout, env=_pytest_env())


def marker_for(patch: Path) -> str:
    """The marker expression for *patch*, from its ``<family>-`` name prefix (the longest family that matches).

    Raises:
        MutationCheckError: When the prefix is not a known family.
    """
    families = [family for family in FAMILY_MARKERS if patch.name.startswith(family + "-")]
    if not families:
        raise MutationCheckError(
            f"{patch.name}: name must start with one of {', '.join(f + '-' for f in FAMILY_MARKERS)}")
    return FAMILY_MARKERS[max(families, key=len)]


def check_mutation(repo: str, patch: Path, timeout: float) -> MutationResult:
    """Apply *patch* in a fresh worktree and require the family's guardrail tests to fail."""
    marker = marker_for(patch)
    try:
        with Scratch(repo, timeout) as scratch:
            applied = _run([*_GIT, "apply", "--whitespace=nowarn", str(patch.resolve())],
                           scratch.path, timeout)
            if applied.returncode != 0:
                return MutationResult(patch.name, marker, NOT_APPLIED, _tail(applied))
            completed = run_marker(scratch.path, marker, timeout)
    except subprocess.TimeoutExpired:
        return MutationResult(patch.name, marker, ERROR, f"timed out after {timeout:.0f}s")
    except (OSError, MutationCheckError) as exc:
        return MutationResult(patch.name, marker, ERROR, str(exc))
    if completed.returncode == PYTEST_TESTS_FAILED:
        return MutationResult(patch.name, marker, CAUGHT, "guardrail tests failed as required")
    if completed.returncode == 0:
        return MutationResult(patch.name, marker, SURVIVED, "guardrail tests still pass: not caught")
    return MutationResult(patch.name, marker, ERROR,
                          f"pytest exited {completed.returncode} (not a test failure): {_tail(completed)}")


def check_clean(repo: str, markers: Sequence[str], timeout: float) -> list[CleanResult]:
    """Run each marker on an unmutated worktree of ``HEAD`` and require a pass."""
    results: list[CleanResult] = []
    try:
        with Scratch(repo, timeout) as scratch:
            for marker in markers:
                try:
                    completed = run_marker(scratch.path, marker, timeout)
                except subprocess.TimeoutExpired:
                    results.append(CleanResult(marker, False, f"timed out after {timeout:.0f}s"))
                    continue
                except OSError as exc:
                    results.append(CleanResult(marker, False, str(exc)))
                    continue
                passed = completed.returncode == 0
                results.append(CleanResult(
                    marker, passed, "" if passed else f"pytest exited {completed.returncode}: {_tail(completed)}"))
    except (OSError, MutationCheckError) as exc:
        return [CleanResult(marker, False, str(exc)) for marker in markers]
    return results


def find_patches(mutations_dir: Path) -> list[Path]:
    """Every ``*.patch`` under *mutations_dir*, in name order.

    Raises:
        MutationCheckError: When the folder is missing or holds no patches.
    """
    if not mutations_dir.is_dir():
        raise MutationCheckError(f"mutations folder not found: {mutations_dir}")
    patches = sorted(mutations_dir.glob("*.patch"))
    if not patches:
        raise MutationCheckError(f"no *.patch files in {mutations_dir}")
    return patches


def run_check(repo: str, mutations_dir: Path, timeout: float = DEFAULT_TIMEOUT, out=None) -> bool:
    """Run the whole check, printing one line per verdict; True when everything is green."""
    out = out or sys.stdout
    patches = find_patches(mutations_dir)
    markers = [marker_for(p) for p in patches]
    ok = True
    for patch in patches:
        result = check_mutation(repo, patch, timeout)
        ok &= result.ok
        print(f"[{result.status.upper():11}] {result.patch}: {result.detail}", file=out, flush=True)
    for clean in check_clean(repo, list(dict.fromkeys(markers)), timeout):
        ok &= clean.passed
        label = "CLEAN-OK" if clean.passed else "CLEAN-FAIL"
        print(f"[{label:11}] {clean.marker}: {clean.detail}", file=out, flush=True)
    print("all mutations caught and the clean run is green" if ok else "mutation check FAILED",
          file=out, flush=True)
    return ok


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command-line entry point; returns the process exit status."""
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Check that the guardrail tests catch each mutation.")
    parser.add_argument("--repo", default=str(root), help="git repository to test (default: this one)")
    parser.add_argument("--mutations", default=None, help="folder of *.patch files (default: <repo>/mutations)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="seconds allowed per git or pytest command")
    args = parser.parse_args(argv)
    repo = str(Path(args.repo).resolve())
    mutations = Path(args.mutations) if args.mutations else Path(repo) / "mutations"
    try:
        return 0 if run_check(repo, mutations, args.timeout) else 1
    except MutationCheckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
