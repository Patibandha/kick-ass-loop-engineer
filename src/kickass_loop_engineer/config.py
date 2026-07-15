"""Configuration loader that assembles a loop from a YAML file.

Lets the entire backend be chosen declaratively: which provider powers the
builder, which powers the reviewer (they may differ), and the loop budgets. YAML
is imported lazily so importing the package does not require PyYAML.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import replace
from typing import Any, Optional

from .agents import BUILDER_SYSTEM, UI_BUILDER_RULES, Builder, Reviewer
from .cursor import PipelineCursor
from .ensemble import AttemptSpec, default_specs
from .gates import GATES, GateSpec
from .guardrails import DEFAULT_VERIFY_PREFIXES, VerificationPolicy
from .ledger import CostLedger
from .memory import Memory
from .modes import MODE_AUTO, validate_mode
from .notify import Notifier
from .objective import Objective
from .orchestrator import Orchestrator
from .playbook import Playbook
from .pricing import context_window, load_pricing
from .providers import build_provider
from .review import CrossModelReviewer, ensure_cross_model
from .worktree import WorktreeManager

logger = logging.getLogger("kickass_loop_engineer.config")

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
  provider: ollama            # claude_code | ollama | anthropic | openai_compat
  model: kimi-k2.7-code:cloud  # any pulled Ollama tag
  host: http://localhost:11434

# builder:                     # ALTERNATIVE: any OpenAI-compatible /chat/completions endpoint
#   provider: openai_compat
#   model: gpt-5.5
#   base_url: https://api.openai.com/v1
#   # base_url: https://generativelanguage.googleapis.com/v1beta/openai  # Gemini
#   # base_url: https://openrouter.ai/api/v1                             # OpenRouter
#   # base_url: http://localhost:11434/v1                                # local Ollama
#   api_key_env: OPENAI_API_KEY  # NAME of the env var holding the key — never the key itself
#   family: gpt                # third-party/unknown models MUST declare a family so
#                              # cross-model review independence can be enforced

reviewer:                      # cross-model reviewer for `loop-engineer run` (family must differ from the builder)
  provider: ollama
  model: qwen2.5

guardrails:
  max_file_bytes: 1000000
  max_files_per_round: 50
  allow_overwrite: true
#   extra_verify_prefixes:     # extend the verification allowlist — MUST be a
#     - "mytool check"         # list (scalars are rejected). WARNING: these run
#                              # with gate authority; every use logs a warning.

# gates:                       # verifier gates, run in order (default: one unit gate)
#   - name: unit               # unit|security|data_leak|performance|smoke|ui|architecture
#     cmd: "python3 -m pytest -q"  # must be on the verification allowlist
#     prove: true              # proof-of-test: revert->red->green
#   - name: security
#     cmd: "bandit -r ."
#     prove: false
#   - name: ui
#     cmd: "npx playwright test"
#     observe: "npx playwright-cli snapshot http://localhost:3000"  # runs when the gate
#                              # FAILS (same allowlist authority, 30s/8KB caps); the
#                              # captured output feeds the builder's retry context

# visual:                      # ADVISORY VLM screenshot critique, run after gates pass
#   screenshot_cmd: "npx playwright-cli screenshot http://localhost:3000 shot.png"
#                              # allowlisted; the LAST token names the image it writes
#   provider:                  # NESTED provider section, standard form (vision model)
#     provider: ollama         # claude_code | ollama | anthropic | openai_compat
#     model: llava
#                              # findings are advisory only (VLM visual judgment is
#                              # ~50% accurate pairwise): they feed review feedback,
#                              # NEVER a pass/fail verdict — gates alone decide that

# format_retries: 1            # corrective re-calls per round when a builder/reviewer
#                              # reply is non-empty but malformed (0 disables)

# architect: auto              # design-first architect stage: auto | on | off.
#                              # auto (default) = smart trigger: decompose runs FIRST,
#                              # and only a 2+-slice objective triggers the architect
#                              # (validated design.md, then re-decompose against it);
#                              # on = always architect, even single-slice; off = never.
#                              # CLI --architect / --no-architect override this value.

# mode: auto                   # task mode: auto | build | enhance | fix | audit.
#                              # auto (default) detects: git-tracked files in the
#                              # workspace -> enhance (repo map + relevant existing
#                              # files ride every brief), none -> build (greenfield).
#                              # fix (reproduce-first bugfix) and audit (read-only
#                              # findings sweep) are NEVER auto-detected — request
#                              # them here or with `run --mode`.

# --- 1.0 settings below are OPTIONAL; the shown values are the defaults that apply
# --- even if you delete these sections. Uncomment to change them.

# ensemble:                    # competing attempts per hard slice (verified selection)
#   n: 2                       # attempts vary by an auto temperature ladder:
#                              # 0.2, 0.7, 1.0, then +0.3 steps capped at 1.5
#   attempts:                  # OR explicit per-attempt specs — n is derived from
#     - model: kimi-k2.7-code:cloud  # this list (ensemble.n is then ignored);
#       temperature: 0.9       # keys per attempt: model, temperature, provider,
#     - model: llama3.3        # family. Every attempt must stay cross-family
#                              # with the reviewer (checked before any spend).

# models:                      # cross-model review: builder != reviewer family
#   builder: kimi-k2.7-code:cloud
#   reviewer: qwen2.5           # different family from the builder; `ollama pull qwen2.5`
#   arbiter: claude             # the MC session arbitrates on risk/ambiguity

# ledger:                      # cost ledger ($ hard cap per run + advisory monthly)
#   run_cap_usd: 10            # hard per-run cap
#   month_cap_usd: 200         # advisory monthly ledger (warns at 80%)
#   path: .loop-engineer/ledger.json
#   pricing_path: my-rates.yaml  # per-Mtok rate overrides on the bundled table
#                              # (a top-level inline `pricing:` map also works);
#                              # models with no pricing row cost $0 + a warning

# notify:                      # ping on escalation + terminal state (unattended runs)
#   # ntfy.sh (free, no account):
#   command: 'curl -s -d @- ntfy.sh/your-topic'
#   # or a webhook (Telegram bot, Slack, etc.):
#   # webhook: https://example.com/hook
"""

