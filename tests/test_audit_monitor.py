"""The diagnostic audit monitor and scripts/compare_audit.py. Diagnostic only: never guardrail evidence."""
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.audit_monitor

ROOT = Path(__file__).resolve().parent.parent
COMPARE = ROOT / "scripts" / "compare_audit.py"

# Engine code does a write and a subprocess; the script itself (test code) also writes.
CHILD = """
import sys
sys.path[:0] = [{src!r}, {tests!r}]
import audit_monitor
audit_monitor.install({log!r})
from kickass_loop_engineer.workspace import write_text_unlinked
from kickass_loop_engineer.verifier import _git
write_text_unlinked({engine_target!r}, "engine")
_git({work!r}, "--version")
open({test_target!r}, "w").write("test code")
"""


def _run_child(tmp_path, log):
    script = tmp_path / "child.py"
    script.write_text(CHILD.format(src=str(ROOT / "src"), tests=str(ROOT / "tests"), log=str(log),
                                   engine_target=str(tmp_path / "engine.txt"), work=str(tmp_path),
                                   test_target=str(tmp_path / "test.txt")), encoding="utf-8")
    return subprocess.run([sys.executable, "-I", str(script)], capture_output=True, text=True, timeout=60)


def _events(log):
    return [json.loads(line) for line in Path(log).read_text(encoding="utf-8").splitlines()]


def test_the_hook_records_a_write_and_a_subprocess_from_engine_code(tmp_path):
    log = tmp_path / "audit.jsonl"
    done = _run_child(tmp_path, log)
    assert done.returncode == 0, done.stderr
    seen = {(e["event"], e["func"]) for e in _events(log)}
    assert ("open", "write_text_unlinked") in seen
    assert ("subprocess.Popen", "_git") in seen
    assert all(e["file"].startswith("src/kickass_loop_engineer/") for e in _events(log))


def test_the_hook_ignores_events_from_test_code(tmp_path):
    log = tmp_path / "audit.jsonl"
    assert _run_child(tmp_path, log).returncode == 0
    assert not any("test.txt" in " ".join(e["args"]) for e in _events(log))
    assert (tmp_path / "test.txt").read_text(encoding="utf-8") == "test code"


def test_the_hook_never_raises_when_the_log_is_unwritable(tmp_path):
    done = _run_child(tmp_path, tmp_path / "no-such-folder" / "audit.jsonl")
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "engine.txt").read_text(encoding="utf-8") == "engine"
    assert (tmp_path / "test.txt").is_file()


def _compare(tmp_path, event):
    inventory = tmp_path / "inventory.md"
    inventory.write_text(
        "## Expected operations\n\n| Event | File | Function | Why |\n|---|---|---|---|\n"
        "| `open` | `src/x.py` | `save` | known |\n\n## Unexpected operations\n\n"
        "| `os.remove` | `src/x.py` | `wipe` | defect |\n", encoding="utf-8")
    log = tmp_path / "audit.jsonl"
    log.write_text(json.dumps(event) + "\n", encoding="utf-8")
    return subprocess.run([sys.executable, str(COMPARE), str(log), str(inventory)],
                          capture_output=True, text=True, timeout=60)


def test_compare_lists_an_unexpected_operation(tmp_path):
    done = _compare(tmp_path, {"event": "os.remove", "args": [], "file": "src/x.py", "line": 3, "func": "wipe"})
    assert done.returncode == 0
    assert "os.remove src/x.py::wipe" in done.stdout


def test_compare_is_silent_for_an_expected_operation(tmp_path):
    done = _compare(tmp_path, {"event": "open", "args": [], "file": "src/x.py", "line": 3, "func": "save"})
    assert done.returncode == 0
    assert done.stdout == ""
