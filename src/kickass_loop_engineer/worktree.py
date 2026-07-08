"""Isolated git worktree manager for parallel loop-engineer agents.

Each agent gets its own worktree — a full checkout of HEAD in a separate
directory — so concurrent file edits cannot collide.
"""

import contextlib
import os
import re
import shutil
import subprocess
import tempfile
from typing import Generator, Optional


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
        return os.path.abspath(path)

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

    def promote(self, src_worktree: str, dest_workspace: str) -> list[str]:
        """Copy the winning attempt's changed files into the main workspace.

        Selection, never merging: only files that differ from HEAD in the
        worktree (tracked modifications and untracked additions, as reported
        by ``git status --porcelain``) are copied.  Run artifacts under
        ``.loop-engineer/`` are never promoted, and deletions are never
        propagated.  Git-detected renames (porcelain ``R old -> new`` lines,
        only possible for staged changes) are skipped rather than followed —
        promotion is writes-only by design.

        Parameters
        ----------
        src_worktree:
            Path to the winning worktree, as returned by :meth:`create`.
        dest_workspace:
            Path to the main workspace that should receive the promoted
            files.

        Returns
        -------
        list[str]
            The promoted paths, relative to *dest_workspace*, sorted.

        Raises
        ------
        WorktreeError
            If ``git status`` fails in *src_worktree*, a promoted path would
            escape *dest_workspace*, or a file cannot be copied.
        """
        src = os.path.abspath(src_worktree)
        dest = os.path.abspath(dest_workspace)
        try:
            out = subprocess.run(
                ["git", "-C", src, "-c", "core.quotepath=false",
                 "status", "--porcelain", "--untracked-files=all"],
                check=True, capture_output=True, text=True, timeout=60,
            ).stdout
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip() or str(exc)
            raise WorktreeError(f"could not read worktree status: {detail}") from exc
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise WorktreeError(f"could not read worktree status: {exc}") from exc

        promoted: list[str] = []
        for line in out.splitlines():
            rel = line[3:].strip().strip('"')
            if not rel or rel.startswith(".loop-engineer/") or rel == ".loop-engineer":
                continue
            src_path = os.path.join(src, rel)
            # Deletions/dirs are never propagated; symlinks are never followed
            # (a link out of the worktree would exfiltrate its target's contents).
            if os.path.islink(src_path) or not os.path.isfile(src_path):
                continue
            target = os.path.abspath(os.path.join(dest, rel))
            if os.path.commonpath([dest, target]) != dest:
                raise WorktreeError(f"promotion path escapes workspace: {rel!r}")
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(src_path, target)
            except OSError as exc:
                raise WorktreeError(f"could not promote {rel!r}: {exc}") from exc
            promoted.append(rel)
        return sorted(promoted)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
