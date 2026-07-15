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


def test_mark_architect_done_persists_durable_flag(tmp_path):
    c = PipelineCursor(str(tmp_path))
    c.start(run_id="r1", objective_goal="g")
    assert not PipelineCursor(str(tmp_path)).load().get("architect_done")
    c.mark_architect_done()
    reloaded = PipelineCursor(str(tmp_path)).load()
    assert reloaded["architect_done"] is True
    events = [json.loads(line) for line in open(c.events_path, encoding="utf-8")]
    assert any(e["event"] == "architect_done" for e in events)


def test_mark_architect_done_survives_later_set_stage(tmp_path):
    c = PipelineCursor(str(tmp_path))
    c.start(run_id="r1", objective_goal="g")
    c.mark_architect_done()
    c.set_stage("ensemble", "running", {"slice": "core"})
    assert PipelineCursor(str(tmp_path)).load()["architect_done"] is True


def test_mark_repro_path_persists_durable_key_and_survives_set_stage(tmp_path):
    c = PipelineCursor(str(tmp_path))
    c.start(run_id="r1", objective_goal="g")
    assert not PipelineCursor(str(tmp_path)).load().get("repro_path")
    c.mark_repro_path("tests/test_repro_x.py")
    c.set_stage("ensemble", "running", {"slice": "core"})
    reloaded = PipelineCursor(str(tmp_path)).load()
    assert reloaded["repro_path"] == "tests/test_repro_x.py"
    events = [json.loads(line) for line in open(c.events_path, encoding="utf-8")]
    assert any(e["event"] == "repro_committed"
               and e["payload"] == {"repro_path": "tests/test_repro_x.py"}
               for e in events)


def test_emit_appends_event_line_without_touching_pipeline_json(tmp_path):
    c = PipelineCursor(str(tmp_path))
    c.start(run_id="r1", objective_goal="g")
    before = open(c.path, encoding="utf-8").read()
    c.emit("attempt_started", {"slice": "core", "attempt": 0})
    assert open(c.path, encoding="utf-8").read() == before, \
        "journal events must never thrash pipeline.json"
    last = json.loads(open(c.events_path, encoding="utf-8").read().splitlines()[-1])
    assert last["event"] == "attempt_started"
    assert last["payload"] == {"slice": "core", "attempt": 0}
    assert last["run_id"] == "r1"
    assert last["ts"]


def test_emit_never_dedupes_repeated_events(tmp_path):
    c = PipelineCursor(str(tmp_path))
    c.start(run_id="r1", objective_goal="g")
    c.emit("mode_resolved", {"mode": "build"})
    c.emit("mode_resolved", {"mode": "build"})
    lines = open(c.events_path, encoding="utf-8").read().splitlines()
    repeats = [l for l in lines if json.loads(l)["event"] == "mode_resolved"]
    assert len(repeats) == 2, "replay tolerance: consumers dedupe, the journal never does"
