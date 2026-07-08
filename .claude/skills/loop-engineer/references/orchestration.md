# Loop Engineer orchestration reference — dispatcher-loop edge cases and recovery

Detailed reference for the session that drives `/loop-engineer`. The skill body has the
protocol; this covers the edge cases.

## Retry envelopes

An envelope carrying `retry: N` + `validation_error: <reason>` means the last artifact
failed deterministic validation. Fix the **artifact** per the exact error — re-run or
re-dispatch the stage so the file truthfully reflects its real outcome — and never fake
a marker to satisfy the validator. A failing validator means the work isn't in shape,
not that the file needs a magic string. Two retries per stage; the third failure
arrives as `ask_user`.

## ask_user envelopes

Ask the user **exactly one question** — the string at `next_action.question` (mirrored
at the top-level `question` key). Apply the answer, usually as feedback/context for the
re-dispatched skill or agent, or as a correction to the artifact in question. Then
resume the loop: call `next` again. Never batch questions or continue past an
unanswered one.

## Crash / restart recovery

All state lives on disk: `pipeline.json` (the engine cursor), `next.json` (the retry
ledger + explicit skips), and the stage artifacts under `.loop-engineer/`. Nothing
lives in the transcript. After any crash or context reset, just call `next` again —
it re-derives the stage from the artifacts and resumes exactly where the run left
off. An engine dispatch with **no cursor** never burns a retry: a session restart
before the engine ran cannot creep toward `ask_user`.

## Engine non-success terminals

When the engine run ends in `stalled`, `blocked`, `budget_exceeded`,
`approval_required`, or `oscillation`, `next` emits an `ask_user` envelope rather
than continuing — any non-success terminal is the user's call in M2. Relay the
cursor's `outcome.reason` and its `evidence` to the user **verbatim**; do not
paraphrase away the failure detail before asking how to proceed.

## Session restart mid-engine-run

If the session dies while `loop-engineer run` is in flight, re-running the same
command with the same `--run-id` resumes the pipeline: completed slices are read
from the cursor and skipped, not rebuilt. `next` hands you the `run_engine` action
again; pass the `run_id` from the envelope when one exists.

## Context continuity

STATE.md is the shared memory. Read it before reporting and rely on it (not your chat
scrollback) as the source of truth for what has been tried. This is what keeps context
intact when the builder model and the dispatching session differ.

## Stopping

Stop and report on: a `terminal` envelope, an `ask_user` envelope awaiting the user,
or an unrecoverable provider/setup error. Always leave STATE.md, the artifacts, and
the final report behind so the run is fully auditable — surfacing `skipped_stages`
and `failed_verdicts` explicitly.
