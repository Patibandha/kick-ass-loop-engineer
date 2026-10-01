# kickAssLoopEngineer

Model-agnostic, multi-agent build/review loop. A local builder model (Ollama/Kimi)
writes code inside the engine pipeline; the Claude Code session is the dispatcher —
it loops on `loop-engineer next` and executes the envelopes it emits.

## Invoking
`/loop-engineer <objective>` runs the loop via `.claude/skills/loop-engineer/SKILL.md`.

## Key commands
- `loop-engineer refine --build "<idea>" --done-when "<criteria>" --workspace <dir>` — assess the idea and write SPEC.md/GOAL.md (exit 3 + gap JSON when not ready).
- `loop-engineer next --workspace <dir> [--available <csv>] [--objective "<goal>"]` — emit ONE dispatcher envelope (invoke_skill | invoke_agent | run_engine | ask_user | terminal); always exits 0, state lives in the envelope.
- `loop-engineer run` — full verified pipeline (decompose → ensemble in worktrees → gates+proof → cross-model review → promote), JSON RunOutcome out.
- `loop-engineer verify --gate <unit|security|data_leak|performance|smoke> --cmd "<allowlisted>" [--prove] --workspace <git-dir>` — safe, gate-based verification that emits a JSON evidence record.
- `loop-engineer build` — one guarded builder round (low-level manual tool; the pipeline runs this internally).

## Conventions
- Generated work lives under the chosen workspace; run artifacts under `.loop-engineer/` (pipeline.json cursor, next.json retry ledger, stage artifacts like plan.md/review.md).
- Write full files, docstrings, exception handling; no noise comments.
- Never relax the security guardrails (see the skill's references/security.md).
