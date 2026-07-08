import pytest

from kickass_loop_engineer.research import (
    Claim, TAGS, check_tag_coverage, parse_research,
)
from kickass_loop_engineer.research import (
    CitationResult, check_citations, select_citations,
)

GOOD = """# RESEARCH

## Decisions
- [VERIFIED] Use Textual >= 0.60 (source: https://textual.io "RAD framework for Python")
- [CITED] Piper runs on CPU (source: https://github.com/rhasspy/piper "does not require a GPU")

## Notes
- [ASSUMED] User prefers dark theme
"""


def test_tags_are_the_three_provenance_tags():
    assert TAGS == ("VERIFIED", "CITED", "ASSUMED")


def test_parse_research_extracts_tagged_claims_with_sections():
    claims = parse_research(GOOD)
    assert [c.tag for c in claims] == ["VERIFIED", "CITED", "ASSUMED"]
    assert claims[0].section == "Decisions"
    assert claims[0].url == "https://textual.io"
    assert claims[0].quote == "RAD framework for Python"
    assert claims[2].section == "Notes"
    assert claims[2].url == "" and claims[2].quote == ""


def test_tag_coverage_passes_on_well_tagged_doc():
    ok, reason = check_tag_coverage(GOOD)
    assert ok and reason == ""


def test_tag_coverage_fails_on_untagged_claim_under_decisions():
    md = "## Decisions\n- Use Textual (no tag here)\n"
    ok, reason = check_tag_coverage(md)
    assert not ok and "untagged" in reason


def test_tag_coverage_fails_on_load_bearing_assumed():
    md = ('## Decisions\n- [ASSUMED] Use Postgres 16\n'
          '## Notes\n- [VERIFIED] logging works (source: https://x "logs")\n')
    ok, reason = check_tag_coverage(md)
    assert not ok
    assert "load-bearing" in reason and "assumed" in reason.lower()


def test_tag_coverage_allows_assumed_under_notes():
    md = "## Decisions\n- [VERIFIED] x is y\n## Notes\n- [ASSUMED] maybe z\n"
    ok, _ = check_tag_coverage(md)
    assert ok


def test_tag_coverage_trivial_empty_decisions_passes():
    md = "## Decisions\n\n## Notes\n- [ASSUMED] nothing load-bearing here\n"
    ok, _ = check_tag_coverage(md)
    assert ok


def test_tag_coverage_rejects_unknown_tag():
    ok, reason = check_tag_coverage("## Decisions\n- [GUESS] x\n")
    assert not ok and "untagged" in reason  # unknown tag == not one of TAGS


# --- Gap 1: markdown-format-drift bypasses (each MUST fail the gate) ---

def test_tag_coverage_fails_on_asterisk_bullet_assumed_under_decisions():
    md = "## Decisions\n* [ASSUMED] use Postgres\n"
    ok, reason = check_tag_coverage(md)
    assert not ok and "load-bearing" in reason


def test_tag_coverage_fails_on_indented_bullet_assumed_under_decisions():
    md = "## Decisions\n  - [ASSUMED] use Postgres\n"
    ok, reason = check_tag_coverage(md)
    assert not ok and "load-bearing" in reason


def test_tag_coverage_fails_on_malformed_bullet_missing_space_after_dash():
    md = "## Decisions\n-[ASSUMED] use Postgres\n"
    ok, reason = check_tag_coverage(md)
    assert not ok and "untagged" in reason


def test_tag_coverage_fails_on_decisions_heading_with_trailing_text():
    md = "## Decisions (load-bearing)\n- [ASSUMED] use Postgres\n"
    ok, reason = check_tag_coverage(md)
    assert not ok and "load-bearing" in reason


def test_tag_coverage_fails_on_uppercase_decisions_heading():
    md = "## DECISIONS\n- [ASSUMED] use Postgres\n"
    ok, reason = check_tag_coverage(md)
    assert not ok and "load-bearing" in reason


def test_tag_coverage_passes_and_select_citations_sees_asterisk_bullet():
    md = '## Decisions\n* [VERIFIED] x (source: https://a "q")\n'
    ok, reason = check_tag_coverage(md)
    assert ok and reason == ""
    picks = select_citations(parse_research(md))
    assert len(picks) == 1
    assert picks[0].url == "https://a" and picks[0].quote == "q"


CITED_DOC = """## Decisions
- [VERIFIED] A (source: https://a.example "alpha passage")
- [CITED] B (source: https://b.example "beta passage")
- [VERIFIED] C no citation here
## Notes
- [CITED] D (source: https://d.example "delta passage")
- [ASSUMED] E maybe
"""


def test_select_citations_picks_cited_verified_with_source_in_order():
    picks = select_citations(parse_research(CITED_DOC))
    assert [c.url for c in picks] == ["https://a.example", "https://b.example",
                                      "https://d.example"]  # C has no source; E is ASSUMED


