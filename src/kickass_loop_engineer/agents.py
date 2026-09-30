"""Builder and reviewer agents that drive the iterative loop.

The builder works one of two ways, decided by the provider it wraps:

* A BLIND provider answers with ``=== FILE: ... ===`` blocks that the engine
  parses and writes (``Builder.build`` + ``BUILDER_SYSTEM``).
* An AGENTIC provider (``provider.edits_in_place``) is handed the workspace
  directory and edits it with its own tools (``Builder.build_in_place`` +
  ``AGENTIC_BUILDER_SYSTEM``); the engine then harvests whatever changed and
  applies the same write guardrails to it.

The builder produces or revises files toward the objective; the reviewer examines
the result against the completion criteria and returns concrete findings (defects)
with feedback. Each agent wraps its own provider, so they can run on different
models (for example, build with a local Kimi model and review with Claude). The
reviewer does not decide pass/fail — that authority belongs entirely to executable
gates (``verifier.run_gate``); findings feed oscillation detection and the next
build round's feedback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from .objective import Objective
from .providers.base import Provider, ProviderError, ProviderResult

logger = logging.getLogger("kickass_loop_engineer.agents")

_CHARS_PER_TOKEN = 4  # rough prompt-size heuristic; exact tokenizers are model-specific
_CONTEXT_WARN_RATIO = 0.75

BUILDER_SYSTEM = (
    "You are a senior software engineer. Produce complete, working files toward "
    "the goal. Output ONLY file blocks in this exact format, one per file:\n"
    "=== FILE: relative/path.ext ===\n<full file contents>\n=== END FILE ===\n"
    "Write entire files (never diffs). Add docstrings and exception handling. "
    "Do not include commentary outside file blocks."
)

# Appended to BUILDER_SYSTEM (by config.build_builder) ONLY when a `ui` gate is
# configured — non-UI runs keep the system prompt byte-identical.
UI_BUILDER_RULES = (
    "UI RULES (a browser `ui` gate verifies this work with Playwright + axe-core):\n"
    "- Use semantic HTML with accessible names (labels, alt text, landmark roles); "
    "the automated axe-core accessibility check must pass.\n"
    "- Give key interactive elements stable selectors (data-testid attributes or "
    "unique roles/text) so DOM assertions can target them.\n"
    "- The page must load without console errors — surface failures in code, "
    "never as console noise.\n"
    "- Keep the app reachable at the URL the ui gate tests (default "
    "http://localhost:3000) with a single dev-server command."
)

AGENTIC_BUILDER_SYSTEM = (
    "You are a senior software engineer working INSIDE a git worktree: your "
    "current working directory IS the workspace. Implement the goal by reading "
    "the repository and editing files directly with your tools. Write complete "
    "files with docstrings and typed exception handling; write or update tests "
    "first and run the project's test, lint and type commands named in the "
    "brief before you finish. Never touch .git, .env, secrets, credentials, or "
    "anything outside the working directory. Do NOT print file blocks or diffs. "
    "Finish with a short summary that lists every file you created or modified. "
    "Never run git commit, git stash, git reset or git checkout: leave every "
    "change uncommitted in the working tree — the engine harvests, verifies and "
    "promotes it."
)

REVIEWER_SYSTEM = (
    "You are a strict code reviewer. Examine the workspace against the completion "
    "criteria and report concrete defects. List each defect on its own line starting "
    "with exactly 'FINDING: '. If there are no defects, write exactly 'NO FINDINGS'. "
    "You do not approve or reject work — executable gates decide that."
)

REVIEWER_FORMAT_REMINDER = (
    "FORMAT ERROR: your previous reply matched neither format. List each defect "
    "as 'FINDING: <defect>' on its own line, or reply exactly 'NO FINDINGS'."
)


def parse_findings(text: str) -> list:
    """Extract ``FINDING:`` lines from reviewer text.

    The shared parser behind every ``FINDING:`` consumer (``Reviewer.review``,
    the visual critique, the audit sweep). Lines are stripped before matching,
    so indented findings parse; the prefix matches case-insensitively; a bare
    ``FINDING:`` with no defect text is ignored.

    Args:
        text: Raw reviewer reply (may be empty or None).

    Returns:
        The non-empty defect descriptions, one per ``FINDING:`` line.
    """
    prefix_len = len("FINDING:")
    findings = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if line.upper().startswith("FINDING:"):
            finding = line[prefix_len:].strip()
            if finding:
                findings.append(finding)
    return findings


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

    def __init__(
        self,
        provider: Provider,
        system_prompt: str = BUILDER_SYSTEM,
        context_window: int = 0,
    ) -> None:
        """Store the provider, system prompt, and context-window budget.

        Args:
            provider: Model backend used to generate files.
            system_prompt: Role instructions enforcing the file-block format.
            context_window: The model's context window in tokens, used only to
                WARN when a round's prompt approaches it (see :meth:`build`).
                ``0`` (the default, and the value for models with no known
                window) disables the check.
        """
        self.provider = provider
        self.system_prompt = system_prompt
        self.context_window = context_window

    def build(self, objective: Objective, feedback: str = "") -> ProviderResult:
        """Produce a build round for the objective, incorporating feedback.

        The FILE-block path, for providers that cannot touch the filesystem:
        the reply is text the engine parses and writes.

        When ``context_window`` is set and the estimated prompt size —
        ``(len(system_prompt) + len(brief)) // 4``, a chars-per-token
        heuristic — exceeds 75% of it, a WARNING naming the model, the
        estimate, and the window is logged before the call. The call itself
        always proceeds: accumulated feedback can legitimately grow large and
        truncation behavior is the model's, not the engine's, to decide.

        Args:
            objective: The goal and completion criteria to build toward.
            feedback: Reviewer feedback from the previous round, if any.

        Returns:
            The provider's result for this round.
        """
        brief = objective.builder_brief(feedback)
        self._warn_if_prompt_crowds_context(self.system_prompt, brief)
        return self.provider.complete(self.system_prompt, brief)

    def build_in_place(self, objective: Objective, feedback: str = "", *,
                       cwd: str) -> ProviderResult:
        """Let an agentic provider implement the objective inside *cwd*.

        The AGENTIC path, for providers that declare ``edits_in_place``: the
        provider edits the directory with its own tools and returns a prose
        summary, which the engine keeps only for the audit trail — the files
        it actually changed are discovered by diffing the directory.

        The system prompt is :data:`AGENTIC_BUILDER_SYSTEM`, plus
        :data:`UI_BUILDER_RULES` when this builder's configured prompt carries
        them (a ui gate is in play). Non-UI runs stay byte-identical to the
        bare agentic prompt.

        Args:
            objective: The goal and completion criteria to build toward.
            feedback: Reviewer feedback from the previous round, if any.
            cwd: The workspace directory the provider must edit (in the
                pipeline: the attempt's git worktree).

        Returns:
            The provider's result — a summary of the edits it made.

        Raises:
            ProviderError: When the provider cannot edit in place, or when the
                edit turn fails.
        """
        system = AGENTIC_BUILDER_SYSTEM
        if UI_BUILDER_RULES in self.system_prompt:
            system = f"{AGENTIC_BUILDER_SYSTEM}\n\n{UI_BUILDER_RULES}"
        brief = objective.builder_brief(feedback)
        self._warn_if_prompt_crowds_context(system, brief)
        return self.provider.edit(system, brief, cwd=cwd)

    def _warn_if_prompt_crowds_context(self, system: str, brief: str) -> None:
        """WARN when the estimated prompt size passes 75% of the context window.

        Purely advisory: the call always proceeds, because accumulated
        feedback can legitimately grow large and truncation behavior is the
        model's, not the engine's, to decide. A ``context_window`` of 0 (an
        unknown model) disables the check.

        Args:
            system: The system prompt about to be sent.
            brief: The user brief about to be sent.
        """
        if self.context_window <= 0:
            return
        estimated = (len(system) + len(brief)) // _CHARS_PER_TOKEN
        if estimated > self.context_window * _CONTEXT_WARN_RATIO:
            model = getattr(self.provider, "model", None) or self.provider.name
            logger.warning(
                "builder prompt is ~%d tokens, over 75%% of %s's %d-token "
                "context window — the model may truncate or drop context",
                estimated, model, self.context_window)


class Reviewer:
    """Produces findings against the objective's completion criteria; makes no
    pass/fail decision."""

    def __init__(
        self,
        provider: Provider,
        system_prompt: str = REVIEWER_SYSTEM,
        format_retries: int = 1,
        on_retry: Optional[Callable[[], None]] = None,
    ) -> None:
        """Store the provider, system prompt, and retry budget.

        Args:
            provider: Model backend used to review files.
            system_prompt: Role instructions enforcing the FINDING/NO FINDINGS format.
            format_retries: Corrective re-calls allowed per review when the
                reply matches neither ``FINDING:`` nor ``NO FINDINGS``.
            on_retry: Optional zero-arg callback invoked once per corrective
                format retry, just before the retry call is issued (journal
                hook — purely observational, never changes retry behavior).
        """
        self.provider = provider
        self.system_prompt = system_prompt
        self.format_retries = format_retries
        self.on_retry = on_retry

    # Kept as a delegating staticmethod for existing callers; the parser
    # itself is the module-level ``parse_findings``.
    _parse_findings = staticmethod(parse_findings)

    def review(self, objective: Objective, workspace_snapshot: str) -> Review:
        """Review the current workspace and return concrete findings.

        When the reviewer replies with non-empty text that matches neither
        ``FINDING:`` nor ``NO FINDINGS``, up to ``format_retries`` corrective
        calls restate the exact reply format. The LAST response supplies the
        findings and feedback; the token and cost fields on the returned
        ``Review.result`` are the SUM across every call, so a paid retry is
        never lost to cost accounting. An EMPTY response signals a provider
        failure, not a format problem, and is never retried. A still-malformed
        final reply degrades to empty findings rather than raising.

        Args:
            objective: The goal and completion criteria under review.
            workspace_snapshot: Current files produced by the builder.

        Returns:
            A ``Review`` with parsed findings, the last raw reply as feedback,
            and an accumulated provider result for usage accounting.
        """
        brief = objective.reviewer_brief(workspace_snapshot)
        result = self.provider.complete(self.system_prompt, brief)
        tokens = result.tokens
        cost_usd = result.cost_usd
        prompt_tokens = result.prompt_tokens
        completion_tokens = result.completion_tokens
        findings = self._parse_findings(result.text)

        attempts = 0
        while (attempts < self.format_retries
               and (result.text or "").strip()
               and not findings
               and "NO FINDINGS" not in (result.text or "").upper()):
            attempts += 1
            if self.on_retry is not None:
                self.on_retry()
            try:
                result = self.provider.complete(
                    self.system_prompt, f"{brief}\n\n{REVIEWER_FORMAT_REMINDER}"
                )
            except ProviderError as exc:
                # The FIRST call completed and may have cost money; attach the
                # accumulated spend so the caller can still ledger it even
                # though no Review will be returned.
                exc.partial_result = ProviderResult(
                    text="", tokens=tokens, cost_usd=cost_usd,
                    model=result.model, prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens)
                raise
            tokens += result.tokens
            cost_usd += result.cost_usd
            prompt_tokens += result.prompt_tokens
            completion_tokens += result.completion_tokens
            findings = self._parse_findings(result.text)

        accumulated = ProviderResult(
            text=result.text,
            tokens=tokens,
            cost_usd=cost_usd,
            model=result.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        return Review(findings=findings, feedback=result.text or "", result=accumulated)
