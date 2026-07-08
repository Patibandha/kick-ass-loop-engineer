"""Deep-research provenance: parse RESEARCH.md and gate it like a test.

Every load-bearing claim (a stack / API / version choice, kept under the
``## Decisions`` heading) must be sourced, never ``[ASSUMED]``. The engine is
network-free: the research sweep and the citation re-fetch are session rounds;
this module only parses and compares deterministically.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

TAGS: tuple[str, ...] = ("VERIFIED", "CITED", "ASSUMED")

_HEADING = re.compile(r"^##\s+(.+?)\s*$")
# [-*+] [TAG] text  (source: URL "quote")   — the source clause is optional.
# Marker requires a following space to be a valid claim; a bare "-[TAG]" is a
# malformed bullet ATTEMPT and must fail as untagged, not silently parse.
_CLAIM = re.compile(
    r'^\s*[-*+]\s+\[(?P<tag>[A-Z]+)\]\s+(?P<text>.*?)'
    r'(?:\s+\(source:\s*(?P<url>\S+)\s+"(?P<quote>[^"]*)"\s*\))?\s*$'
)
# Any line starting with a bullet marker (space-or-not) counts as a bullet
# ATTEMPT; if it fails to parse as a valid claim it FAILs untagged rather than
# being silently skipped.
_BULLET = re.compile(r"^\s*[-*+]")


def _section_kind(heading: str) -> str:
    """Classify a raw ``##`` heading as load-bearing/speculative/other.

    Tolerant of trailing text (``"Decisions (load-bearing)"``) and case
    (``"DECISIONS"``) so markdown drift can't silently exempt a section from
    the gate.
    """
    folded = heading.casefold()
    if folded.startswith("decisions"):
        return "decisions"
    if folded.startswith("notes"):
        return "notes"
    return "other"


@dataclass
class Claim:
    """One provenance-tagged research claim.

    Attributes:
        tag: One of :data:`TAGS` (``VERIFIED``/``CITED``/``ASSUMED``).
        text: The claim text (source clause stripped).
        url: Source URL, or ``""`` when the claim carries no citation.
        quote: The quoted supporting passage, or ``""``.
        section: The ``##`` heading the claim sits under (e.g. ``Decisions``).
    """

    tag: str
    text: str
    url: str = ""
    quote: str = ""
    section: str = ""


def parse_research(text: str) -> list:
    """Parse RESEARCH.md into :class:`Claim` records in document order.

    Only ``[-*+] [TAG] …`` bullet lines are claims; other lines are ignored. Each
    claim records the ``##`` section it appears under so the gate can tell
    load-bearing (``Decisions``) from speculative (``Notes``) claims.

    Args:
        text: The RESEARCH.md contents.

    Returns:
        A list of :class:`Claim` (possibly empty).
    """
    claims: list = []
    section = ""
    for line in (text or "").splitlines():
        heading = _HEADING.match(line)
        if heading:
            section = heading.group(1).strip()
            continue
        match = _CLAIM.match(line)
        if match:
            claims.append(Claim(
                tag=match.group("tag"),
                text=match.group("text").strip(),
                url=(match.group("url") or ""),
                quote=(match.group("quote") or ""),
                section=section,
            ))
    return claims


def check_tag_coverage(text: str) -> tuple[bool, str]:
    """Deterministically gate provenance tagging.

    Fails when: a ``- `` bullet under ``## Decisions``/``## Notes`` is not a
    valid ``[VERIFIED|CITED|ASSUMED]`` claim (untagged / unknown tag), or any
    ``## Decisions`` claim is ``[ASSUMED]`` (a load-bearing guess).

    Args:
        text: The RESEARCH.md contents.

    Returns:
        ``(ok, reason)`` — ``reason`` empty on success, else the first failure.
    """
    section = ""
    kind = "other"
    for line in (text or "").splitlines():
        heading = _HEADING.match(line)
        if heading:
            section = heading.group(1).strip()
            kind = _section_kind(section)
            continue
        if kind not in ("decisions", "notes"):
            continue
        if not _BULLET.match(line):
            continue
        match = _CLAIM.match(line)
        if not match or match.group("tag") not in TAGS:
            return False, f"untagged or unknown-tag claim under ## {section}: {line.strip()!r}"
        if kind == "decisions" and match.group("tag") == "ASSUMED":
            return False, (f"load-bearing claim under ## Decisions is [ASSUMED] "
                           f"(assumed, not sourced): {match.group('text').strip()!r}")
    return True, ""


_CITE_HEADING = re.compile(r"^##\s+(\S+)\s*$")


@dataclass
class CitationResult:
    """Outcome of the citation spot-check.

    Attributes:
        status: ``"ok"`` (all quotes found), ``"incomplete"`` (a selected URL
            has no fetched section yet — re-fetch), or ``"mismatch"`` (a fetched
            page does not contain the claimed quote — a fabricated citation).
        detail: Empty on ``ok``; otherwise names the offending URL/claim.
    """

    status: str
    detail: str = ""


_SMART_PUNCT = str.maketrans({
    "’": "'", "‘": "'", "‚": "'",
    "“": '"', "”": '"', "„": '"',
    "—": "-", "–": "-",
    "…": "...",
})


def _normalize(text: str) -> str:
    """Casefold and collapse whitespace for a robust substring compare.

    Also NFKC-normalizes and folds smart/typographic punctuation (curly
    quotes, em/en-dashes, ellipsis) to their ASCII equivalents so a claimed
    quote using straight punctuation still matches fetched HTML text using
    the Unicode variant of the SAME character. Other punctuation is left
    alone — a genuinely absent quote must still mismatch.
    """
    normalized = unicodedata.normalize("NFKC", text or "")
    folded = normalized.translate(_SMART_PUNCT)
    return " ".join(folded.split()).casefold()


def select_citations(claims: list, n: int = 5) -> list:
    """Return up to *n* citable claims (``VERIFIED``/``CITED`` with url+quote).

    Selection is deterministic: document order, capped at *n*. ``[ASSUMED]``
    claims and claims without a source clause are never spot-checked.

    Args:
        claims: Parsed :class:`Claim` list.
        n: Cap on the number of citations to spot-check (spec: 3–5).

    Returns:
        The selected claims (possibly empty).
    """
    citable = [c for c in claims
               if c.tag in ("VERIFIED", "CITED") and c.url and c.quote]
    return citable[:n]


def _parse_citations(citations_md: str) -> dict:
    """Parse the session's citations.md into ``{url: fetched_text}``."""
    fetched: dict = {}
    current = None
    buffer: list = []
    for line in (citations_md or "").splitlines():
        heading = _CITE_HEADING.match(line)
        if heading:
            if current is not None:
                fetched[current] = "\n".join(buffer)
            current = heading.group(1).strip()
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        fetched[current] = "\n".join(buffer)
    return fetched


