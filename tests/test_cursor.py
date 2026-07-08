import json

from kickass_loop_engineer.cursor import PipelineCursor


def test_start_writes_cursor_and_event(tmp_path):
    c = PipelineCursor(str(tmp_path))
    c.start(run_id="r1", objective_goal="build todo")
    data = json.loads(open(c.path, encoding="utf-8").read())
    assert data == {"protocol": 1, "run_id": "r1", "objective_goal": "build todo",
                    "stage": "start", "status": "started", "detail": {},
                    "completed_slices": []}
    events = open(c.events_path, encoding="utf-8").read().splitlines()
    assert json.loads(events[-1])["event"] == "stage"


def test_set_stage_and_complete_slice_round_trip(tmp_path):
    c = PipelineCursor(str(tmp_path))
    c.start(run_id="r1", objective_goal="g")
    c.set_stage("ensemble", "running", {"slice": "core"})
    c.complete_slice("core")
    reloaded = PipelineCursor(str(tmp_path)).load()
    assert reloaded["stage"] == "ensemble"
    assert reloaded["completed_slices"] == ["core"]


def test_load_returns_none_when_absent_or_corrupt(tmp_path):
    c = PipelineCursor(str(tmp_path))
    assert c.load() is None
    (tmp_path / ".loop-engineer").mkdir(exist_ok=True)
    open(c.path, "w").write("{corrupt")
    assert c.load() is None


def test_complete_slice_is_idempotent(tmp_path):
    c = PipelineCursor(str(tmp_path))
    c.start(run_id="r1", objective_goal="g")
    c.complete_slice("core")
    c.complete_slice("core")
    assert PipelineCursor(str(tmp_path)).load()["completed_slices"] == ["core"]