def test_select_citations_caps_at_five():
    claims = [Claim(tag="CITED", text=f"c{i}", url=f"https://x{i}", quote=f"q{i}",
                    section="Decisions") for i in range(9)]
    assert len(select_citations(claims, n=5)) == 5


def test_select_citations_empty_when_no_citable_claims():
    md = "## Decisions\n- [VERIFIED] self-evident, no source\n"
    assert select_citations(parse_research(md)) == []


def test_check_citations_ok_when_quotes_found_normalized():
    picks = select_citations(parse_research(CITED_DOC))
    citations = (
        '## https://a.example\nThe ALPHA   PASSAGE appears here.\n'
        '## https://b.example\nsome beta passage text\n'
        '## https://d.example\nleading delta passage trailing\n'
    )
    res = check_citations(picks, citations)
    assert res.status == "ok" and res.detail == ""


def test_check_citations_incomplete_when_url_section_missing():
    picks = select_citations(parse_research(CITED_DOC))
    citations = '## https://a.example\nalpha passage\n'  # b, d missing
    res = check_citations(picks, citations)
    assert res.status == "incomplete"
    assert "https://b.example" in res.detail


def test_check_citations_mismatch_when_quote_absent():
    picks = select_citations(parse_research(CITED_DOC))
    citations = (
        '## https://a.example\nalpha passage\n'
        '## https://b.example\nWRONG unrelated text\n'
        '## https://d.example\ndelta passage\n'
    )
    res = check_citations(picks, citations)
    assert res.status == "mismatch"
    assert "https://b.example" in res.detail


# --- Gap 2: unicode-punctuation false-mismatch (fail CLOSED is costly: RESEARCH_BLOCKED
# is terminal) — variant renderings of the SAME character must still compare "ok". ---

def test_check_citations_ok_with_curly_apostrophe_in_fetched_text():
    claim = Claim(tag="VERIFIED", text="c", url="https://a.example",
                  quote="can't run", section="Decisions")
    citations = "## https://a.example\nThe docs say you can’t run this twice.\n"
    res = check_citations([claim], citations)
    assert res.status == "ok" and res.detail == ""


def test_check_citations_ok_with_em_dash_in_fetched_text():
    claim = Claim(tag="VERIFIED", text="c", url="https://a.example",
                  quote="multi-word", section="Decisions")
    citations = "## https://a.example\nThis is a multi—word example.\n"
    res = check_citations([claim], citations)
    assert res.status == "ok" and res.detail == ""


def test_check_citations_mismatch_still_fails_for_genuinely_absent_quote():
    claim = Claim(tag="VERIFIED", text="c", url="https://a.example",
                  quote="totally different phrase", section="Decisions")
    citations = "## https://a.example\nThe docs say you can’t run this twice.\n"
    res = check_citations([claim], citations)
    assert res.status == "mismatch"


from kickass_loop_engineer.memory import Memory
from kickass_loop_engineer.research import ResearchBrief, ingest_research

SPEC_MD = """# SPEC

## Build
A retro TUI dungeon visualizer for loop-engineer runs.

## Consider
Must run in a terminal; reuse the events.jsonl stream.

## Done when (measurable)
python3 -m pytest -q passes
"""


def test_brief_from_spec_derives_questions_from_build_and_consider():
    brief = ResearchBrief.from_spec(SPEC_MD)
    text = brief.to_markdown()
    assert "## Questions" in text
    assert brief.questions  # non-empty
    assert any("TUI" in q or "terminal" in q or "events.jsonl" in q
               for q in brief.questions)


def test_brief_skips_none_placeholder_sections():
    spec = ("# SPEC\n\n"
            "## Build\nA retro TUI dungeon visualizer.\n\n"
            "## Consider\n\n_none_\n\n"
            "## Done when (measurable)\npython3 -m pytest -q passes\n")
    brief = ResearchBrief.from_spec(spec)
    assert not any("_none_" in q for q in brief.questions)
    assert any("TUI" in q for q in brief.questions)  # Build question survives
    # the fixed stack/version question is always present
    assert any("official" in q and "documentation" in q for q in brief.questions)


def test_brief_reuses_canonical_research_memory(tmp_path):
    mem = Memory(str(tmp_path / "m.db"))
    mem.record("research.textual", "Textual >= 0.60 chosen", run_id="r0", kind="research")
    mem.consolidate("r0")  # working -> canonical
    brief = ResearchBrief.from_spec(SPEC_MD, memory=mem)
    assert any("Textual" in known for known in brief.known_facts)
    mem.close()


def test_ingest_research_records_decision_claims_as_research_memory(tmp_path):
    mem = Memory(str(tmp_path / "m.db"))
    n = ingest_research(GOOD, mem, run_id="r1")   # GOOD from Task 2 (module-level)
    assert n >= 1
    rows = mem.query(kind="research", status="working")
    assert any("Textual" in r["value"] for r in rows)
    mem.close()