def check_citations(selected: list, citations_md: str) -> CitationResult:
    """Compare each selected claim's quote against the session-fetched text.

    Pure and deterministic — the engine never fetches. A selected URL absent
    from *citations_md* yields ``incomplete`` (the session must fetch it); a
    present page missing the normalized quote yields ``mismatch`` (a fabricated
    or stale citation). Empty selection is trivially ``ok``.

    Args:
        selected: Claims from :func:`select_citations`.
        citations_md: The session's ``.loop-engineer/citations.md`` (``## <url>``
            sections holding fetched page text).

    Returns:
        A :class:`CitationResult`.
    """
    fetched = _parse_citations(citations_md)
    for claim in selected:
        if claim.url not in fetched:
            return CitationResult("incomplete", f"no fetched text for {claim.url}")
    for claim in selected:
        if _normalize(claim.quote) not in _normalize(fetched[claim.url]):
            return CitationResult("mismatch",
                                  f"claimed quote not found at {claim.url}: {claim.quote!r}")
    return CitationResult("ok", "")


def _is_placeholder(s: str) -> bool:
    """True for M3's literal empty-section placeholders (``_none_``/``_deferred_``)."""
    return s.strip() in ("_none_", "_deferred_")


def _spec_section(spec_md: str, heading: str) -> str:
    """Return the body text under a ``## heading`` in SPEC.md (until the next ##)."""
    lines = (spec_md or "").splitlines()
    out: list = []
    capturing = False
    for line in lines:
        m = _HEADING.match(line)
        if m:
            capturing = m.group(1).strip().lower() == heading.lower()
            continue
        if capturing:
            out.append(line)
    return "\n".join(out).strip()


