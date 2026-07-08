# Changelog

All notable changes to **kickAssLoopEngineer** (engine dist `kick-ass-loop-engineer`,
command `loop-engineer`; formerly `loopforge`). Versions follow
[Semantic Versioning](https://semver.org): `MAJOR.MINOR.PATCH`. The skill version is
mirrored in `.claude/skills/loop-engineer/SKILL.md` frontmatter (`metadata.version`) and
the package `__version__`. Each release is tagged in git, so history, diffs, and rollback
come from version control.

## How versioning works here
- **PATCH** — fixes, prompt tweaks, guardrail tuning; no behavior contract change.
- **MINOR** — new capabilities, backward compatible (a new agent role, a provider).
- **MAJOR** — breaking changes to the CLI, config schema, or skill contract.
- Bump the version in three places together: `pyproject.toml`,
  `src/kickass_loop_engineer/__init__.py`, and the SKILL.md frontmatter. Then
  `git tag vX.Y.Z && git push --tags`.

---

## 2.0.0 — deep research + 2.0 GA

**2.0.0 GA summary.** The 2.0 arc closes here: **M1** replaced the old transcript-graded
loop with a verified pipeline (executable gates, proof-of-test, evidence-required
success). **M2** gave the session a `next` dispatcher protocol so it composes
GSD/Superpowers/gstack (or any installed skill/agent) through validated artifacts
instead of ad-hoc prompting. **M3** put a six-slot think-tank interview and a
measurability lint in front of every objective, so the loop never starts on a vague
target. **M4**, this release, adds the last missing gate: a mandatory deep-research
stage with a provenance vocabulary the engine can check like a test. Together they
turn "a plain-language idea" into shipped, verified work through one honest loop:
**interview → research (gated) → decompose → ensemble-in-worktrees → proof-of-test →
cross-model review → promote.** The test suite grew from 91 (pre-2.0) to ~246; the
transcript-grading anti-pattern this project started from is gone.

### Added
- **Deep-research stage, first in the loop.** `loop-engineer next` now dispatches
  `research` before `plan` — no build proceeds on unverified facts. A no-external-
  research objective still clears the gate cheaply (a `RESEARCH.md` with an empty or
  self-evident `## Decisions` section passes trivially).
- **Provenance tags** `[VERIFIED]` / `[CITED]` / `[ASSUMED]` (`research.py`): every
  claim in `RESEARCH.md` is one of the three; `## Decisions` is the load-bearing
  section (stack/API/version choices — never `[ASSUMED]`), `## Notes` holds
  speculative, non-load-bearing claims.
- **Deterministic tag-coverage gate** (`check_tag_coverage`): pure function — fails on
  any untagged/unknown-tag claim under `## Decisions`/`## Notes`, and on any
  `[ASSUMED]` claim under the load-bearing `## Decisions` section.
- **Citation spot-check** (`select_citations` + `check_citations`): the session
  re-fetches sources via a new `verify_citations` envelope (`next_action.type`), the
  engine only compares normalized claimed-quote ⊂ fetched-text — deterministic, no
  network access from engine code. A fabricated or missing quote is caught, not
  trusted.
- **`RESEARCH_BLOCKED` terminal state** (`TerminalState`): substantive research
  failures (a load-bearing `[ASSUMED]` claim, or a citation mismatch that survives
  the retry budget) end the run honestly here instead of silently proceeding or
  looping forever.
- **`research_v1` / `citations_v1` artifact schemas** (`artifacts.py`): shape-only
  gates (a `## Decisions` heading + a tagged claim line; a `## <url>` fetched-source
  section) — the substantive tag-coverage/citation gate lives in `research.py`, run
  by the `next` state machine.
- **Global research-round ceiling**: a backstop cap on repeated research/citation
  round-trips so a stubborn citation mismatch can't oscillate indefinitely before
  reaching `RESEARCH_BLOCKED`.
- **`research-ingest` CLI + memory lifecycle**: `ingest_research` records a passing
  `RESEARCH.md`'s `## Decisions` claims to workspace memory as `kind="research"`
  (`working`); the engine's existing `memory.consolidate(run_id)` on run `SUCCESS`
  promotes them to `canonical`. `ResearchBrief.from_spec` reuses `canonical` research
  so later runs don't re-research settled facts.
- **`deep-research` router owner** (`router.py`): `STAGES` gains `research` between
  `refine` and `plan`, owned by the `deep-research` skill (fallback: the
  `research-analyst` agent).
- **SKILL.md** + new `references/research.md`: the research sweep, the
  `verify_citations` round, the source hierarchy (Context7/version-pinned docs →
  official docs/changelogs → verified web → training data only as `[ASSUMED]`, never
  for a library/API/version choice), and the honesty rule (don't fabricate a matching
  quote — an honest `RESEARCH_BLOCKED` beats a fake pass).

### Changed (BREAKING)
- **Research now gates every run.** `STAGES` gained `research` as the first,
  mandatory stage — unlike the optional `qa`/`security`/`ship` stages, it is never
  skipped and never escalates to `ask_user`: a toolkit with no `deep-research` skill
  or `research-analyst` agent available, or a research doc that never converges,
  goes straight to `RESEARCH_BLOCKED`. A run cannot reach `plan` without a
  coverage-passing `RESEARCH.md`.

## 2.0.0-alpha.3 — think-tank interview (2.0-M3)
### Added
- **`interview.py`**: fixed, per-slot `QUESTION_TEMPLATES` (multiple-choice biased,
  what/how phrasing — the engine never invents questions, only interpolates the
  current slot value into a fixed template) and `next_questions(spec)`, which lists
  the still-needed questions in `SLOTS` order, prepending a done_when question when
  the measurability lint fails.
- **Measurability lint** (`lint_done_when`): a `done_when` passes iff it contains an
  allowlisted verifier command prefix (from `guardrails.DEFAULT_VERIFY_PREFIXES`,
  word-boundary matched) or a numeric threshold — a comparator + number (`< 200`,
  `>= 99.9`) or a number + unit/percent (`200ms`, `5 s`, `99%`, `10k rows`). Wired
  into `PromptWriter.assess()` so a vague `done_when` cannot pass the readiness gate
  through any entry point. **BREAKING**: vague done_whens ("feels fast", "works
  well", "users are happy") now fail `refine` with exit code 3 — they used to pass
  as long as the string was non-empty.
- **`refine --interview`**: emits `{"ready": false, "questions": [...]}` instead of
  a bare gap list when the spec isn't ready, so a dispatching session can ask the
  fixed questions one at a time instead of guessing what to ask.
- **`refine --defer <slot1,slot2,...>`**: explicit, validated deferral — a deferred
  slot never gaps and renders as `_deferred_` in SPEC.md. An unknown slot name in
  `--defer` exits 1 with `{"error": ...}` before assessment ever runs. There is no
  way to silently skip a slot.
- **Success metrics become verifier targets**: every entry of `IdeaSpec.success_metrics`
  is rendered as a `## Checks` bullet in GOAL.md alongside `done_when` (exact
  duplicates deduped, order preserved).
- **SKILL.md**: the "Refine first" section is now the interview loop — one question
  per turn in emitted order, judge answer quality (accept / re-ask once with the
  hint / defer-and-announce), user approval of the written SPEC.md before the
  dispatcher loop starts. **One-interviewer rule**: the interview runs once, here;
  downstream skills (GSD discuss/plan, Superpowers brainstorming) receive
  SPEC.md/GOAL.md as pre-answered context and must not re-interview the user — new
  `references/interview.md` covers the six-slot taxonomy, deferral semantics, the
  answer-quality ladder, and the lint rule with examples.

### Changed (BREAKING)
- **`IdeaSpec` grows to the six-slot think-tank taxonomy**: `purpose`, `users`,
  `constraints`, `success_metrics` (list), `anti_goals`, `risks`, plus an explicit
  `deferred: list[str]`. `exclude` is renamed `anti_goals`; the CLI keeps `--exclude`
  as a deprecated alias that maps onto `--anti-goals`.
- **`Assessment.gaps` replaces `Assessment.gap`**: `assess()` now returns the FULL
  gap list (never first-miss) — `build`, `done_when` (including a lint failure), and
  every unfilled, undeferred slot. `ready` ⇔ `gaps == []`. The plain `refine` JSON
  error shape changes from `{"gap": str}` to `{"gaps": [...]}`; the readiness gate
  now demands all six slots filled-or-deferred in addition to `build` + a
  lint-passing `done_when`.
- **CLI `refine`** gains slot flags — `--purpose --users --constraints
  --success-metric (repeatable) --anti-goals --risks --defer --interview` — on top
  of the existing `--build --done-when --consider --workspace`. The ready-path JSON
  shape (`{"ready": true, "spec": ..., "goal": ...}`, exit 0) is unchanged.
- **SPEC.md** renders all six slots (`## Purpose`, `## Users`, `## Constraints`,
  `## Success metrics`, `## Anti-goals`, `## Risks`) plus `## Consider` and
  `## Done when (measurable)`; deferred slots render `_deferred_`. The old
  `## Exclude` section is gone (superseded by `## Anti-goals`).

## 2.0.0-alpha.2 — next protocol (2.0-M2)
### Added
- **Envelope state machine** (`next.py`): `emit_next()` derives the session stage from
  artifacts on disk — never from the transcript or fragile stored state — and emits
  exactly ONE JSON envelope per call telling the dispatching session what to run next.
  Retries an invalid stage artifact up to 2 times (`retry: 1`/`retry: 2` +
  `validation_error`) before escalating to an **`ask_user`** envelope; a 4th arrival at
  the same invalid stage never silently skips a mandatory stage. **Engine exemption**:
  a `run_engine` dispatch only burns a retry when a cursor exists and is non-terminal
  (a genuinely stalled/crashed run) — a cursor-absent dispatch (session restart before
  the engine ran) never creeps toward `ask_user`. Optional stages (qa/security/ship)
  are skipped **explicitly** — recorded in the retry ledger and named in the terminal
  envelope's `skipped_stages` — only when neither owner nor fallback is available;
  mandatory stages (plan, review) `ask_user` instead of skipping. The terminal envelope
  also lists `failed_verdicts` for any qa/security artifact whose `VERDICT: FAIL` shape
  validated but signals a real failure, so "complete" is never misread as "all green".
- **Artifact schemas** (`artifacts.py`): `ARTIFACT_SCHEMAS` (`plan_v1` / `review_v1` /
  `qa_v1` / `security_v1` / `ship_v1`) + `validate_artifact()` — deterministic gates
  (exists, non-empty, required marker), same input always the same verdict, no model
  judgment. `research_v1` deliberately absent (2.0-M4).
- **CLI `next` subcommand**: `loop-engineer next --workspace <dir> [--available
  <csv>] [--objective "<goal>"]` prints the envelope as JSON and always exits 0 — state
  lives in the envelope, not the exit code.
- **Scripted fake-session conformance suite** (`tests/test_next_conformance.py`): a
  `FakeSession` plays the dispatcher role end-to-end (happy path, stubborn-invalid-
  artifact → `ask_user`, minimal-toolkit optional-stage skipping) so protocol
  regressions are caught by pytest, not live runs.

### Changed (BREAKING)
- **`router.py`**: `SkillRouter.resolve()` / `.owner()` now return a typed
  `StageRoute(name, kind)` instead of a plain string, so the dispatching session knows
  whether to use the Skill or the Agent tool. Fixes four kind corrections from the 2.0
  audit (`gsd-planner`, `backend-developer`, `gsd-code-reviewer`, `security-auditor` are
  **agents**, not skills) and one name fix (`verify`'s fallback now carries its
  `superpowers:` prefix).
- **`orchestrator.py`**: the terminal cursor detail now persists `outcome:
  outcome.to_dict()` alongside the auditor verdict, so `next` can surface a run's
  evidence without re-deriving it.
- **SKILL.md + `references/routing.md`** rewritten around the dispatcher loop: the
  session's job is to enumerate its installed skills/agents, call `next`, switch on
  `next_action.type` (`invoke_skill` / `invoke_agent` / `run_engine` / `ask_user` /
  `terminal`), write the dispatched result to `expects.artifact` in `expects.schema`'s
  format, and re-call `next` — never fake a marker to satisfy the validator. The old
  per-round build protocol is gone from the main loop (`build`/`verify` remain
  documented as low-level manual tools).

## 2.0.0-alpha.1 — pipeline core (2.0-M1)
### Added
- **`orchestrator.py`** — the full verified pipeline wired end-to-end: decompose →
  ensemble-in-worktrees → gates+proof → cross-model review → promote → governance,
  ending in one of **six terminal states** via `RunOutcome`. The pipeline is
  **resumable**: progress is tracked in a `pipeline.json` cursor.
- **`generate_slices`** — JSON-based objective decomposition with a single-slice
  fallback when decomposition isn't warranted.
- **`WorktreeManager.promote`** — symlink-safe and non-ASCII-safe promotion of a
  winning worktree's files back into the main workspace.
- **`run_gate`** validates gate names against the gate registry before running;
  escalation globs now derive from `guardrails.DEFAULT_PROTECTED` (one source of truth).
- Bounded oscillation history; append-based `rounds.jsonl` with legacy migration from
  the old `rounds.json` format; cross-model check now keys off actually-wired
  provider identities; CLI emits a JSON error on a builder/reviewer family clash.
- **Config**: `EXAMPLE_CONFIG` is the single source of truth (plus a `gates:` section);
  the stale tracked `loopforge.yaml` was removed.
- **E2E toy-repo proof** (the M1 exit criterion), including a proof-of-test
  revert→red→green demonstration.

### Changed (BREAKING)
- The old loop is deleted: `LoopEngine`, `RunStatus`, `build_loop`, and `LoopBundle`
  are removed. `loop-engineer run` now runs the full verified pipeline and prints a
  JSON `RunOutcome` — exit `0` on `SUCCESS`, `2` on any other terminal state, `1` on
  a config/setup error (e.g. builder/reviewer model-family clash).
- `agents.Review` now carries `findings` only — the `approved` field is gone. The
  reviewer never approves; **gates own approval**.

## [1.0.2]
### Fixed
- Verification allowlist now accepts `python3 -m unittest` / `python3 -m pytest` /
  `python3 -m mypy` / `python3 -m pytest_benchmark`. On systems that ship only
  `python3` (no `python`), the `python -m unittest` gate errored with "verification
  tool not found"; the `python3` variants close that portability gap. Arbitrary
  `python3 -c ...` remains blocked.

## [1.0.1]
### Changed
- `loop-engineer init` starter template now surfaces the 1.0 config sections
  (`ensemble`, `models`, `ledger`, `notify`) as commented examples with their default
  values, plus a note that no API keys are used (Ollama via the local daemon; `:cloud`
  served by the daemon's ollama.com login; `ANTHROPIC_API_KEY` never used). The settings
  already applied via defaults; this only makes them discoverable.

## [1.0.0] — first stable release
The full autonomous loop engine, consolidating the alpha line (M1–M5). A plain-language
idea becomes shipped, verified work via an autonomous **loop = goal + verify + feedback +
stop**, governed and cost-capped.

### Highlights
- **Verifier spine** — executable allowlisted gates (unit / security / data-leak / perf /
  smoke) with **proof-of-test** (revert→red→green), judged from real artifacts, never the
  transcript. Terminal-state taxonomy with evidence-required success.
- **Prompt Writer** — turns an idea into a checkable SPEC.md / GOAL.md or "not ready".
- **Decomposition + interface contracts** and **isolated git worktrees** per role.
- **Ensemble (N=2) + verified selection** (gate-passing winner, simplicity tiebreak — never
  merge) and **cross-model review** (Kimi→Qwen→Claude, builder≠reviewer family) with an
  **oscillation stop**.
- **Memory spine** — SQLite store + cross-run-playbook (promote a lesson after 3 successes;
  never silently drop), **loop-auditor** (KEEP/PIVOT/RETIRE/KILL), and a deterministic
  **skill-router** (only `/loop-engineer` auto-invokes).
- **Autonomy** — **L3-gated** default (unattended after a readiness checklist; auto-drop to
  L2 when a gate can't be proven) and an **escalation denylist** for risky/irreversible
  actions. Per-run `$10` hard cap + advisory monthly ledger; subscription-only, no
  `ANTHROPIC_API_KEY`. Minimal **notify hook** for unattended escalations.

### Added since alpha.4
- `autonomy.py` (AutonomyLevel, ReadinessChecklist, resolve_level) and `escalation.py`
  (should_escalate); `references/autonomy.md`. Engine package `kick-ass-loop-engineer`,
  command `loop-engineer`.

## [1.0.0-alpha.4] — M4: memory + governance
### Added
- **SQLite shared memory** (`memory.py`): `Memory` store — record / get-latest /
  query-by-kind-and-status / forget / consolidate (mark a run canonical). Parameterized
  queries; the queryable cross-agent state an MCP memory server exposes.
- **Cross-run-playbook governance** (`playbook.py`): promote a lesson to durable only
  after **3 independent successes**; STALE lessons are kept, never silently dropped.
- **Loop-auditor** (`auditor.py`): `classify_run` → KEEP / PIVOT / RETIRE / KILL by
  hit-rate × waste-ratio — the antidote to "iterate forever".
- **Skill-router** (`router.py`): deterministic stage→owner(+fallback) map; only
  `/loop-engineer` auto-invokes. Resolves the skill-conflict hotspots (one owner per task).
- Skill references `routing.md` + `memory.md`. (Components governed by the session; the
  MCP stdio server and fully-live loop are orchestrator/Phase-2 concerns.)

## [1.0.0-alpha.3] — M3: multi-model + ensemble
### Added
- **Verified selection** (`ensemble.py`): `Attempt`, `select_winner` (gate-passing winner,
  simplicity tiebreak), and `run_ensemble` (N independent attempts returning `Attempt` records).
- **Cross-model reviewer** (`review.py`): `CrossModelReviewer` enforcing builder≠reviewer
  model family (Kimi→Qwen→Claude independent blind spots); `model_family` detector;
  `CrossModelReviewError` on same-family or double-unknown pairs.
- **Oscillation stop** (`oscillation.py`): `OscillationDetector` halts when repeated
  findings appear across consecutive rounds with no new evidence.
- **Ensemble + model-roster config** (`config.py`): `ensemble_n` (N=2 default) and
  `model_roster` (builder/reviewer/arbiter with defaults: kimi-k2.7-code:cloud / qwen2.5 / claude).

> **Note:** these are component implementations; live engine wiring (loop orchestration using
> all M3 components together) is M4+.

---

## [1.0.0-alpha.2] — M2: single-loop brain
### Added
- **Prompt Writer** (`promptwriter.py`): `IdeaSpec` / `PromptWriter` with SPEC/GOAL renderer
  and a not-ready gate (blocks when `build` or `done_when` is empty).
- **Role decomposition + interface contracts** (`decompose.py`): `RoleSlice`, `RoleDAG` with
  topological ordering and contract validation.
- **Isolated git worktrees** (`worktree.py`): `WorktreeManager` for per-agent checkout
  isolation.
- **Cost ledger** (`ledger.py`): `CostLedger` with $10/run hard cap and $200/month advisory
  cap; `build_ledger` config builder.
- **Best-effort notify hook** (`notify.py`): `Notifier` (webhook + shell command, fire-and-forget);
  `build_notifier` config builder; `build_event` helper.
- **`loop-engineer refine` command**: assesses an `IdeaSpec`, writes `SPEC.md` / `GOAL.md` to
  the workspace, exits 0 on success or 3 when the spec is not ready.

> **Note:** these are component implementations; live engine wiring (loop orchestration using
> all M2 components together) is M3.

---

## [1.0.0-alpha.1] — M1: rebrand + verifier core
Pre-release toward `1.0.0` (the full autonomous loop ships at M5). The 1.0 design lives
in `docs/specs/2026-06-25-loop-engineer-1.0-design.md`; the milestone plan in
`docs/plans/2026-06-25-m1-rebrand-verifier-core.md`.
### Changed (BREAKING)
- Rebrand `loopforge` → **kickAssLoopEngineer**: package `loopforge` → `kickass_loop_engineer`,
  dist `kick-ass-loop-engineer`, CLI `loop-engineer` (+ `kickass` alias), skill
  `/loopforge` → `/loop-engineer`, artifacts dir `.loopforge/` → `.loop-engineer/`,
  config `loopforge.yaml` → `loop-engineer.yaml`.
### Added
- **Verifier spine.** `verify` now runs a named gate and emits an **evidence record**
  (`{gate, command, passed, evidence, proof}`), not an opinion.
- **Proof-of-test** (`verify --prove`): a passing gate is proven real via
  revert→red→green over `git stash`; judged from artifacts, never the transcript.
  Safe on `stash pop` failure (change preserved in stash, `aborted` flag, git timeout).
- **Gate registry** (5 categories: unit, security, data_leak, performance, smoke) and an
  extended verify allowlist (bandit, semgrep, gitleaks, detect-secrets, pytest-benchmark, …).
- **Terminal-state taxonomy** (`success · stalled · blocked · budget_exceeded ·
  approval_required · oscillation`); `success` requires evidence.
- Rebranded `/loop-engineer` skill (M1 orchestrator skeleton) + `references/verifier.md`.

---

## [0.2.0] — current
### Added
- Session-as-orchestrator model: the Claude Code session drives review/iterate/report.
- `loopforge build` (one guarded builder round, JSON out, stderr progress bar).
- `loopforge verify` (allowlisted, metacharacter-rejecting verification).
- Security guardrails: write policy (containment, protected paths, size/count caps) and
  verification allowlist with scrubbed environment.
- Transparency: progress events + shared `.loopforge/STATE.md` context across rounds/models.
- Providers: Ollama (Kimi/local), Claude Code (subscription), Anthropic API.
- Fixed policy: verifiable-auto-fix, run-when-runnable, 5-round cap.

## [0.1.0]
### Added
- Initial model-agnostic build/review loop with an internal reviewer (`loopforge run`).

---

## Roadmap (planned versions, mapping the full vision)

### [0.3.0] — Prompt Writer phase
A prompt-refinement step before building: the MC hands the raw idea to a Prompt Writer
agent that produces a vetted, checkable objective + `done-when`, iterating with the human
until approved. Directly fixes loop non-convergence from vague objectives. Lowest risk,
highest leverage — ship this first.

### [0.4.0] — N parallel developers (decomposition)
MC asks the human how many developers and **decomposes** the objective into independent
sub-tasks, runs `loopforge build` per sub-task in parallel (local models), and integrates.
Parallelism is over *different* sub-tasks, not competing attempts at the same one.

### [0.5.0] — Dedicated Reviewer + MC report
A Reviewer agent integrates/dedupes developer output into one solution; the MC does final
acceptance, verification, and a detailed human-facing report. Clear division: Reviewer
synthesizes, MC accepts and reports.

### [0.6.0] — Shared memory via MCP + isolated agent sessions
Expose the per-project SQLite memory + workspace through one MCP server so every isolated
agent session reads/writes shared state. Use the skill `context: fork` + `model` frontmatter
to run agents as isolated subagents. MC consolidates intermediate artifacts into one final
memory and garbage-collects the rest.

### [1.0.0] — Hardened & public
Format-repair fallback for builder output, CONTRIBUTING.md, CI, audited guardrails,
documented stable CLI/config/skill contract.
