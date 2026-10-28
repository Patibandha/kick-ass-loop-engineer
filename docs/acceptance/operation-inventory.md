# Operation inventory

The filesystem and subprocess operations engine code is expected to perform, derived from the
[authority map](authority-map.md). The diagnostic audit monitor (`tests/audit_monitor.py`) logs what engine
code really does; `scripts/compare_audit.py` lists anything not below. Diagnostic only: it never enforces and
its result is never guardrail evidence.

Run it (the `tests` package name clashes with a site-packages one on some hosts, hence `PYTHONPATH`):

```
PYTHONPATH=tests python -m pytest -q -p audit_monitor --audit-log audit.jsonl -m "guardrail_gate or guardrail_isolation or guardrail_proof or guardrail_proof_transition"
python scripts/compare_audit.py audit.jsonl
```

Matched by event, file and function (line numbers drift). Python reports `os.replace` as `os.rename`.
Not seen by the monitor: `sqlite3` opens (`memory.py::__init__`, line 46) and anything outside its six events.

## Expected operations

| Event | File | Function | Why (authority-map actor, source line) |
|---|---|---|---|
| `os.mkdir` | `src/kickass_loop_engineer/worktree.py` | `__init__` | Engine: worktree base folder, `tempfile.mkdtemp()` line 39 |
| `os.mkdir` | `src/kickass_loop_engineer/worktree.py` | `promote` | Engine: promote creates parent folders, line 220 |
| `open` | `src/kickass_loop_engineer/worktree.py` | `promote` | Engine: copies the winner's files into the workspace, line 221 |
| `os.mkdir` | `src/kickass_loop_engineer/workspace.py` | `__post_init__` | Engine: workspace root exists, line 284 |
| `os.mkdir` | `src/kickass_loop_engineer/workspace.py` | `apply` | Builder FILE blocks: parent folders, line 339 |
| `open` | `src/kickass_loop_engineer/workspace.py` | `apply` | Builder FILE blocks: policy-checked write, line 340 |
| `open` | `src/kickass_loop_engineer/workspace.py` | `write_text_unlinked` | Engine: run state, temp file lines 110 and 112 |
| `os.rename` | `src/kickass_loop_engineer/workspace.py` | `write_text_unlinked` | Engine: `os.replace` of the temp file, line 118 |
| `os.remove` | `src/kickass_loop_engineer/workspace.py` | `write_text_unlinked` | Engine: temp file cleanup after a failed write, line 121 |
| `open` | `src/kickass_loop_engineer/workspace.py` | `append_line_unlinked` | Engine: run journal append, line 137 |
| `subprocess.Popen` | `src/kickass_loop_engineer/workspace.py` | `changed_paths` | Engine: `git status`, line 60 |
| `subprocess.Popen` | `src/kickass_loop_engineer/verifier.py` | `_git` | Engine: git for proof-of-test, line 116 |
| `subprocess.Popen` | `src/kickass_loop_engineer/orchestrator.py` | `_is_git_repo` | Engine: `git rev-parse`, line 77 |
| `os.mkdir` | `src/kickass_loop_engineer/guardrails.py` | `run` | Gate: fresh bytecode cache folder in the temp dir, line 255 |
| `subprocess.Popen` | `src/kickass_loop_engineer/guardrails.py` | `run` | Gate: the one allowlisted program, no shell, line 256 |
| `shutil.rmtree` | `src/kickass_loop_engineer/guardrails.py` | `run` | Gate: removes only the bytecode folder it made, line 274 |
| `os.mkdir` | `src/kickass_loop_engineer/cursor.py` | `_write` | Engine: `.loop-engineer/` folder, line 137 |
| `os.mkdir` | `src/kickass_loop_engineer/cursor.py` | `_event` | Engine: `.loop-engineer/` folder, line 146 |
| `os.mkdir` | `src/kickass_loop_engineer/state.py` | `__init__` | Engine: `.loop-engineer/` folder, line 46 |
| `os.mkdir` | `src/kickass_loop_engineer/state.py` | `record_round` | Engine: `.loop-engineer/` folder, line 118 |
| `os.mkdir` | `src/kickass_loop_engineer/state.py` | `_write_state_md` | Engine: `.loop-engineer/` folder, line 154 |
| `os.mkdir` | `src/kickass_loop_engineer/next.py` | `_save_ledger` | Engine: retry ledger folder, line 626 |
| `os.mkdir` | `src/kickass_loop_engineer/review.py` | `write_slice_review` | Reviewer findings are filed by the engine, line 331 |
| `os.mkdir` | `src/kickass_loop_engineer/progress.py` | `__init__` | Engine: events folder, line 70 |
| `os.mkdir` | `src/kickass_loop_engineer/progress.py` | `_write_event` | Engine: events folder, line 93 |
| `os.mkdir` | `src/kickass_loop_engineer/ledger.py` | `_save` | Engine: cost-cap ledger folder, line 50 |
| `os.mkdir` | `src/kickass_loop_engineer/playbook.py` | `_save` | Engine: playbook folder, line 79 |
| `os.mkdir` | `src/kickass_loop_engineer/memory.py` | `__init__` | Engine: memory database folder, line 45 |

## Unexpected operations

Found by one monitor run over the guardrail suites (329 tests, all passing) that were not in the inventory
derived from the authority map. The `os.mkdir` rows for the ledger, playbook, memory, progress, state,
review, next and cursor folders were added above: they are `.loop-engineer/` or configured run-state folders
that the map covers only as "everything in the workspace". Import-time bytecode writes and the steps inside
one `rmtree` are filtered out by the monitor and not listed.

**Defects found, fixed 2026-10-28** (all three now write through `write_text_unlinked` / `append_line_unlinked`, which refuse a link; `tests/test_run_state_links.py`):

| Event | File | Function | Finding |
|---|---|---|---|
| `open` | `src/kickass_loop_engineer/ledger.py` | `_save` | Line 51: plain `open(path, "w")` follows a link, while other run state uses `write_text_unlinked`, which refuses links. The ledger path is configurable and may sit outside the workspace; the map does not say so. |
| `open` | `src/kickass_loop_engineer/playbook.py` | `_save` | Line 80: the same plain write through a possible link; the path is configurable. |
| `open` | `src/kickass_loop_engineer/progress.py` | `_write_event` | Line 94: appends to `.loop-engineer/events.jsonl` with plain `open`; a gate (no sandbox) could plant a link there. Nothing was changed. |
