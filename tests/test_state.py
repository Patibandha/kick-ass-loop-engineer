# tests/test_state.py
"""Tests for persistent run state and bounded round history."""
import json

from kickass_loop_engineer.state import RunState


def test_record_round_appends_jsonl(tmp_path):
    state = RunState(str(tmp_path), {"goal": "g", "done_when": "d"})
    state.record_round({"round_no": 1, "files_written": ["a.py"], "note": "one"})
    state.record_round({"round_no": 2, "files_written": [], "note": "two"})
    lines = open(state.rounds_path, encoding="utf-8").read().splitlines()
    assert [json.loads(l)["round_no"] for l in lines] == [1, 2]
    assert state.rounds_path.endswith("rounds.jsonl")


def test_state_md_renders_all_rounds(tmp_path):
    state = RunState(str(tmp_path), {"goal": "g", "done_when": "d"})
    state.record_round({"round_no": 1, "files_written": ["a.py"], "note": "hello"})
    md = open(state.state_md, encoding="utf-8").read()
    assert "hello" in md and "| 1 |" in md


def test_legacy_rounds_json_is_seeded(tmp_path):
    legacy = tmp_path / ".loop-engineer"
    legacy.mkdir()
    (legacy / "rounds.json").write_text('[{"round_no": 1, "files_written": []}]')
    state = RunState(str(tmp_path), {"goal": "g", "done_when": "d"})
    state.record_round({"round_no": 2, "files_written": []})
    md = open(state.state_md, encoding="utf-8").read()
    assert "| 1 |" in md and "| 2 |" in md


def test_legacy_seed_migrates_and_survives_reinit(tmp_path):
    legacy = tmp_path / ".loop-engineer"
    legacy.mkdir()
    (legacy / "rounds.json").write_text('[{"round_no": 1, "files_written": []}]')
    state1 = RunState(str(tmp_path), {"goal": "g", "done_when": "d"})
    state1.record_round({"round_no": 2, "files_written": []})
    state2 = RunState(str(tmp_path), {"goal": "g", "done_when": "d"})
    state2.record_round({"round_no": 3, "files_written": []})
    md = open(state2.state_md, encoding="utf-8").read()
    assert "| 1 |" in md and "| 2 |" in md and "| 3 |" in md
    assert not (legacy / "rounds.json").exists()


def test_existing_jsonl_is_replayed_on_init(tmp_path):
    state1 = RunState(str(tmp_path), {"goal": "g", "done_when": "d"})
    state1.record_round({"round_no": 1, "files_written": []})
    state2 = RunState(str(tmp_path), {"goal": "g", "done_when": "d"})
    state2.record_round({"round_no": 2, "files_written": []})
    md = open(state2.state_md, encoding="utf-8").read()
    assert "| 1 |" in md and "| 2 |" in md
