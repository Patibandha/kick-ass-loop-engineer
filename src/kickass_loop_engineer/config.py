"""Configuration loader that assembles a loop from a YAML file.

Lets the entire backend be chosen declaratively: which provider powers the
builder, which powers the reviewer (they may differ), and the loop budgets. YAML
is imported lazily so importing the package does not require PyYAML.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from .agents import Builder, Reviewer
from .cursor import PipelineCursor
from .ledger import CostLedger
from .memory import Memory
from .notify import Notifier
from .objective import Objective
from .orchestrator import GateSpec, Orchestrator
from .playbook import Playbook
from .providers import build_provider
from .review import CrossModelReviewer
from .worktree import WorktreeManager

# Single source for the starter config: `loop-engineer init` writes this verbatim
# and `loop-engineer.example.yaml` is a regenerated copy of it (kept in sync by
# tests/test_config.py::test_example_file_matches_single_source). Regenerate with:
#   python3 -c "from kickass_loop_engineer.config import EXAMPLE_CONFIG; \
#     open('loop-engineer.example.yaml', 'w').write(EXAMPLE_CONFIG)"
EXAMPLE_CONFIG = """\
# loop-engineer configuration (set once at skill install time).
# The builder runs every round; in the Claude Code skill workflow the SESSION
# reviews, so a reviewer provider is only needed for standalone `loop-engineer run`.
# No API keys are used: the ollama provider talks to the local daemon
# (localhost:11434, $0 cost); a `:cloud` tag is served via your ollama.com login on
# the daemon (set up once with `ollama signin`). ANTHROPIC_API_KEY is never used.

builder:
  provider: ollama            # claude_code | ollama | anthropic
  model: kimi-k2.7-code:cloud  # any pulled Ollama tag
  host: http://localhost:11434

reviewer:                      # cross-model reviewer for `loop-engineer run` (family must differ from the builder)
  provider: ollama
  model: qwen2.5

guardrails:
  max_file_bytes: 1000000
  max_files_per_round: 50
  allow_overwrite: true

# gates:                       # verifier gate for the pipeline (defaults shown)
#   name: unit                 # unit|security|data_leak|performance|smoke
#   cmd: "python3 -m pytest -q"  # must be on the verification allowlist
#   prove: true                # proof-of-test: revert->red->green

# --- 1.0 settings below are OPTIONAL; the shown values are the defaults that apply
# --- even if you delete these sections. Uncomment to change them.

# ensemble:                    # competing attempts per hard slice (verified selection)
#   n: 2

# models:                      # cross-model review: builder != reviewer family
#   builder: kimi-k2.7-code:cloud
#   reviewer: qwen2.5           # different family from the builder; `ollama pull qwen2.5`
#   arbiter: claude             # the MC session arbitrates on risk/ambiguity

# ledger:                      # cost ledger ($ hard cap per run + advisory monthly)
#   run_cap_usd: 10            # hard per-run cap
#   month_cap_usd: 200         # advisory monthly ledger (warns at 80%)
#   path: .loop-engineer/ledger.json

