# Interview reference — the six-slot think-tank

`refine --interview` pulls real requirements out of the user before any build. The engine
owns a **fixed** set of questions (`interview.QUESTION_TEMPLATES`); it never invents free
text — at most it interpolates the CURRENT slot value into a template (e.g. quoting an
unmeasurable `done_when`). You conduct one question per turn and re-run `refine` with the
accumulated flags. The CLI is stateless: pass every flag and every `--defer` each run.

## The six slots

| Slot | What it captures | The question's intent |
|------|------------------|-----------------------|
| `purpose` | The job this does and the outcome it produces | Name the job, not the implementation — one sentence. |
| `users` | Who (or what) uses it | Separate "just me" from a team / external users / other machines. |
| `constraints` | Hard constraints the build MUST respect | Offline, stack, cost, deadline, integration — not preferences. |
| `success_metrics` | 1-3 measurable metrics | Each becomes a verifier target in `GOAL.md`'s `## Checks`. |
| `anti_goals` | Explicit non-goals (absorbs 1.0's `exclude`) | Block scope creep before it starts. |
| `risks` | Known unknowns / what could go wrong | Surface what a spike or research pass should hit first. |

`build` and `done_when` are the two always-required fields; the six slots above complete
the gate. The interview is over when `refine` returns `{"ready": true}` — i.e. every slot
is filled or explicitly deferred and `done_when` passes the lint.

## Deferral semantics
Deferral is **explicit and named** — there is no silent skip. When the user declines a
slot, pass `--defer <slot>` (comma-separated for several). A deferred slot never gaps, is
validated against the six slot names, and renders as `_deferred_` in `SPEC.md`. Announce
every deferral out loud so the user knows what the spec will NOT capture.

## Answer-quality ladder
Judge each answer before you accept it — the `hint` tells you what "good" looks like.

1. **Accept** — a concrete, on-topic answer. Fold it into the slot's flag.
2. **Re-ask once (with the hint)** — a non-answer ("whatever", "you decide", "idk") gets
   exactly ONE re-ask that incorporates the hint.
3. **Defer and announce** — if the user declines the re-ask, `--defer <slot>` and say so.

An answer that belongs in a different slot goes into that slot's flag, not the one asked.

## The measurability lint (`done_when`)
`done_when` must be checkable. It passes iff it contains an allowlisted runnable command
prefix (from `guardrails.DEFAULT_VERIFY_PREFIXES`) **or** a numeric threshold (a comparator
+ number, or a number + unit/percent).

- **Passing:** `python3 -m pytest -q passes`, `npm test exits 0`, `p95 < 200ms`,
  `handles 10000 rows in < 5 s`, `error rate 1% or less`.
- **Failing:** `feels fast`, `works well`, `users are happy`, `version 2 works` (bare
  number, no comparator or unit), `` (empty).

A failing lint makes the interview emit a `done_when` question FIRST, quoting the current
unmeasurable text. The loop cannot converge on a wish.

## Why one interviewer
The interview runs exactly once, at refine. Downstream skills (GSD discuss/plan,
Superpowers brainstorming) get `SPEC.md`/`GOAL.md` as pre-answered context and are told to
ask only what the spec does not answer. Double interviews burn user patience and produce
two conflicting specs — so if a dispatched skill re-interviews, you answer it FROM
`SPEC.md` rather than relaying the duplicate question back to the user.
