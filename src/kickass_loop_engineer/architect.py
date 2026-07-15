"""Architect stage: one provider call emits a structured ``design.md``.

The design document carries five required sections — components, an
architecture diagram, a primary-flow diagram, a decision log, and a
machine-readable ``layering:`` yaml block — which downstream stages parse
to steer decomposition and (later) enforce architecture conformance.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .guardrails import VerificationPolicy
from .providers.base import ProviderResult

logger = logging.getLogger("kickass_loop_engineer.architect")

_MERMAID_FENCE = re.compile(r"```mermaid[ \t]*\n(?P<body>.*?)\n[ \t]*```", re.DOTALL)
_YAML_FENCE = re.compile(r"```ya?ml[ \t]*\n(?P<body>.*?)\n[ \t]*```", re.DOTALL)
_COMPONENT_BULLET = re.compile(
    r"^[-*]\s+\*{0,2}(?P<name>[^:*]+?)\*{0,2}\s*:\s*(?P<responsibility>.+)$"
)
_HEADING = re.compile(r"^##\s+(?P<title>.+?)\s*$", re.MULTILINE)

_REQUIRED_SECTIONS = ("Components", "Architecture", "Primary flow", "Decisions")


ARCHITECT_SYSTEM = (
    "You are a software architect. Produce a design document in markdown with "
    "EXACTLY these five parts:\n"
    "1. `## Components` — one bullet per component, format `- name: responsibility`. "
    "Reference real module paths the code will use (e.g. `api/routes.py`).\n"
    "2. `## Architecture` — one ```mermaid fence containing a `graph TD` component "
    "diagram whose nodes are the component names.\n"
    "3. `## Primary flow` — one ```mermaid fence containing a `sequenceDiagram` of "
    "the main request/data flow between components.\n"
    "4. `## Decisions` — a markdown table with columns decision | why | alternative.\n"
    "5. A fenced ```yaml block declaring layering:\n"
    "```yaml\n"
    "layering:\n"
    "  layers: [outermost_module, ..., innermost_module]\n"
    "  forbidden:\n"
    "    - {from: <module>, to: <module>}\n"
    "```\n"
    "`layers` is the ordered module list (higher layers may import lower, never the "
    "reverse); `forbidden` lists extra import bans. Use real module names. "
    "Output ONLY the markdown document, no preamble."
)


@dataclass
class Design:
    """A parsed ``design.md`` document.

    Attributes:
        markdown: The full source markdown, verbatim.
        components: ``(name, responsibility)`` pairs from ``## Components``.
        mermaid_fences: Bodies of every ```mermaid fence, in document order.
        layering: The ``layering:`` mapping from the fenced yaml block, or
            ``None`` when absent or malformed (never a raise).
        problems: Human-readable defects found while parsing (missing
            sections, malformed layering yaml, ...). Empty for a
            well-formed document.
    """

    markdown: str
    components: List[Tuple[str, str]] = field(default_factory=list)
    mermaid_fences: List[str] = field(default_factory=list)
    layering: Optional[dict] = None
    problems: List[str] = field(default_factory=list)

    @classmethod
    def parse(cls, markdown: str) -> "Design":
        """Parse *markdown* into a :class:`Design`, recording defects.

        Missing required sections and a missing/malformed layering block are
        reported in ``problems`` — parsing never raises.

        Args:
            markdown: The design document text.

        Returns:
            A :class:`Design` with every extractable field populated.
        """
        text = markdown or ""
        problems: List[str] = []

        headings = {m.group("title") for m in _HEADING.finditer(text)}
        for section in _REQUIRED_SECTIONS:
            if section not in headings:
                problems.append(f"missing required section: ## {section}")

        components = cls._parse_components(text)
        fences = [m.group("body").strip() for m in _MERMAID_FENCE.finditer(text)]
        layering, layering_problem = cls._parse_layering(text)
        if layering_problem:
            problems.append(layering_problem)

        return cls(
            markdown=text,
            components=components,
            mermaid_fences=fences,
            layering=layering,
            problems=problems,
        )

    @property
    def decisions_present(self) -> bool:
        """True when the document carries a ``## Decisions`` heading."""
        return any(m.group("title") == "Decisions" for m in _HEADING.finditer(self.markdown))

    @staticmethod
    def _parse_components(text: str) -> List[Tuple[str, str]]:
        """Extract ``(name, responsibility)`` bullets from ``## Components``."""
        match = re.search(
            r"^##\s+Components\s*$(?P<body>.*?)(?=^##\s|\Z)", text, re.MULTILINE | re.DOTALL
        )
        if not match:
            return []
        components: List[Tuple[str, str]] = []
        for line in match.group("body").splitlines():
            bullet = _COMPONENT_BULLET.match(line.strip())
            if bullet:
                components.append(
                    (bullet.group("name").strip(), bullet.group("responsibility").strip())
                )
        return components

    @staticmethod
    def _parse_layering(text: str) -> Tuple[Optional[dict], str]:
        """Return ``(layering, problem)`` from the first yaml fence carrying it."""
        fences = [m.group("body") for m in _YAML_FENCE.finditer(text)]
        candidates = [body for body in fences if re.search(r"^layering\s*:", body, re.MULTILINE)]
        if not candidates:
            return None, "missing fenced yaml block with a layering: key"
        try:
            import yaml
        except ImportError:  # pragma: no cover - pyyaml is a hard dependency elsewhere
            return None, "layering yaml block present but PyYAML is not installed"
        try:
            loaded = yaml.safe_load(candidates[0])
        except yaml.YAMLError as exc:
            return None, f"malformed layering yaml block: {exc}"
        if not isinstance(loaded, dict) or not isinstance(loaded.get("layering"), dict):
            return None, "layering yaml block does not contain a layering: mapping"
        return loaded["layering"], ""


# Diagram types the stdlib sanity pre-check recognizes on a fence's first line.
_KNOWN_DIAGRAM_TYPES = (
    "graph", "sequenceDiagram", "flowchart", "classDiagram", "stateDiagram", "erDiagram",
)
_BRACKET_CLOSERS = {"(": ")", "[": "]", "{": "}"}

# Allowlisted (M1) validator invocation; maid takes the .mmd file as its
# only argument and exits 1 with caret-style diagnostics on invalid diagrams.
_MAID_PREFIX = "npx --yes @probelabs/maid"


def _stdlib_precheck(fences: List[str]) -> List[str]:
    """Sanity-check *fences* without any external tool.

    Checks each fence's first line against the known mermaid diagram types
    and verifies bracket/paren/brace balance.

    Args:
        fences: Mermaid fence bodies.

    Returns:
        One message per defect found; empty when everything looks sane.
    """
    problems: List[str] = []
    for idx, fence in enumerate(fences, start=1):
        lines = fence.strip().splitlines()
        first = lines[0].strip() if lines else ""
        if not first.startswith(_KNOWN_DIAGRAM_TYPES):
            problems.append(
                f"diagram {idx}: first line {first!r} is not a known mermaid diagram type"
            )
        stack: List[str] = []
        balanced = True
        for char in fence:
            if char in _BRACKET_CLOSERS:
                stack.append(_BRACKET_CLOSERS[char])
            elif char in _BRACKET_CLOSERS.values():
                if not stack or stack.pop() != char:
                    balanced = False
                    break
        if not balanced or stack:
            problems.append(f"diagram {idx}: unbalanced brackets/parens")
    return problems


def _default_runner(fence: str, policy: Optional[VerificationPolicy] = None) -> Optional[str]:
    """Validate one mermaid fence with maid through the verification policy.

    Args:
        fence: The mermaid fence body to validate.
        policy: Verification policy to run the command through (a fresh
            :class:`VerificationPolicy` when omitted; injectable for tests).

    Returns:
        ``""`` when the diagram is valid, the validator's error text when it
        is invalid, or ``None`` when the tool is unavailable / did not run.
    """
    policy = policy or VerificationPolicy()
    with tempfile.TemporaryDirectory(prefix="maid-") as tmpdir:
        path = os.path.join(tmpdir, "diagram.mmd")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(fence if fence.endswith("\n") else fence + "\n")
        result = policy.run(f"{_MAID_PREFIX} {path}", cwd=tmpdir)
    if result.error:
        logger.warning("mermaid validator did not run: %s", result.error)
        return None
    if result.passed:
        return ""
    return (result.stdout_tail or result.stderr_tail or "mermaid validation failed").strip()


def validate_mermaid(fences: List[str], runner=None) -> Tuple[str, List[str]]:
    """Validate mermaid *fences*, degrading to SKIPPED when no validator runs.

    Args:
        fences: Mermaid fence bodies extracted from a design document.
        runner: Callable taking one fence body and returning ``""`` (valid),
            error text (invalid), or ``None`` (validator unavailable; raising
            ``FileNotFoundError`` means the same). Defaults to the
            maid-backed :func:`_default_runner`.

    Returns:
        ``("ok", [])`` when every fence validates; ``("invalid", errors)``
        when the validator found problems (or there are no fences at all);
        ``("skipped", warnings)`` when the validator is unavailable — the
        warnings carry the visible SKIPPED notice plus any stdlib pre-check
        findings. SKIPPED is never silent and never a hard failure.
    """
    if not fences:
        return "invalid", ["design has no mermaid diagrams"]
    run = runner or _default_runner
    errors: List[str] = []
    for idx, fence in enumerate(fences, start=1):
        try:
            outcome = run(fence)
        except FileNotFoundError as exc:
            logger.warning("mermaid validator unavailable: %s", exc)
            outcome = None
        if outcome is None:
            warnings = [
                f"mermaid validation SKIPPED: validator unavailable ({_MAID_PREFIX}); "
                "stdlib pre-check only"
            ]
            warnings.extend(_stdlib_precheck(fences))
            logger.warning("%s", "; ".join(warnings))
            return "skipped", warnings
        if outcome:
            errors.append(f"diagram {idx}: {outcome}")
    if errors:
        return "invalid", errors
    return "ok", []


def _qualify(module: str, package: str) -> str:
    """Return *module* as a fully-qualified path inside *package*."""
    module = str(module).strip()
    if module == package or module.startswith(package + "."):
        return module
    return f"{package}.{module}"


def _relative(module: str, package: str) -> str:
    """Return *module* relative to *package* (for container-scoped layers)."""
    module = str(module).strip()
    if module.startswith(package + "."):
        return module[len(package) + 1:]
    return module


def render_importlinter(layering: Optional[dict], package: str) -> Optional[str]:
    """Render a declared layering into ``.importlinter`` INI text.

    Pure function: the design's machine-readable ``layering:`` block becomes
    an import-linter configuration with one ``layers`` contract (container
    scoped to *package*) and one ``forbidden`` contract per declared ban.
    Layer/forbidden module names may be package-relative or already
    fully qualified — both render correctly. Assumes ``layering["layers"]``
    lists layers outermost/highest FIRST (import-linter's layers-contract
    convention: earlier layers may import later ones, never the reverse).

    Args:
        layering: The ``layering`` mapping (``layers`` list + optional
            ``forbidden`` list of ``{from, to}`` entries), or ``None``.
        package: The root package the contracts apply to.

    Returns:
        The INI text, or ``None`` when *layering* declares nothing usable
        (absent, empty, or no ``layers``/``forbidden`` entries).
    """
    if not layering or not isinstance(layering, dict):
        return None
    layers = [str(l) for l in (layering.get("layers") or [])]
    forbidden = [f for f in (layering.get("forbidden") or []) if isinstance(f, dict)]
    if not layers and not forbidden:
        return None

    sections = [f"[importlinter]\nroot_package = {package}\n"]
    if layers:
        layer_lines = "\n".join(f"    {_relative(l, package)}" for l in layers)
        sections.append(
            "[importlinter:contract:layers]\n"
            "name = declared layering\n"
            "type = layers\n"
            f"layers =\n{layer_lines}\n"
            f"containers =\n    {package}\n"
        )
    for idx, ban in enumerate(forbidden, start=1):
        src, dst = str(ban.get("from", "")).strip(), str(ban.get("to", "")).strip()
        if not src or not dst:
            continue
        sections.append(
            f"[importlinter:contract:forbidden-{idx}]\n"
            f"name = forbidden: {_relative(src, package)} -> {_relative(dst, package)}\n"
            "type = forbidden\n"
            f"source_modules =\n    {_qualify(src, package)}\n"
            f"forbidden_modules =\n    {_qualify(dst, package)}\n"
        )
    return "\n".join(sections)


def generate_design(builder, objective, max_diagram_retries: int = 2,
                    validate=None, repo_map: str = ""):
    """Ask the builder model for a design document, validating its diagrams.

    Invalid mermaid drives a bounded regeneration loop: the validator's error
    text is appended to the prompt and the provider is re-asked, at most
    *max_diagram_retries* times. A SKIPPED validation (no validator available)
    never retries — the stage records the warning in ``Design.problems`` and
    proceeds. Still-invalid diagrams after the retries degrade the same way;
    the architect stage never hard-fails the run over diagrams.

    Args:
        builder: An ``agents.Builder``; its provider does the completion.
        objective: The run objective (``goal``/``done_when`` seed the prompt).
        max_diagram_retries: Extra provider calls allowed on invalid diagrams.
        validate: Validation runner passed through to :func:`validate_mermaid`
            (``None`` selects the maid-backed default).
        repo_map: Rendered orientation map of the existing codebase
            (enhance mode); rendered as a REPO MAP section in the prompt.
            Empty keeps the prompt byte-identical to a greenfield run.

    Returns:
        tuple[Design, ProviderResult]: The parsed design (defects and degrade
        notices recorded in ``Design.problems``) and an accumulated provider
        result for ledgering — ``text``/``model`` come from the LAST call
        while the token and cost fields are the SUM across every call, so a
        paid retry is never lost to cost accounting (M1 invariant).
    """
    map_section = f"REPO MAP:\n{repo_map}\n\n" if repo_map else ""
    base_user = (
        f"OBJECTIVE:\n{objective.goal}\n\nDONE WHEN:\n{objective.done_when}\n\n"
        f"{map_section}"
        "Produce the design document as instructed."
    )
    result = builder.provider.complete(ARCHITECT_SYSTEM, base_user)
    tokens = result.tokens
    cost_usd = result.cost_usd
    prompt_tokens = result.prompt_tokens
    completion_tokens = result.completion_tokens
    design = Design.parse(result.text or "")
    status, messages = validate_mermaid(design.mermaid_fences, runner=validate)

    retries = 0
    while status == "invalid" and retries < max_diagram_retries:
        retries += 1
        logger.warning(
            "mermaid validation failed (retry %d/%d): %s",
            retries, max_diagram_retries, "; ".join(messages),
        )
        retry_user = (
            f"{base_user}\n\nYOUR PREVIOUS DESIGN HAD INVALID MERMAID DIAGRAMS:\n"
            + "\n".join(messages)
            + "\nRegenerate the FULL design document with corrected diagrams."
        )
        result = builder.provider.complete(ARCHITECT_SYSTEM, retry_user)
        tokens += result.tokens
        cost_usd += result.cost_usd
        prompt_tokens += result.prompt_tokens
        completion_tokens += result.completion_tokens
        design = Design.parse(result.text or "")
        status, messages = validate_mermaid(design.mermaid_fences, runner=validate)

    if status == "invalid":
        note = (
            f"mermaid diagrams still invalid after {max_diagram_retries} retries: "
            + "; ".join(messages)
        )
        design.problems.append(note)
        logger.warning("%s (architect degrades; run continues)", note)
    elif status == "skipped":
        design.problems.extend(messages)

    if design.problems:
        logger.warning("design.md carries problems: %s", "; ".join(design.problems))
    accumulated = ProviderResult(
        text=result.text,
        tokens=tokens,
        cost_usd=cost_usd,
        model=result.model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    return design, accumulated