# notify:                      # ping on escalation + terminal state (unattended runs)
#   # ntfy.sh (free, no account):
#   command: 'curl -s -d @- ntfy.sh/your-topic'
#   # or a webhook (Telegram bot, Slack, etc.):
#   # webhook: https://example.com/hook
"""


def load_config(path: str) -> dict[str, Any]:
    """Read and parse a YAML config file.

    Args:
        path: Path to the YAML configuration.

    Returns:
        The parsed configuration mapping.

    Raises:
        RuntimeError: When PyYAML is unavailable or the file cannot be parsed.
    """
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to load config files (pip install pyyaml)") from exc
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"failed to load config {path!r}: {exc}") from exc


def build_builder(config: dict[str, Any]) -> Builder:
    """Instantiate just the builder agent from config (for single-round use).

    Args:
        config: Parsed config with an optional ``builder`` section.

    Returns:
        A configured ``Builder``.
    """
    builder_cfg = dict(config.get("builder", {"provider": "ollama", "model": "kimi-k2"}))
    return Builder(build_provider(builder_cfg.pop("provider"), **builder_cfg))


def build_ledger(config: dict[str, Any], month: str) -> CostLedger:
    """Instantiate a :class:`CostLedger` from the optional ``ledger`` config section.

    Args:
        config: Parsed config with an optional ``ledger`` section.
        month: The ``YYYY-MM`` bucket for the current run (caller-supplied).

    Returns:
        A configured :class:`CostLedger`.
    """
    section = config.get("ledger", {})
    return CostLedger(
        path=section.get("path", ".loop-engineer/ledger.json"),
        run_cap_usd=float(section.get("run_cap_usd", 10.0)),
        month_cap_usd=float(section.get("month_cap_usd", 200.0)),
        warn_ratio=float(section.get("warn_ratio", 0.8)),
        month=month,
    )


def build_notifier(config: dict[str, Any]) -> Notifier:
    """Instantiate a :class:`Notifier` from the optional ``notify`` config section.

    When the ``notify`` key is absent, returns a no-op ``Notifier`` with both
    ``webhook`` and ``command`` set to ``None``.

    Args:
        config: Parsed config with an optional ``notify`` section.

    Returns:
        A configured :class:`Notifier`.
    """
    section = config.get("notify", {})
    return Notifier(
        webhook=section.get("webhook") or None,
        command=section.get("command") or None,
        timeout=float(section.get("timeout", 10.0)),
    )


def ensemble_n(config: dict[str, Any]) -> int:
    """Return the number of ensemble attempts from config.

    Args:
        config: Parsed config with an optional ``ensemble`` section.

    Returns:
        ``config["ensemble"]["n"]`` if present, otherwise ``2`` (the default).
    """
    return int(config.get("ensemble", {}).get("n", 2))


_DEFAULT_ROSTER: dict[str, str] = {
    "builder": "kimi-k2.7-code:cloud",
    "reviewer": "qwen2.5",
    "arbiter": "claude",
}


def model_roster(config: dict[str, Any]) -> dict[str, str]:
    """Return the model roster, overlaying config on top of defaults.

    Defaults::

        {"builder": "kimi-k2.7-code:cloud", "reviewer": "qwen2.5", "arbiter": "claude"}

    Any key present in ``config["models"]`` overrides the default.

    Args:
        config: Parsed config with an optional ``models`` section.

    Returns:
        A mapping of role → model identifier.
    """
    roster = dict(_DEFAULT_ROSTER)
    roster.update(config.get("models", {}))
    return roster


def build_orchestrator(config: dict[str, Any], *, workspace: str, month: str,
                       objective: Objective, run_id: str = "") -> Orchestrator:
    """Assemble a fully wired Orchestrator from parsed config.

    The cross-model policy is enforced here at construction (builder vs
    reviewer family via CrossModelReviewer) so misconfiguration fails before
    any spend. The check reads identity off the CONSTRUCTED provider objects
    (``provider.model``, falling back to ``provider.name`` when the provider
    carries no explicit model, e.g. claude_code's CLI default) so no config
    labeling can diverge from what actually runs.

    Args:
        config: Parsed loop-engineer.yaml mapping.
        workspace: Directory the pipeline promotes work into (a git repo).
        month: ``YYYY-MM`` ledger bucket (clock lives at the CLI boundary).
        objective: The run objective.
        run_id: Optional stable id; auto-generated when empty.

    Returns:
        A ready-to-run :class:`Orchestrator`.

    Raises:
        CrossModelReviewError: If builder and reviewer share a model family.
    """
    roster = model_roster(config)
    builder = build_builder(config)
    reviewer_cfg = dict(config.get("reviewer", {"provider": "ollama", "model": roster["reviewer"]}))
    reviewer_provider = build_provider(reviewer_cfg.pop("provider"), **reviewer_cfg)
    builder_model = getattr(builder.provider, "model", None) or builder.provider.name
    reviewer_model = getattr(reviewer_provider, "model", None) or reviewer_provider.name
    cross = CrossModelReviewer(builder_model, reviewer_model, Reviewer(reviewer_provider))
    gates_cfg = config.get("gates", {})
    run_dir = os.path.join(workspace, ".loop-engineer")
    return Orchestrator(
        objective=objective,
        workspace=workspace,
        builder=builder,
        reviewer=cross,
        worktrees=WorktreeManager(workspace),
        ledger=build_ledger(config, month),
        notifier=build_notifier(config),
        memory=Memory(config.get("memory", {}).get("path", os.path.join(run_dir, "memory.db"))),
        playbook=Playbook(config.get("playbook", {}).get("path", os.path.join(run_dir, "playbook.json"))),
        cursor=PipelineCursor(workspace),
        gate=GateSpec(name=gates_cfg.get("name", "unit"),
                      command=gates_cfg.get("cmd", "python3 -m pytest -q")),
        ensemble_n=ensemble_n(config),
        run_id=run_id or f"run-{uuid.uuid4().hex[:8]}",
        prove=bool(gates_cfg.get("prove", True)),
    )
