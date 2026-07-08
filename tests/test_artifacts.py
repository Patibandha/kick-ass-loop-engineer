import pytest

from kickass_loop_engineer.artifacts import ARTIFACT_SCHEMAS, validate_artifact


def _write(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_schema_registry_covers_m2_stages():
    assert {"plan_v1", "review_v1", "qa_v1", "security_v1", "ship_v1"} <= set(ARTIFACT_SCHEMAS)
    for spec in ARTIFACT_SCHEMAS.values():
        assert spec["artifact"].startswith(".loop-engineer/")


def test_missing_artifact_fails(tmp_path):
    ok, reason = validate_artifact(str(tmp_path), "plan_v1")
    assert not ok and "missing" in reason


def test_empty_artifact_fails(tmp_path):
    _write(tmp_path, ".loop-engineer/plan.md", "   \n")
    ok, reason = validate_artifact(str(tmp_path), "plan_v1")
    assert not ok and "empty" in reason


def test_plan_requires_a_task_checkbox(tmp_path):
    _write(tmp_path, ".loop-engineer/plan.md", "# Plan\nprose only, no tasks\n")
    ok, reason = validate_artifact(str(tmp_path), "plan_v1")
    assert not ok and "checkbox" in reason
    _write(tmp_path, ".loop-engineer/plan.md", "# Plan\n- [ ] Task 1: do the thing\n")
    ok, _ = validate_artifact(str(tmp_path), "plan_v1")
    assert ok


@pytest.mark.parametrize("schema,artifact,good,bad", [
    ("review_v1", ".loop-engineer/review.md", "NO FINDINGS\n", "looks nice\n"),
    ("qa_v1", ".loop-engineer/qa.md", "VERDICT: PASS\n", "ran some stuff\n"),
    ("security_v1", ".loop-engineer/security.md", "VERDICT: FAIL\nFINDING: xss\n", "n/a\n"),
    ("ship_v1", ".loop-engineer/ship.md", "SHIPPED: v1 tag pushed\n", "maybe later\n"),
])
def test_marker_schemas(tmp_path, schema, artifact, good, bad):
    _write(tmp_path, artifact, bad)
    ok, _ = validate_artifact(str(tmp_path), schema)
    assert not ok
    _write(tmp_path, artifact, good)
    ok, _ = validate_artifact(str(tmp_path), schema)
    assert ok


def test_unknown_schema_fails_loudly(tmp_path):
    ok, reason = validate_artifact(str(tmp_path), "nope_v9")
    assert not ok and "unknown schema" in reason


def test_research_and_citations_schemas_registered():
    assert "research_v1" in ARTIFACT_SCHEMAS
    assert ARTIFACT_SCHEMAS["research_v1"]["artifact"] == ".loop-engineer/RESEARCH.md"
    assert "citations_v1" in ARTIFACT_SCHEMAS


def test_research_v1_requires_decisions_and_a_tagged_claim(tmp_path):
    _write(tmp_path, ".loop-engineer/RESEARCH.md", "# RESEARCH\n## Notes\n- prose\n")
    ok, reason = validate_artifact(str(tmp_path), "research_v1")
    assert not ok  # no ## Decisions + no tagged claim
    _write(tmp_path, ".loop-engineer/RESEARCH.md",
           "# RESEARCH\n## Decisions\n- [VERIFIED] x (source: https://a \"q\")\n")
    ok, _ = validate_artifact(str(tmp_path), "research_v1")
    assert ok


def test_research_v1_accepts_tolerant_decisions_headings(tmp_path):
    # research.py's check_tag_coverage/_section_kind treat any heading whose
    # casefolded text starts with "decisions" as load-bearing (trailing text
    # and case allowed). The shape gate must accept the same headings or the
    # two gates disagree.
    _write(tmp_path, ".loop-engineer/RESEARCH.md",
           "# RESEARCH\n## Decisions (load-bearing)\n"
           '- [VERIFIED] x (source: https://a "q")\n')
    ok, reason = validate_artifact(str(tmp_path), "research_v1")
    assert ok, reason

    _write(tmp_path, ".loop-engineer/RESEARCH.md",
           "# RESEARCH\n## DECISIONS\n"
           '- [VERIFIED] x (source: https://a "q")\n')
    ok, reason = validate_artifact(str(tmp_path), "research_v1")
    assert ok, reason


def test_citations_v1_requires_a_url_section(tmp_path):
    _write(tmp_path, ".loop-engineer/citations.md", "no sections here\n")
    ok, _ = validate_artifact(str(tmp_path), "citations_v1")
    assert not ok
    _write(tmp_path, ".loop-engineer/citations.md", "## https://a.example\nfetched text\n")
    ok, _ = validate_artifact(str(tmp_path), "citations_v1")
    assert ok