# Appended to EXAMPLE_CONFIG by `loop-engineer init --ui` so a fresh UI project
# starts with the unit gate plus a Playwright ui gate whose observer snapshots
# the page (a11y tree) on failure and feeds it back to the builder.
UI_GATES_SNIPPET = """\

# UI gates (written by `loop-engineer init --ui`): the unit gate runs first,
# then the Playwright ui gate. When the ui gate fails, its observe command
# captures a page snapshot that feeds the builder's retry context.
gates:
  - name: unit
    cmd: "python3 -m pytest -q"
  - name: ui
    cmd: "npx playwright test"
    observe: "npx playwright-cli snapshot http://localhost:3000"
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


#: Base builder section applied when the config carries no ``builder`` key.
_DEFAULT_BUILDER_SECTION: dict[str, Any] = {"provider": "ollama", "model": "kimi-k2"}


def _builder_system_prompt(config: dict[str, Any]) -> str:
    """Return the builder system prompt for *config* (UI rules when a ui gate runs).

    Configs without a ui-category gate keep the prompt byte-identical to
    ``BUILDER_SYSTEM``; a ui gate appends ``UI_BUILDER_RULES`` so the guidance
    persists across every round.
    """
    if any(GATES.get(spec.name, {}).get("category") == "ui"
           for spec in parse_gates(config)):
        return f"{BUILDER_SYSTEM}\n\n{UI_BUILDER_RULES}"
    return BUILDER_SYSTEM


def build_builder(config: dict[str, Any], pricing: Optional[dict] = None) -> Builder:
    """Instantiate just the builder agent from config (for single-round use).

    The optional ``family`` key is a cross-model-review declaration consumed by
    :class:`CrossModelReviewer`, not a provider argument, so it is popped here
    before the remaining keys are forwarded to the provider constructor. The
    builder's context window is resolved from the pricing table (prefix match
    on the constructed provider's model), so oversized prompts can be warned
    about; an unknown model resolves to ``0``, which disables the check.

    When the parsed gates include a ``ui``-category gate, ``UI_BUILDER_RULES``
    is appended to the builder's system prompt (persistent UI guidance across
    every round); configs without a ui gate keep the prompt byte-identical to
    ``BUILDER_SYSTEM``.

    Args:
        config: Parsed config with an optional ``builder`` section.
        pricing: Optional pre-loaded pricing table; ``None`` loads the bundled
            table via :func:`load_pricing` (callers that already loaded one —
            :func:`build_orchestrator` — pass it to keep a single load).

    Returns:
        A configured ``Builder``.
    """
    builder_cfg = dict(config.get("builder", _DEFAULT_BUILDER_SECTION))
    builder_cfg.pop("family", "")
    provider = build_provider(builder_cfg.pop("provider"), **builder_cfg)
    table = pricing if pricing is not None else load_pricing()
    model = getattr(provider, "model", None) or provider.name
    return Builder(provider, system_prompt=_builder_system_prompt(config),
                   context_window=context_window(model, table))


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


_ATTEMPT_KEYS = ("model", "temperature", "provider", "family")

#: Providers whose constructor has NO temperature parameter: an attempt spec's
#: temperature is stripped (with a once-per-model warning) instead of causing
#: a TypeError deep inside provider construction.
_TEMPERATURE_FREE_PROVIDERS = ("claude_code", "anthropic")

#: Providers whose constructor REQUIRES a ``model`` argument (no default). A
#: cross-provider attempt spec that switches to one of these MUST name a model,
#: else ``build_provider`` dies with a raw ``TypeError`` deep in construction —
#: parse-time validation raises a clear RuntimeError naming the attempt instead.
_MODEL_REQUIRED_PROVIDERS = ("ollama", "openai_compat")


def parse_attempts(config: dict[str, Any]) -> list:
    """Return the per-attempt :class:`AttemptSpec` list from config.

    ``ensemble.attempts`` (a list of ``{model, temperature?, provider?,
    family?}`` mappings) wins when present — ``n`` is then derived from its
    length and any explicit ``ensemble.n`` is ignored with a warning. Absent
    ``attempts``, today's behavior is preserved: ``ensemble.n`` attempts on
    the default temperature ladder (:func:`~kickass_loop_engineer.ensemble.default_specs`).

    Args:
        config: Parsed config with an optional ``ensemble`` section.

    Returns:
        A non-empty list of :class:`AttemptSpec`.

    Raises:
        RuntimeError: When ``ensemble.attempts`` is not a non-empty list, or
            any entry is not a mapping, carries unknown keys, or has a
            non-numeric temperature — the error names the offending field.
    """
    section = config.get("ensemble", {}) or {}
    raw = section.get("attempts")
    if raw is None:
        return default_specs(ensemble_n(config))
    if not isinstance(raw, list) or not raw:
        raise RuntimeError(
            f"ensemble.attempts must be a non-empty LIST of attempt mappings "
            f"({{model, temperature?, provider?, family?}}), got "
            f"{type(raw).__name__}: {raw!r}")
    if "n" in section:
        logger.warning(
            "ensemble.n (%s) is ignored because ensemble.attempts is present; "
            "n=%d is derived from the attempts list", section.get("n"), len(raw))
    base_provider = str((config.get("builder") or {}).get("provider", "ollama")
                        or "ollama")
    specs = []
    for index, entry in enumerate(raw):
        field = f"ensemble.attempts[{index}]"
        if not isinstance(entry, dict):
            raise RuntimeError(
                f"{field} must be a mapping with model/temperature/provider/"
                f"family keys, got {type(entry).__name__}: {entry!r}")
        unknown = sorted(set(entry) - set(_ATTEMPT_KEYS))
        if unknown:
            raise RuntimeError(
                f"{field} has unknown keys {unknown}; allowed keys: "
                f"{', '.join(_ATTEMPT_KEYS)}")
        temperature = entry.get("temperature")
        if temperature is not None and (isinstance(temperature, bool)
                                        or not isinstance(temperature, (int, float))):
            raise RuntimeError(
                f"{field}.temperature must be a number, got "
                f"{type(temperature).__name__}: {temperature!r}")
        provider = str(entry.get("provider", "") or "")
        model = str(entry.get("model", "") or "")
        # A spec that switches PROVIDER drops the base builder's kwargs (model
        # included), so a model-required provider with no model would die at
        # construction with a raw TypeError — fail here instead, naming the
        # attempt (matching every other parse_attempts failure).
        if (provider and provider != base_provider
                and provider in _MODEL_REQUIRED_PROVIDERS and not model):
            raise RuntimeError(
                f"{field} switches to provider {provider!r}, which requires a "
                f"model, but names none; add a 'model' to this attempt spec")
        specs.append(AttemptSpec(
            model=model,
            temperature=float(temperature) if temperature is not None else None,
            provider=provider,
            family=str(entry.get("family", "") or ""),
        ))
    return specs


def build_attempt_factory(config: dict[str, Any], pricing: Optional[dict] = None):
    """Return a ``spec -> Builder`` factory for per-attempt builder construction.

    The factory owns ALL config knowledge for diverse ensembles so the
    orchestrator never parses config: each spec starts from the base
    ``builder`` section (its kwargs are kept only when the spec stays on the
    same provider), overlays the spec's model/temperature, and constructs the
    provider through the same :func:`build_provider` registry path as every
    other agent. Provider constructors are pure (no network), so the factory
    is safe to call at config time for fail-fast validation.

    Temperature handling: ``claude_code`` and ``anthropic`` constructors take
    no temperature — the factory STRIPS it with a once-per-model warning
    (shared across every call to this factory instance) instead of crashing.

    Args:
        config: Parsed config whose ``builder`` section supplies the base.
        pricing: Optional pre-loaded pricing table for context-window lookup;
            ``None`` loads the bundled table.

    Returns:
        A callable ``AttemptSpec -> Builder``.
    """
    table = pricing if pricing is not None else load_pricing()
    system_prompt = _builder_system_prompt(config)
    warned_models: set = set()

    def factory(spec: AttemptSpec) -> Builder:
        base = dict(config.get("builder", _DEFAULT_BUILDER_SECTION))
        base.pop("family", "")
        base_provider = base.pop("provider", "ollama")
        name = spec.provider or base_provider
        kwargs = dict(base) if name == base_provider else {}
        if spec.model:
            kwargs["model"] = spec.model
        if spec.temperature is not None:
            kwargs["temperature"] = spec.temperature
        if name in _TEMPERATURE_FREE_PROVIDERS and "temperature" in kwargs:
            dropped = kwargs.pop("temperature")
            label = str(kwargs.get("model") or name)
            if label not in warned_models:
                warned_models.add(label)
                logger.warning(
                    "provider %r (model %s) takes no temperature parameter; "
                    "dropping temperature=%s from this attempt spec",
                    name, label, dropped)
        provider = build_provider(name, **kwargs)
        model = getattr(provider, "model", None) or provider.name
        return Builder(provider, system_prompt=system_prompt,
                       context_window=context_window(model, table))

    return factory


_ARCHITECT_MODES = ("auto", "on", "off")


def architect_mode(config: dict[str, Any]) -> str:
    """Return the validated architect smart-trigger mode from config.

    YAML 1.1 parses bare ``on``/``off`` as booleans, so boolean values are
    normalized back to their intended mode strings.

    Args:
        config: Parsed config with an optional top-level ``architect`` key.

    Returns:
        ``"auto"`` (the default), ``"on"``, or ``"off"``.

    Raises:
        RuntimeError: When the configured value is none of the valid modes.
    """
    raw = config.get("architect", "auto")
    if isinstance(raw, bool):  # yaml: `architect: on` loads as True, `off` as False
        return "on" if raw else "off"
    mode = str(raw)
    if mode not in _ARCHITECT_MODES:
        raise RuntimeError(
            f"architect must be one of {', '.join(_ARCHITECT_MODES)} (got {mode!r})")
    return mode


def task_mode(config: dict[str, Any]) -> str:
    """Return the validated task mode from config.

    The ``mode:`` key selects what kind of run this is — ``build`` /
    ``enhance`` / ``fix`` / ``audit``, or ``auto`` (the default), which the
    orchestrator resolves against the workspace at run start (tracked files →
    enhance, else build; fix/audit are explicit-only).

    Args:
        config: Parsed config with an optional top-level ``mode`` key.

    Returns:
        The requested mode (``"auto"`` when the key is absent).

    Raises:
        RuntimeError: When the configured value is none of the valid modes.
    """
    return validate_mode(str(config.get("mode", MODE_AUTO)))


def parse_gates(config: dict[str, Any]) -> list[GateSpec]:
    """Normalize the ``gates`` config section into a list of :class:`GateSpec`.

    3.0 configs write ``gates:`` as a list of entries (``{name, cmd, prove?,
    observe?}``); the 1.0 single-mapping form and a missing/empty section are
    still accepted (back-compat: a mapping becomes a one-element list, an empty
    section becomes the default unit gate).

    Args:
        config: Parsed config with an optional ``gates`` section (list or
            mapping).

    Returns:
        The configured gates in listed order; ``[GateSpec()]`` (the default
        unit gate) when the section is absent or empty.

    Raises:
        RuntimeError: When any gates entry is not a mapping (e.g. a bare
            string like ``gates: ["pytest -q"]``) — a clear config error
            beats an AttributeError from deep inside the loader.
    """
    section = config.get("gates") or {}
    entries = section if isinstance(section, list) else [section] if section else [{}]
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError(
                f"each gates entry must be a mapping with name/cmd (and optional "
                f"prove/observe) keys, got {type(entry).__name__}: {entry!r}")
    return [
        GateSpec(name=e.get("name", "unit"),
                 command=e.get("cmd", "python3 -m pytest -q"),
                 prove=bool(e.get("prove", True)),
                 observe=str(e.get("observe", "") or ""))
        for e in entries
    ]


def build_verification_policy(config: dict[str, Any]) -> VerificationPolicy:
    """Build the verification policy: bundled allowlist plus warned-about extras.

    The bundled ``DEFAULT_VERIFY_PREFIXES`` allowlist is always included. Users
    may extend it via ``guardrails.extra_verify_prefixes`` in config; every use
    of that escape hatch logs a WARNING naming the extra prefixes, because they
    run with gate authority.

    Args:
        config: Parsed config with an optional ``guardrails`` section carrying
            an optional ``extra_verify_prefixes`` list.

    Returns:
        A :class:`VerificationPolicy` whose ``allowed_prefixes`` is the bundled
        allowlist followed by any configured extras.

    Raises:
        RuntimeError: When ``extra_verify_prefixes`` is a scalar rather than a
            list — iterating a string would split it per-character and silently
            widen the security allowlist.
    """
    raw = (config.get("guardrails", {}) or {}).get("extra_verify_prefixes") or []
    if not isinstance(raw, (list, tuple)):
        raise RuntimeError(
            f"guardrails.extra_verify_prefixes must be a LIST of command prefixes "
            f"(got {type(raw).__name__}); a scalar would be interpreted per-character "
            f"and silently widen the security allowlist")
    extras = tuple(str(p) for p in raw if p)
    if extras:
        logger.warning("extending the verification allowlist from config: %s "
                       "(review these — they run with gate authority)", list(extras))
    return VerificationPolicy(allowed_prefixes=DEFAULT_VERIFY_PREFIXES + extras)


def build_visual(config: dict[str, Any]) -> tuple:
    """Parse the optional ``visual:`` section into ``(visual_cfg, provider)``.

    The section arms the advisory VLM screenshot critique and carries two
    required keys: ``screenshot_cmd`` (allowlisted; its LAST whitespace token
    names the output image) and a NESTED ``provider`` section in the standard
    provider form (``{provider, model, ...}``), constructed through the same
    :func:`build_provider` path as the builder and reviewer.

    Args:
        config: Parsed config with an optional ``visual`` section.

    Returns:
        ``({"screenshot_cmd": ...}, provider)`` when configured, else
        ``(None, None)`` — the feature entirely off.

    Raises:
        RuntimeError: When the section is present but not a mapping, or is
            missing ``screenshot_cmd``, the nested ``provider`` section, or
            the provider name inside it.
    """
    section = config.get("visual")
    if not section:
        return None, None
    if not isinstance(section, dict):
        raise RuntimeError(
            f"the visual section must be a mapping with screenshot_cmd and a "
            f"nested provider section, got {type(section).__name__}: {section!r}")
    command = str(section.get("screenshot_cmd", "") or "")
    provider_cfg = dict(section.get("provider") or {})
    if not command or not provider_cfg:
        raise RuntimeError(
            "the visual section requires both screenshot_cmd and a nested "
            "provider section (visual.provider: {provider, model, ...})")
    provider_cfg.pop("family", "")
    name = provider_cfg.pop("provider", "")
    if not name:
        raise RuntimeError(
            "visual.provider must name a provider (e.g. provider: openai_compat)")
    return {"screenshot_cmd": command}, build_provider(name, **provider_cfg)


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
    any spend. The check reads MODEL IDENTITY off the CONSTRUCTED provider
    objects (``provider.model``, falling back to ``provider.name`` when the
    provider carries no explicit model, e.g. claude_code's CLI default), so no
    config labeling can diverge from which model identity actually runs. A
    ``family`` key on the ``builder``/``reviewer`` sections declares that
    side's model family explicitly (for third-party models family detection
    cannot classify); declared families override detection, rejection
    semantics are unchanged — but note a declared ``family`` is an UNVERIFIED
    user assertion, not something the engine can check against the endpoint.

    Diverse ensembles extend the same fail-fast property to every attempt:
    each :class:`~kickass_loop_engineer.ensemble.AttemptSpec` from
    :func:`parse_attempts` has its provider constructed HERE (guard layer 1)
    and its family checked against the reviewer, so a same-family attempt
    spec raises before any spend. The winner's constructed provider object is
    re-checked at review time (guard layer 2, ``CrossModelReviewer.check``).

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
    # One table for the whole run: bundled rates + optional user file + inline map.
    pricing_table = load_pricing(
        config.get("ledger", {}).get("pricing_path", "") or "",
        config.get("pricing"))
    builder_family = str(config.get("builder", {}).get("family", "") or "")
    builder = build_builder(config, pricing=pricing_table)
    reviewer_cfg = dict(config.get("reviewer", {"provider": "ollama", "model": roster["reviewer"]}))
    reviewer_family = str(reviewer_cfg.pop("family", "") or "")
    reviewer_provider = build_provider(reviewer_cfg.pop("provider"), **reviewer_cfg)
    builder_model = getattr(builder.provider, "model", None) or builder.provider.name
    reviewer_model = getattr(reviewer_provider, "model", None) or reviewer_provider.name
    # format_retries governs BOTH agents (as documented): the reviewer's paid
    # corrective calls must honor the same knob the builder's do.
    format_retries = int(config.get("format_retries", 1))
    cross = CrossModelReviewer(builder_model, reviewer_model,
                               Reviewer(reviewer_provider, format_retries=format_retries),
                               builder_family=builder_family, reviewer_family=reviewer_family)
    # Diverse ensembles: one AttemptSpec per attempt, constructed through the
    # injected factory. GUARD LAYER 1 (fail-fast): EVERY spec's provider is
    # constructed right here — provider ctors are pure/no-network — and its
    # family checked against the reviewer, so a same-family attempt spec fails
    # at config time, before any spend.
    attempt_specs = []
    builder_factory = build_attempt_factory(config, pricing=pricing_table)
    for spec in parse_attempts(config):
        # The base builder's declared family only covers specs that reuse the
        # base builder identity; a spec overriding model/provider must declare
        # its own family (a declaration is per-model, never transferable).
        # Baking the inherited declaration into the spec keeps the config-time
        # and review-time checks reading the SAME resolved family.
        if not spec.family and builder_family and not (spec.model or spec.provider):
            spec = replace(spec, family=builder_family)
        attempt_provider = builder_factory(spec).provider
        attempt_model = (getattr(attempt_provider, "model", None)
                         or attempt_provider.name)
        ensure_cross_model(str(attempt_model), reviewer_model,
                           builder_family=spec.family,
                           reviewer_family=reviewer_family)
        attempt_specs.append(spec)
    visual_cfg, visual_provider = build_visual(config)
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
        gates=parse_gates(config),
        policy=build_verification_policy(config),
        ensemble_n=len(attempt_specs),
        attempt_specs=attempt_specs,
        builder_factory=builder_factory,
        run_id=run_id or f"run-{uuid.uuid4().hex[:8]}",
        pricing=pricing_table,
        format_retries=format_retries,
        architect_mode=architect_mode(config),
        visual_cfg=visual_cfg,
        visual_provider=visual_provider,
        mode=task_mode(config),
    )