@dataclass
class ResearchBrief:
    """A research brief derived from SPEC.md plus reused canonical memory.

    Attributes:
        questions: Fixed-form research questions derived from Build/Consider.
        known_facts: Canonical research values reused from prior runs (never
            re-researched).
    """

    questions: list
    known_facts: list

    @classmethod
    def from_spec(cls, spec_md: str, memory=None) -> "ResearchBrief":
        """Derive a brief from SPEC.md's Build/Consider plus canonical memory.

        Questions are templated deterministically (the engine never invents free
        text): one question per non-empty Build/Consider paragraph, plus a fixed
        stack/version question. Known facts come from ``memory.query(
        kind="research", status="canonical")``.

        Args:
            spec_md: The SPEC.md contents.
            memory: Optional :class:`~kickass_loop_engineer.memory.Memory`.

        Returns:
            A :class:`ResearchBrief`.
        """
        build = _spec_section(spec_md, "Build")
        consider = _spec_section(spec_md, "Consider")
        questions: list = []
        if build and not _is_placeholder(build):
            questions.append(f"What libraries, APIs, or versions are needed to build: {build}?")
        if consider and not _is_placeholder(consider):
            questions.append(f"What do these constraints require or rule out: {consider}?")
        questions.append("For each stack/API/version choice, what does the official "
                         "documentation say, and what is the exact supporting quote?")
        known_facts: list = []
        if memory is not None:
            known_facts = [row["value"] for row in
                           memory.query(kind="research", status="canonical")]
        return cls(questions=questions, known_facts=known_facts)

    def to_markdown(self) -> str:
        """Render the brief as a Markdown block for the research skill."""
        lines = ["# RESEARCH BRIEF", "", "## Questions"]
        lines += [f"- {q}" for q in self.questions]
        if self.known_facts:
            lines += ["", "## Already known (do not re-research)"]
            lines += [f"- {f}" for f in self.known_facts]
        lines.append("")
        return "\n".join(lines)


# Cap on the slugified claim text used as the memory key suffix
# (`research.<slug>`) in ingest_research — keeps keys short and stable.
_SLUG_MAX_LEN = 40


def ingest_research(research_md: str, memory, run_id: str) -> int:
    """Record a passing RESEARCH.md's claims into memory as ``kind='research'``.

    Only ``## Decisions`` claims (the load-bearing, sourced facts) are recorded,
    keyed ``research.<slug>``. Status defaults to ``working``; the engine's
    ``memory.consolidate(run_id)`` on run SUCCESS promotes them to canonical.

    Args:
        research_md: A RESEARCH.md that has passed the gate.
        memory: A :class:`~kickass_loop_engineer.memory.Memory`.
        run_id: The current run id.

    Returns:
        The number of claims recorded.
    """
    count = 0
    for claim in parse_research(research_md):
        if _section_kind(claim.section) != "decisions":
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", claim.text.lower()).strip("-")[:_SLUG_MAX_LEN] or "claim"
        value = claim.text + (f' (source: {claim.url})' if claim.url else "")
        memory.record(f"research.{slug}", value, run_id=run_id, kind="research")
        count += 1
    return count
