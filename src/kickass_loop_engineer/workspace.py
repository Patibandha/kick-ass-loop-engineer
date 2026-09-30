"""Isolated workspace where the loop materializes generated files.

Two ways work arrives, both ending under the same enforced ``WritePolicy``
(containment, protected paths, size and count caps):

* **FILE blocks** — a blind builder returns fenced blocks; :meth:`Workspace.parse`
  extracts them and :meth:`Workspace.apply` writes the allowed ones.
* **In-place edits** — an agentic builder edits the directory itself with its own
  tools. :meth:`Workspace.fingerprint` records a digest per changed file BEFORE
  the turn, and :meth:`Workspace.harvest` diffs against it afterwards, accepting
  what the policy allows and REVERTING what it does not. A builder that commits
  its own work would hide it from that diff, so :meth:`Workspace.uncommit_to`
  rewinds to the pre-turn :meth:`Workspace.head` first. The guardrails are
  enforced in code either way: an agent's prompt-level promise not to touch
  ``.env`` is never the thing that protects ``.env``.

The module also produces a compact snapshot of the current files for review.

File block format the builder is instructed to emit:

    === FILE: relative/path.py ===
    <file contents>
    === END FILE ===
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from .guardrails import WritePolicy

logger = logging.getLogger("kickass_loop_engineer.workspace")

#: Wall-clock cap for the git plumbing calls behind fingerprint/harvest. These
#: are metadata reads on a single repository, so a minute is already generous.
_GIT_TIMEOUT_SECONDS = 60

#: Chunk size for hashing a file without loading it whole into memory.
_HASH_CHUNK_BYTES = 65536


class WorkspaceError(RuntimeError):
    """Raised when the workspace cannot be inspected or restored.

    Carries git failures (non-zero exit, timeout, OS error) out of
    :meth:`Workspace.fingerprint`: an empty fingerprint would silently mean
    "nothing changed", which would hand an agentic builder a free pass, so the
    failure is raised instead of swallowed. :meth:`Workspace.uncommit_to`
    raises it for the same reason, plus one of its own: a ``HEAD`` that moved
    somewhere unexpected is never rewound on a guess.
    """

_FILE_BLOCK = re.compile(
    r"===\s*FILE:\s*(?P<path>.+?)\s*===\n(?P<body>.*?)\n===\s*END FILE\s*===",
    re.DOTALL,
)

_CODE_FENCE = re.compile(r"^(?:`{3,}|~{3,})[\w.+-]*\s*$")


def _strip_wrapping_fence(body: str) -> str:
    """Drop one markdown code-fence pair wrapping an entire block body.

    Builders occasionally wrap a FILE block's contents in ``` fences despite
    the format instructions; written literally the fence corrupts the file
    (e.g. a pyproject.toml whose line 1 is ```toml is invalid TOML). Strips
    ONLY a matched pair — first line an opening fence, last non-blank line a
    closing fence — and leaves any other body untouched.
    """
    lines = body.split("\n")
    if len(lines) >= 2 and _CODE_FENCE.match(lines[0]):
        end = len(lines) - 1
        while end > 0 and not lines[end].strip():
            end -= 1
        if end > 0 and _CODE_FENCE.match(lines[end]):
            return "\n".join(lines[1:end])
    return body


@dataclass
class ParsedFile:
    """A single file parsed from builder output.

    Attributes:
        path: Workspace-relative destination path.
        content: File contents to write.
    """

    path: str
    content: str


@dataclass
class WriteOutcome:
    """Result of applying builder output to the workspace.

    Attributes:
        written: Relative paths successfully written this call.
        rejected: ``(path, reason)`` pairs blocked by the write policy.
    """

    written: list = field(default_factory=list)
    rejected: list = field(default_factory=list)


@dataclass
class Workspace:
    """A sandboxed directory for generated files, guarded by a write policy.

    Attributes:
        root: Absolute path to the workspace root.
        policy: Write policy enforced on every file.
        written: Relative paths written so far during the run.
    """

    root: str
    policy: Optional[WritePolicy] = None
    written: set = field(default_factory=set)

    def __post_init__(self) -> None:
        """Resolve the root to an absolute path and ensure it exists."""
        self.root = os.path.abspath(self.root)
        self.policy = self.policy or WritePolicy()
        os.makedirs(self.root, exist_ok=True)

    @staticmethod
    def parse(text: str) -> list[ParsedFile]:
        """Extract file blocks from builder output text."""
        return [
            ParsedFile(path=m.group("path").strip(),
                       content=_strip_wrapping_fence(m.group("body")))
            for m in _FILE_BLOCK.finditer(text or "")
        ]

    def apply(self, text: str) -> WriteOutcome:
        """Parse builder output and write each allowed file into the workspace.

        Args:
            text: Builder output containing zero or more file blocks.

        Returns:
            A ``WriteOutcome`` listing written and policy-rejected files.
        """
        outcome = WriteOutcome()
        parsed = self.parse(text)
        if len(parsed) > self.policy.max_files_per_round:
            parsed = parsed[: self.policy.max_files_per_round]
            outcome.rejected.append(("(extra files)", "exceeded max files per round"))

        for item in parsed:
            ok, reason = self.policy.check_path(item.path)
            if not ok:
                logger.warning("rejected %s: %s", item.path, reason)
                outcome.rejected.append((item.path, reason))
                continue
            ok, reason = self.policy.check_content(item.path, item.content)
            if not ok:
                outcome.rejected.append((item.path, reason))
                continue

            target = os.path.abspath(os.path.join(self.root, item.path))
            if os.path.commonpath([self.root, target]) != self.root:
                outcome.rejected.append((item.path, "resolves outside workspace"))
                continue
            if os.path.exists(target) and not self.policy.allow_overwrite:
                outcome.rejected.append((item.path, "overwrite not permitted"))
                continue

            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "w", encoding="utf-8") as handle:
                    handle.write(item.content)
            except OSError as exc:
                logger.error("failed writing %s: %s", item.path, exc)
                outcome.rejected.append((item.path, f"write error: {exc}"))
                continue

            self.written.add(item.path)
            outcome.written.append(item.path)
        return outcome

    def fingerprint(self) -> dict:
        """Return ``{relative path: sha256 hex}`` for every changed file.

        "Changed" means what ``git status --porcelain --untracked-files=all``
        reports: tracked modifications and untracked additions, which is
        exactly the set the worktree seeder and the promoter already agree on.
        Run artifacts under ``.loop-engineer/`` are excluded, symlinks and
        non-files are skipped (a link out of the workspace must never be
        followed), and a rename is recorded at its NEW path.

        Taken before an in-place builder's turn, the result is the baseline
        :meth:`harvest` diffs against afterwards.

        Returns:
            A mapping of workspace-relative path to the sha256 hex digest of
            the file's bytes. Empty only when nothing has changed.

        Raises:
            WorkspaceError: When git cannot be run or reports a failure — an
                empty fingerprint must never be mistaken for "no changes".
        """
        digests: dict = {}
        for rel in self._changed_paths():
            path = os.path.join(self.root, rel)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            digest = self._digest(path)
            if digest is not None:
                digests[rel] = digest
        return digests

    def harvest(self, before: dict) -> WriteOutcome:
        """Adopt the files an in-place builder changed since *before*.

        Every path whose digest is new or differs from *before* is a
        candidate. Candidates run the SAME write policy the FILE-block path
        enforces (``check_path`` then ``check_content``); a violation is
        REVERTED on disk — ``git checkout --`` for a tracked file, deletion
        for an untracked one — and reported in ``rejected``, so a guardrail
        breach cannot survive into the gates or the promotion.

        Exceeding ``max_files_per_round`` keeps every survivor and records an
        ``("(extra files)", ...)`` rejection instead of dropping some: the
        orchestrator's escalation cap is what decides blast radius, and
        reverting an arbitrary subset of an agent's coherent change would
        leave the tree in a state that compiles for nobody.

        Args:
            before: The fingerprint taken before the builder's turn.

        Returns:
            A ``WriteOutcome`` whose ``written`` lists the surviving paths
            (sorted) and whose ``rejected`` carries ``(path, reason)`` pairs.

        Raises:
            WorkspaceError: When the post-turn fingerprint cannot be taken.
        """
        outcome = WriteOutcome()
        after = self.fingerprint()
        candidates = sorted(rel for rel, digest in after.items()
                            if before.get(rel) != digest)

        survivors: list = []
        for rel in candidates:
            ok, reason = self.policy.check_path(rel)
            if ok:
                ok, reason = self._check_size(rel)
            if not ok:
                logger.warning("reverting %s: %s", rel, reason)
                self._revert(rel)
                outcome.rejected.append((rel, reason))
                continue
            survivors.append(rel)

        cap = self.policy.max_files_per_round
        if len(survivors) > cap:
            outcome.rejected.append(
                ("(extra files)",
                 f"exceeded max files per round: {len(survivors)} > {cap}"))

        self.written.update(survivors)
        outcome.written = sorted(survivors)
        return outcome

    def head(self) -> str:
        """Return the commit sha ``HEAD`` currently points at.

        Recorded before an in-place builder's turn, it is the baseline
        :meth:`uncommit_to` rewinds to when the builder commits its own work.

        Returns:
            The full sha of the current ``HEAD``.

        Raises:
            WorkspaceError: When git cannot be run or reports a failure (no
                repository, an unborn branch, a timeout).
        """
        return self._git("rev-parse", "HEAD").stdout.strip()

    def uncommit_to(self, base_sha: str) -> int:
        """Rewind commits an in-place builder made inside the worktree.

        A builder that runs ``git commit`` hides its own work from the engine:
        :meth:`harvest` diffs ``git status`` fingerprints, so a committed change
        reads as "no files changed" (and earns a pointless no-edit retry), and
        proof-of-test cannot make the gate go red by stashing a change that
        already lives in ``HEAD``. A soft reset back to *base_sha* followed by
        an unstage puts the identical bytes back where the engine expects them —
        ordinary modifications and untracked files in ``git status`` — without
        altering a single line of the builder's work.

        Args:
            base_sha: The ``HEAD`` recorded before the builder's turn.

        Returns:
            How many commits were undone; ``0`` when ``HEAD`` never moved.

        Raises:
            WorkspaceError: When git fails, or when ``HEAD`` moved to a commit
                *base_sha* is NOT an ancestor of — a rebase, a branch switch, a
                history rewrite. Rewinding there could discard real history, so
                the engine stops and says so rather than guessing.
        """
        current = self.head()
        if current == base_sha:
            return 0
        if not self._is_ancestor(base_sha, current):
            raise WorkspaceError(
                f"workspace HEAD moved from {base_sha} to {current}, which is "
                f"not a descendant of it (a rebase or branch switch in "
                f"{self.root!r}); refusing to rewind the worktree")
        count = self._commit_count(base_sha, current)
        logger.warning(
            "builder committed %d commit(s) in the worktree; un-committing so "
            "harvest and proof-of-test see the change", count)
        self._git("reset", "--soft", base_sha)
        self._git("reset", "-q")
        return count

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        """Run one git command in the workspace, raising on any failure.

        Args:
            *args: The git subcommand and its arguments.

        Returns:
            The completed process, with stdout captured as text.

        Raises:
            WorkspaceError: On a non-zero exit, a timeout, or an OS error.
        """
        try:
            return subprocess.run(
                ["git", "-C", self.root, *args], check=True, capture_output=True,
                text=True, timeout=_GIT_TIMEOUT_SECONDS)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip() or str(exc)
            raise WorkspaceError(
                f"git {args[0]} failed in {self.root!r}: {detail}") from exc
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise WorkspaceError(
                f"git {args[0]} failed in {self.root!r}: {exc}") from exc

    def _is_ancestor(self, candidate: str, commit: str) -> bool:
        """Return True when *candidate* is reachable from *commit*.

        ``git merge-base --is-ancestor`` answers through its exit status: ``0``
        yes, ``1`` no. Any other status is a real failure (an unknown object, a
        broken repository) and is raised rather than read as "no".

        Raises:
            WorkspaceError: When git could not answer the question.
        """
        try:
            completed = subprocess.run(
                ["git", "-C", self.root, "merge-base", "--is-ancestor",
                 candidate, commit],
                check=False, capture_output=True, text=True,
                timeout=_GIT_TIMEOUT_SECONDS)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise WorkspaceError(
                f"could not compare {candidate} with {commit} in "
                f"{self.root!r}: {exc}") from exc
        if completed.returncode in (0, 1):
            return completed.returncode == 0
        detail = (completed.stderr or "").strip() or f"exit {completed.returncode}"
        raise WorkspaceError(
            f"could not compare {candidate} with {commit} in "
            f"{self.root!r}: {detail}")

    def _commit_count(self, base_sha: str, head_sha: str) -> int:
        """Return how many commits *head_sha* carries beyond *base_sha*.

        Raises:
            WorkspaceError: When git fails or answers with a non-number.
        """
        raw = self._git("rev-list", "--count",
                        f"{base_sha}..{head_sha}").stdout.strip()
        try:
            return int(raw)
        except ValueError as exc:
            raise WorkspaceError(
                f"could not count commits {base_sha}..{head_sha} in "
                f"{self.root!r}: unexpected git output {raw!r}") from exc

    def _changed_paths(self) -> list:
        """Return the workspace-relative paths git reports as changed.

        Raises:
            WorkspaceError: On a git failure of any kind.
        """
        command = ["git", "-C", self.root, "-c", "core.quotepath=false",
                   "status", "--porcelain", "--untracked-files=all"]
        try:
            completed = subprocess.run(
                command, check=True, capture_output=True, text=True,
                timeout=_GIT_TIMEOUT_SECONDS)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip() or str(exc)
            raise WorkspaceError(
                f"could not read workspace status in {self.root!r}: {detail}") from exc
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise WorkspaceError(
                f"could not read workspace status in {self.root!r}: {exc}") from exc

        paths: list = []
        for line in completed.stdout.splitlines():
            rel = line[3:].strip().strip('"')
            if " -> " in rel:  # a staged rename: the NEW path is the file on disk
                rel = rel.split(" -> ", 1)[1].strip().strip('"')
            if not rel or rel == ".loop-engineer" or rel.startswith(".loop-engineer/"):
                continue
            paths.append(rel)
        return paths

    @staticmethod
    def _digest(path: str) -> Optional[str]:
        """Return the sha256 hex of a file, or None when it cannot be read."""
        hasher = hashlib.sha256()
        try:
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
                    hasher.update(chunk)
        except OSError as exc:
            logger.warning("could not fingerprint %s: %s", path, exc)
            return None
        return hasher.hexdigest()

    def _check_size(self, rel: str) -> tuple:
        """Return ``(ok, reason)`` from the policy's content check for *rel*.

        The file is read as bytes and decoded with ``errors="replace"`` so a
        binary artifact can never raise here: valid UTF-8 round-trips to its
        exact byte length, and invalid bytes become U+FFFD (3 bytes each),
        which OVER-estimates the size and therefore only ever errs toward
        rejecting.
        """
        path = os.path.join(self.root, rel)
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            return False, f"unreadable: {exc}"
        return self.policy.check_content(rel, raw.decode("utf-8", "replace"))

    def _revert(self, rel: str) -> None:
        """Undo one policy-violating in-place edit, best effort.

        A tracked file is restored from the index (``git checkout --``); an
        untracked one is deleted. A failure is logged at WARNING rather than
        raised: the file is already recorded as rejected, and losing the whole
        round because a single revert failed would be worse than reporting it.
        """
        path = os.path.join(self.root, rel)
        if self._is_tracked(rel):
            try:
                subprocess.run(
                    ["git", "-C", self.root, "checkout", "--", rel],
                    check=True, capture_output=True, text=True,
                    timeout=_GIT_TIMEOUT_SECONDS)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                    OSError) as exc:
                logger.warning("could not restore tracked %s: %s", rel, exc)
            return
        try:
            os.remove(path)
        except OSError as exc:
            logger.warning("could not remove rejected %s: %s", rel, exc)

    def _is_tracked(self, rel: str) -> bool:
        """Return True when git tracks *rel* (unknown paths count as untracked)."""
        try:
            completed = subprocess.run(
                ["git", "-C", self.root, "ls-files", "--error-unmatch", "--", rel],
                check=False, capture_output=True, text=True,
                timeout=_GIT_TIMEOUT_SECONDS)
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("could not classify %s (assuming untracked): %s", rel, exc)
            return False
        return completed.returncode == 0

    def snapshot(self, max_chars_per_file: int = 4000) -> str:
        """Return a text snapshot of current workspace files for review.

        Args:
            max_chars_per_file: Truncate each file's content to this length.

        Returns:
            A concatenated, labeled view of the files written so far.
        """
        if not self.written:
            return "(workspace is empty)"
        parts: list[str] = []
        for rel in sorted(self.written):
            path = os.path.join(self.root, rel)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    body = handle.read(max_chars_per_file)
            except OSError as exc:
                body = f"(unreadable: {exc})"
            parts.append(f"--- {rel} ---\n{body}")
        return "\n\n".join(parts)
