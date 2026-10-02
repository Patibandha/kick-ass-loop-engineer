"""kickass_loop_engineer: a model-agnostic, multi-agent build/review loop.

Provide an objective with checkable completion criteria and kickAssLoopEngineer
iterates a builder and a reviewer over an isolated workspace until the criteria
are met or a budget is reached. Builder and reviewer can run on different model
backends (Claude Code, a local Ollama model such as Kimi, or the Anthropic API),
and an agentic backend (Claude Code, Gemini) can edit the workspace directly
while the engine harvests and guards whatever it changed.
"""

from __future__ import annotations

from .agents import Builder, Reviewer, Review
from .artifacts import ARTIFACT_SCHEMAS, validate_artifact
from .auditor import classify_run
from .autonomy import AutonomyLevel, ReadinessChecklist, resolve_level
from .config import build_builder, build_orchestrator, load_config
from .cursor import PipelineCursor
from .decompose import RoleDAG, RoleSlice, generate_slices
from .ensemble import Attempt, select_winner, run_ensemble
from .escalation import should_escalate
from .guardrails import VerificationPolicy, VerificationResult, WritePolicy
from .interview import QUESTION_TEMPLATES, SLOTS, lint_done_when, next_questions
from .ledger import CostLedger
from .memory import Memory
from .next import emit_next
from .notify import Notifier, build_event
from .objective import Objective
from .orchestrator import GateSpec, Orchestrator
from .oscillation import OscillationDetector
from .playbook import Playbook
from .progress import ProgressReporter
from .promptwriter import IdeaSpec, PromptWriter
from .providers import Provider, ProviderError, ProviderResult, build_provider
from .research import (
    Claim,
    CitationResult,
    ResearchBrief,
    TAGS,
    check_citations,
    check_tag_coverage,
    ingest_research,
    parse_research,
    select_citations,
)
from .review import CrossModelReviewer, model_family
from .router import SkillRouter, StageRoute, STAGES
from .session import BuildRound, BuildSession
from .state import RunState
from .terminal import RunOutcome, TerminalState
from .workspace import Workspace, WorkspaceError, WriteOutcome
from .worktree import WorktreeManager

__version__ = "3.2.1"

__all__ = [
    "Objective",
    "Builder",
    "Reviewer",
    "Review",
    "Orchestrator",
    "GateSpec",
    "PipelineCursor",
    "RunOutcome",
    "TerminalState",
    "generate_slices",
    "build_orchestrator",
    "BuildSession",
    "BuildRound",
    "Workspace",
    "WorkspaceError",
    "WriteOutcome",
    "WritePolicy",
    "VerificationPolicy",
    "VerificationResult",
    "ProgressReporter",
    "RunState",
    "Provider",
    "ProviderResult",
    "ProviderError",
    "build_provider",
    "build_builder",
    "load_config",
    "IdeaSpec",
    "PromptWriter",
    "RoleSlice",
    "RoleDAG",
    "WorktreeManager",
    "CostLedger",
    "Notifier",
    "build_event",
    "Attempt",
    "select_winner",
    "run_ensemble",
    "OscillationDetector",
    "model_family",
    "CrossModelReviewer",
    "Memory",
    "Playbook",
    "classify_run",
    "SkillRouter",
    "StageRoute",
    "STAGES",
    "ARTIFACT_SCHEMAS",
    "validate_artifact",
    "emit_next",
    "AutonomyLevel",
    "ReadinessChecklist",
    "resolve_level",
    "should_escalate",
    "lint_done_when",
    "next_questions",
    "QUESTION_TEMPLATES",
    "SLOTS",
    "Claim",
    "CitationResult",
    "ResearchBrief",
    "TAGS",
    "check_citations",
    "check_tag_coverage",
    "ingest_research",
    "parse_research",
    "select_citations",
    "__version__",
]
