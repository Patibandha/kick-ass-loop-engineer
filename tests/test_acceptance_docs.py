"""Acceptance docs: the authority map and proof-binding inventory stay complete and truthful in form."""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.proof_binding

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs" / "acceptance"
PROOF_BINDING = DOCS / "proof-binding.md"
AUTHORITY_MAP = DOCS / "authority-map.md"
DECISIONS = DOCS / "decisions.md"

ITEMS = ["Candidate bytes", "Workspace identity", "Gate command", "Configuration",
         "Environment", "Dependencies", "Git config", "Plugins"]
ALLOWED = {"BOUND", "MUTABLE-ON-PURPOSE"}
MAX_PAGE_LINES = 60
SRC_PATH = re.compile(r"src/[A-Za-z0-9_./-]+\.py")
LINK = re.compile(r"\]\(decisions\.md#([^)]+)\)")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _anchors(markdown: str) -> set:
    """GitHub-style heading slugs of a markdown document."""
    slugs = set()
    for line in markdown.splitlines():
        match = re.match(r"#+\s+(.*\S)\s*$", line)
        if match:
            slug = re.sub(r"[^a-z0-9 _-]", "", match.group(1).lower()).replace(" ", "-")
            slugs.add(slug)
    return slugs


def _rows(markdown: str) -> list:
    """Data rows of the first table: cells per row, header and separator excluded."""
    rows = []
    for line in markdown.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if set("".join(cells)) <= set("-: ") or cells[0] == "Item":
            continue
        rows.append(cells)
    return rows


def test_both_pages_exist():
    assert PROOF_BINDING.is_file()
    assert AUTHORITY_MAP.is_file()
    assert DECISIONS.is_file()


@pytest.mark.parametrize("path", [PROOF_BINDING, AUTHORITY_MAP])
def test_pages_stay_one_page(path):
    assert len(_read(path).splitlines()) <= MAX_PAGE_LINES


def test_proof_binding_has_exactly_the_eight_rows():
    rows = _rows(_read(PROOF_BINDING))
    assert [row[0] for row in rows] == ITEMS


def test_no_row_is_left_open():
    assert _read(PROOF_BINDING).count("UNDECIDED") == 0


def test_every_row_has_an_allowed_status():
    for row in _rows(_read(PROOF_BINDING)):
        assert row[1] in ALLOWED, row


def test_every_row_links_to_an_existing_decision_anchor():
    anchors = _anchors(_read(DECISIONS))
    for row in _rows(_read(PROOF_BINDING)):
        found = LINK.findall(row[3])
        assert len(found) == 1, row
        assert found[0] in anchors, row


def test_every_row_cites_a_source_file():
    for row in _rows(_read(PROOF_BINDING)):
        assert SRC_PATH.search(row[2]), row


@pytest.mark.parametrize("path", [PROOF_BINDING, AUTHORITY_MAP])
def test_every_cited_src_path_exists(path):
    cited = set(SRC_PATH.findall(_read(path)))
    assert cited
    for rel in sorted(cited):
        assert (ROOT / rel).is_file(), rel


def test_authority_map_covers_every_actor():
    text = _read(AUTHORITY_MAP)
    for actor in ("Builder model", "Reviewer model", "Gate command", "Engine", "Meet"):
        assert f"| {actor} |" in text, actor


def test_decisions_has_the_open_questions_heading():
    assert "open-questions-for-meet" in _anchors(_read(DECISIONS))
