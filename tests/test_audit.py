"""Tests for the read-only audit sweep (``audit.run_audit`` and helpers).

Fakes throughout: a FakeProvider-backed ``Reviewer`` (the shared ``FINDING:``
parser is exercised through its real ``review`` path) and a ``FakePolicy``
recording every gate command. ``run_audit`` itself must stay read-only: the
only file it may write is ``<workspace>/.loop-engineer/findings.md``.
"""
import os
import subprocess

from kickass_loop_engineer.agents import Reviewer
from kickass_loop_engineer.audit import (
    AUDIT_SELECT_BUDGET_BYTES,
    chunk_files,
    fallback_files,
    read_only_gates,
    run_audit,
)
from kickass_loop_engineer.gates import GateSpec
from kickass_loop_engineer.repomap import DEFAULT_SELECT_BUDGET_BYTES

from helpers import FakePolicy, FakeProvider


def _reviewer(responses, **provider_kwargs):
    return Reviewer(FakeProvider(list(responses), **provider_kwargs))


def _ws(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return str(ws)


def _git_repo(tmp_path, files):
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=repo, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return str(repo)


# --- chunking -----------------------------------------------------------


def test_chunk_files_groups_up_to_the_per_chunk_budget():
    files = [("a.py", "x" * 10), ("b.py", "y" * 10), ("c.py", "z" * 10)]
    chunks = chunk_files(files, chunk_budget_bytes=20)
    assert chunks == [[("a.py", "x" * 10), ("b.py", "y" * 10)],
                      [("c.py", "z" * 10)]]


def test_chunk_files_preserves_input_order_and_is_deterministic():
    files = [("b.py", "bb"), ("a.py", "aa"), ("c.py", "cc")]
    first = chunk_files(files, chunk_budget_bytes=4)
    second = chunk_files(files, chunk_budget_bytes=4)
    assert first == second == [[("b.py", "bb"), ("a.py", "aa")],
                               [("c.py", "cc")]]


def test_chunk_files_gives_an_oversized_file_its_own_chunk():
    files = [("big.py", "x" * 100), ("small.py", "y" * 5)]
    chunks = chunk_files(files, chunk_budget_bytes=10)
    assert chunks == [[("big.py", "x" * 100)], [("small.py", "y" * 5)]]


def test_chunk_files_empty_input_yields_no_chunks():
    assert chunk_files([]) == []


def test_audit_select_budget_is_larger_than_the_enhance_default():
    assert AUDIT_SELECT_BUDGET_BYTES > DEFAULT_SELECT_BUDGET_BYTES


# --- reviewer calls: one per chunk, every one ledgered -------------------


def test_one_reviewer_call_per_chunk_each_ledgered(tmp_path):
    reviewer = _reviewer(["NO FINDINGS", "NO FINDINGS"])
    files = [("a.py", "x" * 30000), ("b.py", "y" * 30000)]  # 2 chunks at default budget
    usages = []
    path, findings = run_audit(
        reviewer, "MAP", files, [], _ws(tmp_path), None,
        on_usage=usages.append)

    assert len(reviewer.provider.calls) == 2
    assert len(usages) == 2
    assert all(hasattr(u, "cost_usd") for u in usages)
    assert findings == []


def test_chunk_prompt_carries_repo_map_and_full_file_contents(tmp_path):
    reviewer = _reviewer(["NO FINDINGS"])
    run_audit(reviewer, "THE-REPO-MAP", [("mod.py", "SECRET_CONTENT = 1\n")],
              [], _ws(tmp_path), None, on_usage=None)

    (_system, user), = reviewer.provider.calls
    assert "THE-REPO-MAP" in user
    assert "--- mod.py ---" in user
    assert "SECRET_CONTENT = 1" in user
    assert "FINDING" in user  # the prompt instructs the FINDING: format


# --- FINDING parsing + file-ref prefixing --------------------------------


def test_finding_already_naming_a_chunk_file_keeps_its_ref(tmp_path):
    reviewer = _reviewer(["FINDING: a.py: division by zero in divide()"])
    _path, findings = run_audit(
        reviewer, "", [("a.py", "def divide(x): return x / 0\n")],
        [], _ws(tmp_path), None, on_usage=None)

    assert findings == [{"file": "a.py",
                         "text": "a.py: division by zero in divide()"}]


def test_finding_without_a_ref_is_prefixed_with_the_chunk_files(tmp_path):
    reviewer = _reviewer(["FINDING: unchecked input reaches eval"])
    _path, findings = run_audit(
        reviewer, "", [("b.py", "eval(input())\n")],
        [], _ws(tmp_path), None, on_usage=None)

    assert findings == [{"file": "b.py",
                         "text": "b.py: unchecked input reaches eval"}]


def test_path_match_is_boundary_aware_not_substring(tmp_path):
    # "a.py" is a substring of "data.py": a finding referencing only data.py
    # must file under data.py, never under a.py.
    reviewer = _reviewer(["FINDING: data.py: leaks secret"])
    files = [("a.py", "A = 1\n"), ("data.py", "SECRET = 'x'\n")]  # one chunk
    _path, findings = run_audit(reviewer, "", files, [], _ws(tmp_path), None,
                                on_usage=None)

    assert findings == [{"file": "data.py",
                         "text": "data.py: leaks secret"}]


def test_most_specific_matching_path_wins(tmp_path):
    # Both "a.py" and "sub/a.py" genuinely appear as chunk paths; a finding
    # naming "sub/a.py" belongs there — the longer path must win, and the
    # embedded "a.py" occurrence inside it must not match.
    reviewer = _reviewer(["FINDING: sub/a.py: broken import"])
    files = [("a.py", "A = 1\n"), ("sub/a.py", "B = 2\n")]  # one chunk
    _path, findings = run_audit(reviewer, "", files, [], _ws(tmp_path), None,
                                on_usage=None)

    assert findings == [{"file": "sub/a.py",
                         "text": "sub/a.py: broken import"}]


def test_path_at_sentence_end_still_matches(tmp_path):
    reviewer = _reviewer(["FINDING: dead code in a.py."])
    _path, findings = run_audit(
        reviewer, "", [("a.py", "A = 1\n"), ("b.py", "B = 2\n")],
        [], _ws(tmp_path), None, on_usage=None)

    assert findings == [{"file": "a.py", "text": "dead code in a.py."}]


def test_findings_parsed_with_the_shared_parser_across_chunks(tmp_path):
    reviewer = _reviewer([
        "FINDING: a.py: first\nnoise line\nFINDING: a.py: second",
        "NO FINDINGS",
    ])
    files = [("a.py", "x" * 30000), ("b.py", "y" * 30000)]
    _path, findings = run_audit(reviewer, "", files, [], _ws(tmp_path), None,
                                on_usage=None)

    assert [f["text"] for f in findings] == ["a.py: first", "a.py: second"]


# --- read-only gate filtering by category --------------------------------


def test_read_only_gates_keeps_only_security_and_data_leak():
    gates = [
        GateSpec(name="unit", command="pytest -q"),
        GateSpec(name="security", command="bandit -r ."),
        GateSpec(name="data_leak", command="gitleaks detect"),
        GateSpec(name="ui", command="npx playwright test"),
        GateSpec(name="architecture", command="lint-imports"),
        GateSpec(name="performance", command="pytest-benchmark"),
        GateSpec(name="smoke", command="make test"),
    ]
    kept = read_only_gates(gates)
    assert [g.name for g in kept] == ["security", "data_leak"]


def test_read_only_gates_neutralize_prove_and_observe():
    gates = [GateSpec(name="security", command="bandit -r .", prove=True,
                      observe="cat report.txt")]
    kept = read_only_gates(gates)
    assert kept[0].prove is False
    assert kept[0].observe == ""


def test_run_audit_runs_only_read_only_gate_commands(tmp_path):
    policy = FakePolicy(passed=True)
    gates = [
        GateSpec(name="unit", command="pytest -q"),
        GateSpec(name="security", command="bandit -r ."),
        GateSpec(name="data_leak", command="gitleaks detect"),
    ]
    run_audit(_reviewer(["NO FINDINGS"]), "", [("a.py", "A = 1\n")],
              gates, _ws(tmp_path), policy, on_usage=None)

    assert [cmd for (cmd, _cwd) in policy.runs] == ["bandit -r .",
                                                    "gitleaks detect"]


def test_run_audit_without_read_only_gates_runs_no_commands(tmp_path):
    policy = FakePolicy(passed=True)
    run_audit(_reviewer(["NO FINDINGS"]), "", [("a.py", "A = 1\n")],
              [GateSpec(name="unit", command="pytest -q")],
              _ws(tmp_path), policy, on_usage=None)

    assert policy.runs == []


# --- findings.md rendering ------------------------------------------------


def test_findings_md_written_under_loop_engineer_with_advisory_framing(tmp_path):
    ws = _ws(tmp_path)
    path, _findings = run_audit(
        _reviewer(["FINDING: a.py: bad thing"]), "", [("a.py", "A = 1\n")],
        [], ws, None, on_usage=None)

    assert path == os.path.join(ws, ".loop-engineer", "findings.md")
    text = open(path, encoding="utf-8").read()
    assert "advisory" in text.lower()
    assert "pass/fail" in text.lower()
    assert "### a.py" in text
    assert "a.py: bad thing" in text


def test_findings_md_gate_section_reports_pass_fail(tmp_path):
    policy = FakePolicy(passed=True)
    path, _findings = run_audit(
        _reviewer(["NO FINDINGS"]), "", [("a.py", "A = 1\n")],
        [GateSpec(name="security", command="bandit -r ."),
         GateSpec(name="data_leak", command="gitleaks detect")],
        _ws(tmp_path), policy, on_usage=None)

    text = open(path, encoding="utf-8").read()
    assert "Gate results" in text
    assert "PASS security" in text
    assert "PASS data_leak" in text


def test_findings_md_gate_section_reports_a_failure(tmp_path):
    policy = FakePolicy(passed=False)
    path, _findings = run_audit(
        _reviewer(["NO FINDINGS"]), "", [("a.py", "A = 1\n")],
        [GateSpec(name="security", command="bandit -r .")],
        _ws(tmp_path), policy, on_usage=None)

    text = open(path, encoding="utf-8").read()
    assert "FAIL security" in text


def test_findings_md_without_read_only_gates_says_so(tmp_path):
    path, _findings = run_audit(
        _reviewer(["NO FINDINGS"]), "", [("a.py", "A = 1\n")],
        [], _ws(tmp_path), None, on_usage=None)

    text = open(path, encoding="utf-8").read()
    assert "No read-only gates configured" in text


def test_findings_md_rendering_is_deterministic(tmp_path):
    ws = _ws(tmp_path)
    args = ("", [("a.py", "A = 1\n"), ("b.py", "B = 2\n")], [], ws, None)
    path, _ = run_audit(_reviewer(["FINDING: a.py: x\nFINDING: b.py: y"]),
                        *args, on_usage=None)
    first = open(path, encoding="utf-8").read()
    path, _ = run_audit(_reviewer(["FINDING: a.py: x\nFINDING: b.py: y"]),
                        *args, on_usage=None)
    second = open(path, encoding="utf-8").read()
    assert first == second


# --- zero-findings and zero-files paths -----------------------------------


def test_zero_findings_still_writes_the_report(tmp_path):
    path, findings = run_audit(
        _reviewer(["NO FINDINGS"]), "", [("a.py", "A = 1\n")],
        [], _ws(tmp_path), None, on_usage=None)

    assert findings == []
    text = open(path, encoding="utf-8").read()
    assert "No findings" in text


def test_zero_files_makes_no_reviewer_calls_and_still_reports(tmp_path):
    reviewer = _reviewer([])
    path, findings = run_audit(reviewer, "", [], [], _ws(tmp_path), None,
                               on_usage=None)

    assert reviewer.provider.calls == []
    assert findings == []
    assert os.path.isfile(path)


def test_fallback_files_returns_all_tracked_files_up_to_budget(tmp_path):
    repo = _git_repo(tmp_path, {"a.py": "A = 1\n", "sub/b.txt": "hello\n"})
    files = fallback_files(repo, AUDIT_SELECT_BUDGET_BYTES)
    assert dict(files) == {"a.py": "A = 1\n", "sub/b.txt": "hello\n"}


def test_fallback_files_skips_files_over_the_remaining_budget(tmp_path):
    repo = _git_repo(tmp_path, {"a.py": "x" * 100, "b.py": "y" * 5})
    files = fallback_files(repo, 10)
    assert files == [("b.py", "y" * 5)]


def test_fallback_files_empty_for_a_non_repo(tmp_path):
    assert fallback_files(_ws(tmp_path), 1000) == []


# --- read-only guarantee at the run_audit level ----------------------------


def test_run_audit_writes_nothing_outside_loop_engineer(tmp_path):
    repo = _git_repo(tmp_path, {"a.py": "A = 1\n"})

    def snapshot():
        seen = {}
        for base, _dirs, names in os.walk(repo):
            if ".loop-engineer" in base:
                continue
            for name in names:
                full = os.path.join(base, name)
                with open(full, "rb") as handle:
                    seen[full] = (os.stat(full).st_mtime_ns, handle.read())
        return seen

    before = snapshot()
    run_audit(_reviewer(["FINDING: a.py: planted"]), "MAP",
              [("a.py", "A = 1\n")], [], repo, None, on_usage=None)
    assert snapshot() == before


# --- per-chunk budget check (rider 2) --------------------------------------


def test_budget_check_false_before_second_chunk_stops_and_renders_partial(tmp_path):
    reviewer = _reviewer(["FINDING: a.py: leak", "FINDING: b.py: bug"])
    files = [("a.py", "x" * 30000), ("b.py", "y" * 30000)]  # 2 chunks at default
    seen = {"n": 0}

    def budget_check():
        seen["n"] += 1
        return seen["n"] == 1  # continue for chunk 1, stop before chunk 2

    path, findings = run_audit(reviewer, "MAP", files, [], _ws(tmp_path), None,
                               on_usage=None, budget_check=budget_check)

    assert len(reviewer.provider.calls) == 1, "only chunk 1 was reviewed"
    assert len(findings) == 1 and "a.py" in findings[0]["file"]
    text = open(path, encoding="utf-8").read()
    assert "TRUNCATED" in text


def test_budget_check_defaults_to_running_every_chunk(tmp_path):
    reviewer = _reviewer(["NO FINDINGS", "NO FINDINGS"])
    files = [("a.py", "x" * 30000), ("b.py", "y" * 30000)]
    path, _findings = run_audit(reviewer, "MAP", files, [], _ws(tmp_path), None,
                                on_usage=None)

    assert len(reviewer.provider.calls) == 2
    assert "TRUNCATED" not in open(path, encoding="utf-8").read()


def test_budget_check_can_stop_before_the_first_chunk(tmp_path):
    reviewer = _reviewer(["FINDING: a.py: x"])
    path, findings = run_audit(reviewer, "", [("a.py", "A = 1\n")], [],
                               _ws(tmp_path), None, on_usage=None,
                               budget_check=lambda: False)

    assert reviewer.provider.calls == [], "nothing was reviewed"
    assert findings == []
    assert "TRUNCATED" in open(path, encoding="utf-8").read()
