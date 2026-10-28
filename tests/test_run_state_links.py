"""The cost ledger, the playbook and the progress log never write through a link planted at their path.

Found by the audit monitor (docs/acceptance/operation-inventory.md): they used a plain ``open``, which follows a
link, while every other run-state writer refuses one. A real file symlink needs privilege on Windows, so the link
is simulated by making ``os.path.islink`` report the planted path as a link; the refusal code is the real one.
"""

import os

from kickass_loop_engineer.ledger import CostLedger
from kickass_loop_engineer.playbook import Playbook
from kickass_loop_engineer.progress import ProgressEvent, ProgressReporter


def _plant_link(monkeypatch, path):
    real = os.path.islink
    monkeypatch.setattr(os.path, "islink", lambda p: os.path.abspath(p) == os.path.abspath(path) or real(p))


def test_the_ledger_never_writes_through_a_link(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    path.write_text("untouched", encoding="utf-8")
    _plant_link(monkeypatch, path)

    CostLedger(path=str(path))._save()

    assert path.read_text(encoding="utf-8") == "untouched"


def test_the_playbook_never_writes_through_a_link(tmp_path, monkeypatch):
    path = tmp_path / "playbook.json"
    path.write_text("untouched", encoding="utf-8")
    _plant_link(monkeypatch, path)

    Playbook(path=str(path))._save()

    assert path.read_text(encoding="utf-8") == "untouched"


def test_the_progress_log_never_appends_through_a_link(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    path.write_text("untouched", encoding="utf-8")
    _plant_link(monkeypatch, path)

    ProgressReporter(1, events_path=str(path))._write_event(ProgressEvent(1, 1, "built", "x", 0.0))

    assert path.read_text(encoding="utf-8") == "untouched"
