# Autonomy reference — the L1→L3 ladder, gating, and escalation

Full autonomy is the goal, reached safely. `kickass_loop_engineer.autonomy` and
`.escalation` encode the rules; the orchestrating session enforces them.

## The ladder
| Level | Value | Behavior |
|-------|-------|----------|
| L1 | `report_only` | The loop proposes + writes a report; never auto-applies to real files. |
| L2 | `assisted` | Auto-fixes anything objectively verifiable and re-loops; pauses for ambiguous/risky. |
| L3 | `unattended` | Full autonomy within the gates, budget, and escalation policy. |

## L3-gated default
L3 is the standing default, but it **engages only after the readiness checklist passes**
(`ReadinessChecklist.ready()`):
- `verifier_proven` — a real executable gate exists and proof-of-test reds.
- `budget_set` — a per-run `max_budget_usd` is configured.
- `denylist_active` — the escalation denylist is in force.
- `workspace_isolated` — work happens in an isolated workspace/worktree.

`resolve_level(requested, checklist)`: if `requested` is L3 and the checklist is not
ready, it **auto-drops to L2** — assisted, asks you — rather than charging ahead blind.
`gaps()` names the unmet items so the session can tell you what to fix.

## Escalation denylist (always pauses, every level)
`should_escalate(action=, paths=, files_touched=, attempt=)` returns `(True, reason)` for:
- **Risky/irreversible actions** — deploy, delete, drop table, spend, `rm -rf`, force push.
- **Protected paths** — `.env`, `secrets/**`, `migrations/**`, `*/auth/*`, `*payment*`,
  `*billing*`, `*.pem`, `*.key`.
- **Blast radius** — more than `max_files` (default 10) touched.
- **No-progress** — the `max_attempts`-th attempt (default 3rd) is reached.

On escalation the loop pauses with full context and (via the notify hook) pings you. The
denylist is the only recurring human touchpoint in an L3 run.
