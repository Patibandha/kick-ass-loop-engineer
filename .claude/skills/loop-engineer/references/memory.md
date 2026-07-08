# Memory reference — the extended-memory spine

Two layers, joined so long projects resume without context loss:

1. **SQLite store** (`kickass_loop_engineer.memory.Memory`) — queryable cross-agent
   facts. `record(key, value, run_id, kind)`, `get(key)` (latest wins), `query(kind,
   status)`, `forget(key)`, `consolidate(run_id)` (mark a run's facts `canonical`).
   Parameterized queries only. This is what an MCP memory server exposes to every
   isolated agent session.
2. **STATE.md / STORY / cross-run-playbook** — the greppable, human-readable layer.
   `STATE.md` holds the live round-by-round context; the playbook holds durable lessons.

## Governance (non-negotiable)
- **Carry every thread forward or mark it STALE** — never silently drop one.
- **Lessons are untrusted by default.** A lesson is promoted to durable only after **3
  independent successes** (`kickass_loop_engineer.playbook.Playbook`,
  `is_promoted` requires `successes >= 3` and not stale). STALE lessons are kept, not
  deleted, but never promoted. This is the anti-Goodhart safeguard against memory rot.
- The MC **consolidates** intermediate artifacts into one canonical final memory per
  project and garbage-collects the rest (`consolidate(run_id)`).

## Loop-auditor
`kickass_loop_engineer.auditor.classify_run(hit_rate, waste_ratio)` returns
KEEP / PIVOT / RETIRE / KILL — periodically classify the loop's own runs and retire the
ones not earning their budget. The antidote to "iterate forever".
