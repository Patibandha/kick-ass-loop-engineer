"""Isolated git worktree manager for parallel loop-engineer agents.

Each agent gets its own worktree — a full checkout of HEAD in a separate
directory — so concurrent file edits cannot collide.
"""

import contextlib
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from typing import Generator, Optional

from .guardrails import WritePolicy
from .workspace import candidate_files, changed_paths, contained, digest_matches, digest_of


class WorktreeError(Exception):
    """Raised when a git worktree operation fails."""


class WorktreeManager:
    """Creates and removes git worktrees rooted at *repo_root*.

    Parameters
    ----------
    repo_root:
        Absolute path to the git repository root.  Must be an initialised
        git repository; otherwise :meth:`create` will raise
        :class:`WorktreeError`.
    base_dir:
        Directory under which worktree checkouts are placed.  Defaults to a
        freshly-created temporary directory so that callers never need to
        clean up the parent themselves.
    """

    def __init__(self, repo_root: str, base_dir: Optional[str] = None) -> None:
        self._repo_root = os.path.abspath(repo_root)
        self._base_dir: str = base_dir if base_dir is not None else tempfile.mkdtemp()
        self._counter: int = 0
        self._git_Baseline: dict[str, str] = {}
        self._init_git_baseline: str = self._git_config_digest()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, name: str) -> str:
        """Add a new detached worktree at HEAD for the given *name*.

        The checkout is placed at ``<base_dir>/<slug>-<n>`` where *slug* is a
        lowercase, non-alphanumeric-replaced-with-dash version of *name* and
        *n* is a monotonically-increasing counter to guarantee uniqueness
        across repeated calls with the same name.

        Parameters
        ----------
        name:
            A human-readable label for the worktree (e.g. ``"frontend"``).

        Returns
        -------
        str
            Absolute path to the newly-created worktree directory.

        Raises
        ------
        WorktreeError
            If the underlying ``git worktree add`` command fails for any
            reason, including *repo_root* not being a git repository.
        """
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "worktree"
        self._counter += 1
        path = os.path.join(self._base_dir, f"{slug}-{self._counter}")
        self._git("worktree", "add", "--detach", path, "HEAD")
        self._seed_uncommitted(os.path.abspath(path))
        wt_path = os.path.abspath(path)
        self._git_Baseline[os.path.normcase(os.path.realpath(wt_path))] = self._git_config_digest()
        return wt_path

    def _seed_uncommitted(self, worktree_path: str) -> None:
        """Copy the workspace's uncommitted changes into a fresh worktree.

        The symmetric counterpart of :meth:`promote`: a promoted winner lands
        in the main workspace as uncommitted files, so a later slice's
        worktree checked out from bare HEAD would not see the modules the
        previous slice just added — and any code importing them fails its
        gate. Seeding applies the same selection promote uses (tracked
        modifications and untracked additions from ``git status --porcelain``,
        never ``.loop-engineer/``, never deletions, never symlinks), so a
        chain of slices builds on the accumulated uncommitted state.

        Best-effort by design: a file that cannot be copied is skipped rather
        than failing worktree creation — the gate then reports honestly
        against whatever state the worktree has.
        """
        try:
            changed = changed_paths(self._repo_root)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return
        for rel in changed:
            if not rel or rel.startswith(".loop-engineer/") or rel == ".loop-engineer":
                continue
            src_path = os.path.join(self._repo_root, rel)
            if os.path.islink(src_path) or not os.path.isfile(src_path):
                continue
            target = os.path.abspath(os.path.join(worktree_path, rel))
            if not contained(worktree_path, target):
                continue
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(src_path, target)
            except OSError:
                continue

    def remove(self, path: str) -> None:
        """Remove a worktree that was previously created by :meth:`create`.

        This is **idempotent**: if the worktree is already gone (e.g. because
        it was removed manually or the directory was cleaned up externally)
        the call succeeds silently.

        Parameters
        ----------
        path:
            Absolute path to the worktree directory as returned by
            :meth:`create`.
        """
        try:
            self._git("worktree", "remove", "--force", path)
        except WorktreeError:
            # Already removed or path unknown — best-effort, swallow.
            pass

    @contextlib.contextmanager
    def worktree(self, name: str) -> Generator[str, None, None]:
        """Context manager that creates a worktree and removes it on exit.

        The worktree is removed even if the body of the ``with`` block
        raises an exception.

        Parameters
        ----------
        name:
            Human-readable label passed through to :meth:`create`.

        Yields
        ------
        str
            Absolute path to the worktree directory.

        Example
        -------
        >>> mgr = WorktreeManager("/path/to/repo")
        >>> with mgr.worktree("frontend") as path:
        ...     # work inside the isolated checkout
        ...     pass
        """
        path = self.create(name)
        try:
            yield path
        finally:
            self.remove(path)

    def promote(self, src_worktree: str, dest_workspace: str,
                expected_digest: Optional[str] = None,
                policy: Optional[WritePolicy] = None) -> list[str]:
        """Copy the winning attempt's changed files into the main workspace.

        Selection, never merging: only files that differ from HEAD in the
        worktree (tracked modifications and untracked additions, as reported
        by ``git status --porcelain``) are copied.  Run artifacts under
        ``.loop-engineer/`` are never promoted, and deletions are never
        propagated.  A git-detected rename is promoted at its new path and the
        old path is left in place — promotion is writes-only by design.

        Parameters
        ----------
        src_worktree:
            Path to the winning worktree, as returned by :meth:`create`.
        dest_workspace:
            Path to the main workspace that should receive the promoted
            files.
        expected_digest:
            The gate verdict's candidate binding (``GatesResult.candidate_digest``).
            When given, the files are read once, their binding is checked against
            it, and exactly the bytes that were checked are written: a candidate
            changed after its gate passed (even back to the same bytes) is refused
            and nothing is promoted.  An empty binding is always refused.

        Returns
        -------
        list[str]
            The promoted paths, relative to *dest_workspace*, sorted.

        Raises
        ------
        WorktreeError
            If ``git status`` fails in *src_worktree*, a promoted path would
            escape *dest_workspace*, a file cannot be copied, or the candidate
            no longer matches *expected_digest*.
        """
        src = os.path.abspath(src_worktree)
        dest = os.path.abspath(dest_workspace)
        write_policy = policy if policy is not None else WritePolicy()
        baseline = self._git_Baseline.get(
            os.path.normcase(os.path.realpath(src)), self._init_git_baseline
        )
        if self._git_config_digest() != baseline:
            raise WorktreeError(
                "refusing to promote: shared .git/config or .git/hooks was modified during the attempt"
            )
        try:
            # Deletions/dirs are never propagated; symlinks are never followed
            # (a link out of the worktree would exfiltrate its target's contents).
            files = candidate_files(src)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip() or str(exc)
            raise WorktreeError(f"could not read worktree status: {detail}") from exc
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise WorktreeError(f"could not read worktree status: {exc}") from exc
        if expected_digest is not None and not digest_matches(expected_digest, digest_of(files)):
            raise WorktreeError("candidate changed after its gate passed; the earlier verdict is "
                                "stale and nothing was promoted (fail closed)")

        for rel, _content, _mtime_ns in files:
            ok, reason = write_policy.check_path(rel)
            if not ok:
                raise WorktreeError(
                    f"refusing to promote path rejected by WritePolicy {rel!r}: {reason}"
                )
            target = os.path.abspath(os.path.join(dest, rel))
            if not contained(dest, target):
                raise WorktreeError(f"promotion path escapes workspace: {rel!r}")

        promoted: list[str] = []
        for rel, content, _mtime_ns in files:
            target = os.path.abspath(os.path.join(dest, rel))
            # Resolved through links: the user's workspace may hold a junction the worktree did not.
            if not contained(dest, target):
                raise WorktreeError(f"promotion path escapes workspace: {rel!r}")
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as handle:
                    handle.write(content)
                shutil.copystat(os.path.join(src, rel), target)
            except OSError as exc:
                raise WorktreeError(f"could not promote {rel!r}: {exc}") from exc
            promoted.append(rel)
        return sorted(promoted)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _git_config_digest(self) -> str:
        """Compute a deterministic SHA-256 digest of ``.git/config`` and ``.git/hooks/``."""
        hasher = hashlib.sha256()
        git_dir = os.path.join(self._repo_root, ".git")
        config_path = os.path.join(git_dir, "config")
        if os.path.isfile(config_path) and not os.path.islink(config_path):
            try:
                with open(config_path, "rb") as handle:
                    data = handle.read()
                hasher.update(b"config\0" + data + b"\0")
            except OSError:
                hasher.update(b"config:unreadable\0")
        hooks_dir = os.path.join(git_dir, "hooks")
        if os.path.isdir(hooks_dir):
            hook_files: list[str] = []
            for root, _dirs, filenames in os.walk(hooks_dir):
                for fname in filenames:
                    full = os.path.join(root, fname)
                    rel = os.path.relpath(full, git_dir).replace("\\", "/")
                    hook_files.append(rel)
            for rel in sorted(hook_files):
                full = os.path.join(git_dir, rel)
                if os.path.islink(full):
                    try:
                        target = os.readlink(full).encode("utf-8", errors="surrogateescape")
                    except OSError:
                        target = b"?"
                    hasher.update(b"symlink:" + rel.encode("utf-8") + b"\0" + target + b"\0")
                elif os.path.isfile(full):
                    try:
                        with open(full, "rb") as handle:
                            data = handle.read()
                        hasher.update(b"hook:" + rel.encode("utf-8") + b"\0" + data + b"\0")
                    except OSError:
                        hasher.update(b"hook:unreadable:" + rel.encode("utf-8") + b"\0")
        return hasher.hexdigest()

    def _git(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        """Run a ``git -C <repo_root> <args>`` command.

        Parameters
        ----------
        *args:
            Arguments forwarded to git after ``-C <repo_root>``.
        timeout:
            Maximum seconds to wait for the command.  Defaults to 60.

        Returns
        -------
        subprocess.CompletedProcess
            The completed process object on success.

        Raises
        ------
        WorktreeError
            On non-zero exit, timeout, or OS-level failure.
        """
        cmd = ["git", "-C", self._repo_root, *args]
        try:
            return subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.CalledProcessError as exc:
            raise WorktreeError(exc.stderr.strip() or str(exc)) from exc
        except subprocess.TimeoutExpired as exc:
            raise WorktreeError(f"git timed out after {timeout}s: {exc}") from exc
        except OSError as exc:
            raise WorktreeError(f"OS error running git: {exc}") from exc
