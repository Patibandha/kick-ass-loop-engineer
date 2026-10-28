"""Gate evidence names the failing tests even when long output pushes them out of the tail.

On CI the temp paths in pytest's failure lines are long, and the bare 200-character tail held only the end of an
assertion message, so a stall report could not say which test failed.
"""

from kickass_loop_engineer.gates import DEFAULT_GATE_CMD
from kickass_loop_engineer.guardrails import VerificationPolicy
from kickass_loop_engineer.verifier import run_gate

LONG = "x" * 400


def _repo(tmp_path):
    folder = tmp_path / ("deep_" + "d" * 60) / ("nested_" + "n" * 60)
    folder.mkdir(parents=True)
    (folder / "test_long.py").write_text(
        "def test_named_failure():\n"
        f"    assert False, '{LONG}'\n", encoding="utf-8", newline="\n")
    return folder


def test_a_red_gate_names_the_failing_test_beyond_the_tail(tmp_path, monkeypatch):
    # A wide terminal, as on CI: pytest then prints the whole assertion on the FAILED line, past the bare tail.
    monkeypatch.setenv("COLUMNS", "2000")
    repo = _repo(tmp_path)

    record = run_gate("unit", f"{DEFAULT_GATE_CMD} -p no:cacheprovider", workspace=str(repo),
                      policy=VerificationPolicy())

    assert record.passed is False
    assert "FAILED test_long.py::test_named_failure" in record.evidence


def test_the_evidence_stays_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "2000")
    repo = _repo(tmp_path)

    record = run_gate("unit", f"{DEFAULT_GATE_CMD} -p no:cacheprovider", workspace=str(repo),
                      policy=VerificationPolicy())

    assert len(record.evidence) <= len("exit=1; ") + 5 * 301 + 200
