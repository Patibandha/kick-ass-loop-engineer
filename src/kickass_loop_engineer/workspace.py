"""Isolated workspace where the loop materializes generated files.

Builder agents return their work as fenced file blocks; this module parses those
blocks and writes them into a sandboxed directory under an enforced ``WritePolicy``
(containment, protected paths, size and count caps). It also produces a compact
snapshot of the current files for review.

File block format the builder is instructed to emit:

    === FILE: relative/path.py ===
    <file contents>
    === END FILE ===
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from .guardrails import WritePolicy

logger = logging.getLogger("kickass_loop_engineer.workspace")

_FILE_BLOCK = re.compile(
    r"===\s*FILE:\s*(?P<path>.+?)\s*===\n(?P<body>.*?)\n===\s*END FILE\s*===",
    re.DOTALL,
)


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
            ParsedFile(path=m.group("path").strip(), content=m.group("body"))
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
