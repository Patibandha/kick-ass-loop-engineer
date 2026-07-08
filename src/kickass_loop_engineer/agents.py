"""Builder and reviewer agents that drive the iterative loop.

The builder produces or revises files toward the objective; the reviewer examines
the result against the completion criteria and returns concrete findings (defects)
with feedback. Each agent wraps its own provider, so they can run on different
models (for example, build with a local Kimi model and review with Claude). The
reviewer does not decide pass/fail — that authority belongs entirely to executable
gates (``verifier.run_gate``); findings feed oscillation detection and the next
build round's feedback.
"""

from __future__ import annotations

from dataclasses import dataclass

from .objective import Objective
from .providers.base import Provider, ProviderResult

BUILDER_SYSTEM = (
    "You are a senior software engineer. Produce complete, working files toward "
    "the goal. Output ONLY file blocks in this exact format, one per file:\n"
    "=== FILE: relative/path.ext ===\n<full file contents>\n=== END FILE ===\n"
    "Write entire files (never diffs). Add docstrings and exception handling. "
    "Do not include commentary outside file blocks."
)

REVIEWER_SYSTEM = (
    "You are a strict code reviewer. Examine the workspace against the completion "
    "criteria and report concrete defects. List each defect on its own line starting "
    "with exactly 'FINDING: '. If there are no defects, write exactly 'NO FINDINGS'. "
    "You do not approve or reject work — executable gates decide that."
)


@dataclass
class Review:
    """A reviewer's findings for one build round.

    Attributes:
        findings: Concrete defects, one per entry (empty when clean).
        feedback: The full reviewer text, passed to the next build round.
        result: The raw provider result, for usage accounting.
    """

    findings: list
    feedback: str
    result: ProviderResult


class Builder:
    """Generates or revises files toward the objective."""

    def __init__(self, provider: Provider, system_prompt: str = BUILDER_SYSTEM) -> None:
        """Store the provider and system prompt.

        Args:
            provider: Model backend used to generate files.
            system_prompt: Role instructions enforcing the file-block format.
        """
        self.provider = provider
        self.system_prompt = system_prompt

    def build(self, objective: Objective, feedback: str = "") -> ProviderResult:
        """Produce a build round for the objective, incorporating feedback."""
        return self.provider.complete(self.system_prompt, objective.builder_brief(feedback))


class Reviewer:
    """Produces findings against the objective's completion criteria; makes no
    pass/fail decision."""

    def __init__(self, provider: Provider, system_prompt: str = REVIEWER_SYSTEM) -> None:
        """Store the provider and system prompt.

        Args:
            provider: Model backend used to review files.
            system_prompt: Role instructions enforcing the FINDING/NO FINDINGS format.
        """
        self.provider = provider
        self.system_prompt = system_prompt

    def review(self, objective: Objective, workspace_snapshot: str) -> Review:
        """Review the current workspace and return concrete findings."""
        result = self.provider.complete(
            self.system_prompt, objective.reviewer_brief(workspace_snapshot)
        )
        prefix_len = len("FINDING:")
        findings = []
        for raw_line in (result.text or "").splitlines():
            line = raw_line.strip()
            if line.upper().startswith("FINDING:"):
                finding = line[prefix_len:].strip()
                if finding:
                    findings.append(finding)
        return Review(findings=findings, feedback=result.text or "", result=result)
