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

## 3.2.0 — Jev staffing controller: a decision model sizes the team above quality floors (2026-09-30)

### Added
- **Staffing controller** (`decision/` package), armed by a `decision:` config
  section and off when that section is absent. A decision model sizes each run so
  it spends only the tokens the objective needs. The model is TypeSafe Jev
  `jev-1.13.0`, pinned, and falls back to deterministic rules. It decides:
  - risk tier and security depth (L1 gates → L2 AI security review → L3 strict
    blocking review → L4 human sign-off)
  - whether QA and DevOps/ship run, review depth, and whether builders must
    write tests
  - whether the architect stage runs, for `architect: auto` only
  - attempts and starting model tier per slice
  - one decision-gated stall recovery
- **Deterministic quality floors.** The model optimizes only above these.
  - Sensitive domains (auth, payments, secrets, data, infra) raise security,
    review and QA.
  - Auth, payments, secrets, or critical risk force L3 with strict review.
  - `decision.signoff_domains` force L4.
  - `decision.floors` set global minimums, and configured `review.block_on`
    severities are never removed.
- **Two routes to Jev** (`decision.route`):
  - `typesafe` calls TypeSafe directly (`TYPESAFE_API_KEY`).
  - `cloudflare` goes through Cloudflare Workers AI `typesafe/jev`, with zero
    data retention and the same $0.042/M input price (`CLOUDFLARE_ACCOUNT_ID` +
    `CLOUDFLARE_API_TOKEN`).
- **Tier cascade** (`decision.tiers`). Attempts climb a cheap-to-strong builder
  ladder and stop at the first pass (`run_ensemble(stop_on_pass=True)`). Every
  tier is family-checked against the reviewer at config time.
- **Decision journal** `.loop-engineer/decisions.jsonl`. Each record holds the
  probabilities, confidence, backend, latency and cost; the run outcome is
  appended at `_finish`. Decisions are replayed on resume, so a resumed run
  never re-asks a model.
- **Staffing plan artifact** `.loop-engineer/staffing.json`. `next` reads it:
  - it skips QA, security or ship when the plan says so, reported as
    `staffing.skipped_by_plan` on the terminal envelope;
  - it asks for sign-off before ship when the plan is L4
    (`status: "signoff_required"`, new `signoff_v1` artifact
    `.loop-engineer/signoff.md` holding `APPROVED: <name>`).
- **Safety of the hosted call.**
  - The state sent is allowlisted and typed; code, logs, file contents and the
    environment are never sent.
  - Anything secret-shaped fails closed to rules.
  - There is a hard byte cap, strict answer validation, and a `min_confidence`
    gate.
  - A circuit breaker stops calls after 3 consecutive failures.
  - Jev spend is metered through the existing cost ledger (`decide:<site>`).

### Security
- `TYPESAFE_API_KEY` and `CLOUDFLARE_API_TOKEN` are scrubbed from verification
  subprocesses (`guardrails.SCRUBBED_ENV_KEYS`) and from the `claude_code` and
  `gemini_cli` builder CLIs.

### Also in this release (previously unreleased on the 3.1 branch)
- A review finding at a `review.block_on` severity blocks a slice from
  promoting, and every review is filed.
- A gate may declare what its output must prove (`expect` / `min_count`).
- `claude_code` and `gemini_cli` send the prompt on stdin instead of argv, and a
  Gemini SUCCESS with no answer counts as a failure.

## 3.1.3 — proof-of-test reverts only non-test files (2026-09-07)

### Fixed
- **Proof-of-test no longer rejects additive slices.** `prove()` stashed the WHOLE
  working-tree change, so a slice that adds new tests *and* the source they exercise
  to a repo whose existing suite is already green lost its new tests to the stash:
  the old suite stayed green, `red_when_reverted` was `false`, and a genuinely
  passing attempt was thrown away (observed three times on one project in a day).
  The revert is now scoped to the changed **non-test** files — new tests stay in the
  tree and fail exactly as they must without their source. A path is a test path when
  any directory component is `tests`/`test`, or its basename starts with `test_`, ends
  with `_test.py`, or is `conftest.py`. Restore is unchanged (auto-pop, and a failed
  pop still reports `aborted` with the change preserved in the stash), and the
  side-effect discard before the pop is scoped to the stashed paths so a modified test
  file is never clobbered. A staged deletion or rename source (which git cannot name in
  a pathspec stash without leaving a half-saved entry) falls back to the whole-tree
  revert.
- **A tests-only change reports "not applicable" instead of failing the gate.** There
  is no non-test file to revert, so nothing could go red: `ProofRecord.skipped_reason`
  says so, `run_gate` keeps the gate's own pass/fail result, and the journal still
  reports `proven: false` — proof is never claimed for a cycle that did not run.

---

## 3.1.2 — builder commits are un-committed before harvest (2026-09-06)

### Fixed
- **A builder that commits its own work is no longer invisible.** An agentic
  in-place builder (`claude_code`) that finished a turn with `git commit` had
  every trace of its work erased from the engine's point of view: `Workspace.harvest`
  diffs `git status --porcelain` fingerprints, so a committed change reads as
  "no files changed" (firing the pointless no-edit retry), and proof-of-test's
  `git stash` cannot make a gate go red for code that already sits in `HEAD`.
  Two real attempts of paid agentic work were rejected this way. The round now
  records `HEAD` alongside the fingerprint and rewinds to it after EVERY builder
  turn — the first and each corrective retry — before harvesting:

  - `Workspace.head()` returns the current `HEAD` sha (git failures raise
    `WorkspaceError`, never an empty string).
  - `Workspace.uncommit_to(base_sha)` soft-resets to `base_sha` and unstages
    (`git reset --soft <base>` then `git reset -q`), so the builder's bytes come
    back as ordinary modifications and untracked files. It returns how many
    commits it undid, logs a WARNING naming that count, and returns `0` when
    `HEAD` never moved. A `HEAD` that moved to a NON-descendant of `base_sha`
    (a rebase, a branch switch) raises `WorkspaceError` — the engine never
    rewinds history on a guess.
  - The round emits an `uncommitted <N> builder commit(s)` progress event when
    anything was undone.

- **`AGENTIC_BUILDER_SYSTEM`** now tells the builder plainly: never run
  `git commit`, `git stash`, `git reset` or `git checkout` — leave every change
  uncommitted in the working tree, because the engine harvests, verifies and
  promotes it. The prompt is the polite ask; `uncommit_to` is the enforcement,
  the same division of labour as the write guardrails.

---

## 3.1.1 — escalation.allow_tokens (2026-09-05)

### Added
- **`escalation.allow_tokens`** — a per-run exemption for the escalation
  denylist's keyword layer. `should_escalate(action=...)` ended a run in
  `APPROVAL_REQUIRED` whenever a slice objective contained a risky keyword, and
  real objectives legitimately contain them: writing a systemd unit file reads
  as *deploy*, a "model spend cap" reads as *spend*, so a run died on its own
  wording. Listing the keyword under `escalation.allow_tokens` skips it for that
  run only:

  ```yaml
  escalation:
    allow_tokens: ["deploy"]
  ```

  The knob is deliberately narrow. Tokens are compared case-insensitively after
  `.strip()` and must ALREADY be denylist keywords (`deploy`, `delete`,
  `drop table`, `drop`, `spend`, `rm -rf`, `force push`) — an unknown value, a
  non-list, or an empty entry is a `RuntimeError` at load time, not a silently
  ignored line. Each accepted token logs a WARNING (`escalation: risky token
  'deploy' exempted for this run by config`) so the exemption shows up in the
  run log rather than staying buried in YAML.

  It relaxes the KEYWORD check and nothing else: protected paths, the
  file-count cap and the attempt cap always escalate. Configs without the key
  behave exactly as before.
  (`escalation.should_escalate(allow_tokens=...)`,
  `config.escalation_allow_tokens`, `Orchestrator(escalation_allow_tokens=...)`,
  wired at both escalation call sites.)

## 3.1.0 — agentic in-place builders + transient retry (2026-09-04)

The build stage no longer requires a blind chat model. A provider that drives
its own tools — `claude_code`, `gemini` — can now BE the pipeline builder: it
edits the attempt's git worktree directly and the engine harvests the files
that changed, applies the same write guardrails, and continues with gates,
proof-of-test, review, and promotion unchanged. Real projects converge because
the builder can read the repository it is editing instead of re-emitting whole
files from memory.

The second half of the release is durability: every provider now retries
transient failures (HTTP 429/5xx/529 "overloaded", rate limits, connection
resets, timeouts) with exponential backoff, so a busy API costs a round a few
seconds instead of failing it.

### Added
- **Agentic in-place builders.** `Provider.edits_in_place` declares that a
  backend edits the working directory itself; `Provider.edit(system, user,
  cwd=...)` is the entry point the engine hands a workspace to (the base
  implementation refuses loudly). `claude_code` and `gemini` implement it.
- `agents.AGENTIC_BUILDER_SYSTEM` and `Builder.build_in_place(objective,
  feedback, cwd=...)` — the agentic counterpart of `BUILDER_SYSTEM` /
  `Builder.build`. `UI_BUILDER_RULES` still ride along when a `ui` gate is
  configured; non-UI runs stay byte-identical to the bare agentic prompt.
- `Workspace.fingerprint()` — sha256 per file that `git status --porcelain
  --untracked-files=all` reports as changed (`.loop-engineer/` excluded,
  symlinks skipped, renames recorded at their new path). Raises the new
  `WorkspaceError` on any git failure: an empty fingerprint must never be
  mistaken for "nothing changed".
- `Workspace.harvest(before)` — adopts every file whose digest is new or
  changed since `before`, running the SAME `WritePolicy` the FILE-block path
  enforces.
- `BuildSession.build_round` grows the in-place branch: fingerprint → provider
  edit → harvest, with a `"building (in-place)"` progress stage and a
  `"no-edit-retry"` stage when a turn changed nothing.
- Shared transient-failure policy in `providers.base`:
  `is_transient_error(exc)` classifies a flattened message (HTTP
  408/425/429/500/502/503/504/529 on word boundaries, "overloaded", any
  spelling of "rate limit", "too many requests", "timed out"/"timeout",
  "temporarily unavailable", "connection reset/refused", "server error") and
  `retry_transient(call, ...)` re-invokes with exponential backoff.
- Every provider takes `retry_attempts` (3) and `retry_base_delay` (2.0);
  `claude_code`/`gemini` also take `edit_timeout_seconds` (1800.0) for the
  longer agentic turn, and `claude_code` takes `edit_tools` and
  `edit_permission_mode`.
- The starter config template offers `provider: claude_code` as a builder
  ("agentic: edits the worktree in place (Claude Max subscription)") and lists
  `gemini` among the providers.

### Changed
- **Contract change:** `ClaudeCodeProvider`'s default `allowed_tools` for the
  PROSE path (`complete`) is now the read-only set `("Read", "Glob", "Grep")`,
  was `("Read", "Edit", "Bash")`. The roles that call `complete` — decompose,
  architect, review — must never edit the code they judge; editing happens on
  the `edit` path, whose tools are configured separately (`edit_tools`,
  default `("Read", "Edit", "Write", "Glob", "Grep", "Bash")`). Pass
  `allowed_tools` explicitly to restore the old behavior.
- **Harvest semantics:** over `max_files_per_round`, `harvest` keeps EVERY
  surviving file and records an `("(extra files)", "exceeded max files per
  round: N > cap")` rejection, rather than truncating the way `apply` does.
  Blast radius is the orchestrator's escalation decision; reverting an
  arbitrary subset of one coherent agent change would leave the tree
  consistent for nobody. Guardrail violations are still reverted individually
  — `git checkout --` for tracked files, deletion for untracked ones.
- `claude_code`'s non-zero-exit error now falls back to stdout when stderr is
  silent, so the CLI's own overload text reaches the transient classifier.
- `anthropic` and `openai_compat` map a bare socket read timeout to
  `ProviderError` (as `ollama` already did); uncaught it escaped the retry and
  the ledger both.
- The docs stop calling `claude_code`/`gemini` "not a pipeline builder"
  (README providers table, module and class docstrings, config template).

### Tests
- `tests/test_providers_retry.py` (new): the transient truth table — "HTTP
  529" and "overloaded_error" are transient, "1529 bytes" and "ANTHROPIC_API_KEY
  is not set" are not — plus backoff `[2.0, 4.0]`, attempt exhaustion, and the
  permanent-failure fast path, all with an injected sleep.
- `tests/test_claude_code.py`: the read-only prose default, `edit` argv/cwd/
  timeout, a missing `cwd` rejected before any subprocess, a 529 exit retried
  then succeeding, a permanent exit not retried.
- `tests/test_gemini_cli.py`: `edit` argv/cwd/timeout and retry behaviour.
- `tests/test_workspace.py`: fingerprint over untracked/modified files,
  `.loop-engineer/` exclusion, `WorkspaceError` outside a git repo; harvest
  ignoring pre-seeded baseline files, reverting protected and oversized files,
  and keeping everything over the count cap.
- `tests/test_session.py`: an in-place fake provider yielding a `BuildRound`
  whose `files_written` names the file it wrote, exactly one no-edit retry
  with `on_retry` fired once, and usage summed across the retry.
- `tests/test_pipeline_e2e.py`: the 3.1 exit proof — an agentic builder that
  writes into the attempt worktree (and tries to write `.env`) converges
  through decompose → build → gates with proof-of-test → review → promote,
  with the protected path reverted and never promoted.
- 875 tests pass (786 before).

---

## 3.0.0 — any model, verifiable everywhere (2026-07-15)

**3.0.0 GA summary.** The 3.0 arc turns the verified 2.0 loop into a general,
brownfield-ready engine that runs on *any* model and proves its work everywhere.
**M1** made the verifier a gate LIST and opened the roster to any LLM — a native
`openai_compat` provider (OpenAI, Gemini, Grok, DeepSeek, Mistral, Qwen, OpenRouter,
Groq, Together, and local runtimes), pricing-backed budget caps, and format retries.
**M2** added a design-first architect stage with deterministic Mermaid validation and
executable architecture-conformance gates. **M3** gave text-only builders working
"eyes" for browser work — `ui` gates, a failure observer, an `init --ui` scaffold, and
a strictly-advisory VLM screenshot critique. **M4** made the pipeline brownfield-aware
with `build`/`enhance`/`fix`/`audit` task modes (repo-context briefs, reproduce-first
fixes, read-only audits). **M5**, this release, makes ensembles DIVERSE by construction
(per-attempt temperature/model, gates still pick the winner), turns `events.jsonl` into
a run journal that reconstructs any run post-hoc, and ships the engine over `uvx` with a
real proof-of-test demo. The cross-model independence guard holds throughout — builder
and reviewer never share a family, checked at config time and at review time.

### M5 — ensemble diversity, run journal, release

#### Added
- **Diverse ensembles** (`ensemble.py`, `config.py`). Ensemble attempts now vary by
  construction: an auto temperature ladder (`0.2, 0.7, 1.0`, then `+0.3` steps capped
  at `1.5`) over `ensemble.n`, or an explicit `ensemble.attempts` list of
  `{model, temperature?, provider?, family?}` (n derived from the list; `ensemble.n`
  ignored with a warning). `AttemptSpec`/`default_specs` are deterministic — the same
  `n` always yields the same specs. **Winner selection is unchanged: gates decide**;
  diversity only changes how each attempt's builder is built. `claude_code`/`anthropic`
  attempts strip temperature with a once-per-model warning and still run; every attempt
  is wired to the ledger.
- **Two-layer cross-model guard for diverse attempts.** (1) Config-time fail-fast:
  `build_orchestrator` constructs every attempt spec's provider and rejects any attempt
  sharing the reviewer's family *before any spend*. (2) Review-time invariant: the
  WINNING attempt's constructed provider object (never a roster label) is re-checked
  via a new `CrossModelReviewer` per-call API; a violation ends the run `BLOCKED` with
  attempt worktrees cleaned up. Per-attempt `family:` declares independence for
  third-party `openai_compat` attempts; two-unknown rejection stays.
- **Run journal** (`cursor.py` event-only `emit`, orchestrator emissions). Additive
  events on `events.jsonl` reconstruct a full run post-hoc: `attempt_started` (slice,
  attempt index, spec model+temperature), `gate_result` (per gate per attempt: name,
  ok, proven), `observer_ran` (gate, truncation flag), `format_retry` (stage), and
  `cost_recorded` (call site, model, dollars). Cost events reconcile with the ledger
  (the ledger stays the source of truth). Journal writes go only to `events.jsonl`,
  never thrashing `pipeline.json`'s live stage; the journal is append-only and never
  deduplicated, so consumers tolerate a resumed run's re-announced `mode_resolved`.
- **Builder/reviewer format-retry hooks** (`session.py`, `agents.py`). An optional
  `on_retry` callback fires exactly once per corrective format retry — builder-side in
  `BuildSession.build_round`, reviewer-side in the reviewer — with no behavior change
  when absent.
- **`uvx` distribution + a real proof-of-test demo GIF.** The CLI runs under
  `uvx --from <path|git+URL> loop-engineer` (pure stdlib + `pyyaml`, no install).
  `scripts/make_demo_gif.py` records a REAL `loop-engineer verify --prove` run against
  a throwaway repo and renders the revert→red→restore→green cycle as a terminal-style
  GIF (`assets/proof-of-test-demo.gif`, <800KB) — grounded in the run's actual proof
  record, re-runnable, embedded in the README.
- **Import-purity test** (`tests/test_purity.py`) asserting the core imports stdlib +
  lazy `pyyaml` only, and a **version-agreement test** (`tests/test_version.py`) pinning
  `pyproject.toml` and `__version__` together.

#### Changed
- **Version 3.0.0** in `pyproject.toml` and `__init__.__version__`; project URLs point
  at the public `github.com/Patibandha/kick-ass-loop-engineer` repo.

#### Fixed
- **M4 riders.** Audit-mode success no longer records `memory["verifier.proven"]`
  (build/enhance/fix still do); `run_audit` takes a per-chunk `budget_check` that stops
  a sweep cleanly with partial findings + a truncation note (an escalate-level
  `audit_budget_stop` event; outcome stays SUCCESS); fix-mode briefs now compose repo
  context (map + selected files) BEFORE `FIX_MODE_RULES`, same as enhance.

### M4 — brownfield task modes

#### Added
- **Task modes** (`modes.py`): a run is `build` / `enhance` / `fix` / `audit`,
  selected by config `mode:` (top-level, default `auto`) or CLI `run --mode`.
  `auto` detects with one deliberately coarse signal — git-tracked files in the
  workspace (`.loop-engineer/` excluded) → `enhance`, otherwise `build`; `fix`
  and `audit` are NEVER auto-detected. The resolved mode is recorded on the
  pipeline cursor (a resumed run keeps its recorded mode, with a warning when
  this invocation resolved differently) and announced as a `mode_resolved` event.
- **Repo orientation primitives** (`repomap.py`, stdlib only): `repo_map` (a
  compact skeleton of the tracked files — `class`/`def` signatures, ranked by
  import in-degree, 8,000-char budget), `select_files` (keyword-scored FULL
  contents of the objective-relevant files, 24 KB budget, overflow files skipped
  never truncated), and `assemble_context`, which closes with an update
  instruction: these files EXIST — re-emit the complete updated file, don't drop
  existing behavior.
- **Enhance mode.** The repo context is assembled ONCE per run against the main
  workspace and rides every slice's builder brief as a new `CONTEXT:` section
  (`Objective.context`; an empty context keeps briefs byte-identical to 3.0-M3);
  the raw repo map is also handed to the architect prompt. Resume re-assembles
  against the current workspace by design (completed slices are already
  promoted).
- **Reproduce-first fix mode** (`reproduce.py`). The builder must emit ONE
  minimal failing test at `tests/test_repro_<slug>.py`, verified RED with pytest
  configuration disabled (`--noconftest -o addopts=`); a green repro gets one
  corrective regeneration, then the run ends `BLOCKED` "cannot reproduce". The
  ENGINE commits the repro before any fix attempt — proof-of-test stashes
  uncommitted files, so an uncommitted repro would revert WITH the fix and "red"
  would mean file-missing, not bug-back — and appends a `prove: true` proving
  gate on the config-isolated repro command. Tamper hardening in code, not
  prompts: `FIX_MODE_RULES` on every fix brief, pre-gate repro restore in every
  attempt worktree, post-promote restore that fails CLOSED, and a
  `tamper_detected` event on any detected edit. Slug collisions get a numeric
  suffix; resume skips regeneration and re-arms the gate from the cursor's
  durable `repro_path`.
- **Read-only audit mode** (`audit.py`). A chunked reviewer sweep over the
  selected files (4× the enhance selection budget; empty selection falls back to
  ALL tracked files — an audit always sweeps), one ledgered reviewer call per
  24 KB chunk, every finding carrying a file reference. Only `security`/
  `data_leak`-category gates run, as neutralized copies (`prove: false`, no
  observer); findings land in `.loop-engineer/findings.md` under an ADVISORY
  header — only gate results make pass/fail claims. The run ends `SUCCESS` with
  `"audit complete: N findings"`; nothing outside `.loop-engineer/` is written —
  no worktrees, no promote.

#### Changed
- **Starter config** documents the top-level `mode:` key (default `auto`, all
  five choices) and `loop-engineer.example.yaml` was regenerated from it.
- **`parse_findings` promoted to a module-level function** (`agents.py`) — the
  one shared `FINDING:` parser behind the reviewer, the visual critique, and the
  audit sweep. `Reviewer._parse_findings` remains as a delegating staticmethod,
  so no caller breaks; parsing behavior is unchanged.

#### Fixed
- **A stall no longer erases prior feedback.** Observer output captured when a
  slice stalls now APPENDS to the feedback the next attempt/resumed run starts
  from (bounded to the last 3 entries, the oscillation-history precedent)
  instead of replacing what the previous review taught.

---

### M3 — UI gates + observer + visual critique

#### Added
- **Failing-gate observer — eyes for the retry loop.** A failing gate with an
  `observe:` command runs it under the SAME authority as gates (allowlist +
  metachar rejection + scrubbed env) with observer caps (30s timeout, 8KB output).
  The captured output (e.g. a Playwright a11y-tree snapshot of the live page) lands
  on the failing evidence record (`observed`) and verifiably reaches three places:
  later attempts of the same ensemble slice (an `OBSERVED (gate: <name>):` block in
  the next attempt's builder brief), next-slice feedback, and STALLED evidence. A
  rejected/failed/timed-out observer logs a warning and contributes nothing — it
  never crashes the run and never changes a gate verdict.
- **Vision transport on `openai_compat`.** `complete()` gains an optional
  `images=` parameter (file paths → base64 `image_url` content parts in the OpenAI
  content-array shape; mime from extension, png default). Purely additive:
  text-only calls send a byte-identical body; an unreadable image path raises
  `ProviderError`.
- **Advisory VLM screenshot critique** (`visual.py` + `visual:` config). After a
  slice's gates pass, the allowlist-validated `screenshot_cmd` runs (its LAST token
  names the output image) and a vision-capable provider critiques the screenshot
  against the slice objective; `FINDING:` lines join the reviewer's advisory stream
  (oscillation detection + next-slice feedback) and the call is ledgered. The
  config takes a NESTED `visual.provider` section in the standard provider form
  (deliberate deviation from the spec's flat sketch — reuses standard provider
  construction). Advisory-only **by design** (~50% pairwise VLM accuracy on similar
  UIs): never a gate, never a verdict, and any failure in the chain degrades to a
  warning + zero findings. Absent `visual:` section → feature entirely off.
- **`init --ui` Playwright scaffold.** Writes `playwright.config.ts` +
  `tests-ui/smoke.spec.ts` (page loads, a documented `KEY_ELEMENT` placeholder
  selector is visible, no console errors, axe-core a11y scan) and a config whose
  gates list carries unit + ui (`npx playwright test`, snapshot observer at
  `http://localhost:3000`). Existing files are skipped with a warning, never
  overwritten; no npm/npx command is ever run; plain `init` output is unchanged.
- **Conditional UI builder rules.** `UI_BUILDER_RULES` (semantic HTML +
  accessibility, stable selectors, no console errors, reachable dev-server URL) is
  appended to the builder SYSTEM prompt only when a `ui`-category gate is
  configured; non-UI configs keep the prompt byte-identical.

#### Changed
- **Starter config** documents the `visual:` section (nested provider form) and the
  ui gate's `observe:` comment now describes the live observer instead of pointing
  at a future milestone.

---

### M2 — architect stage + diagrams + conformance

#### Added
- **Smart-triggered architect stage.** Decompose runs first; a 2+-slice objective
  triggers one architect call that emits a validated `design.md` (Components /
  Architecture / Primary flow / Decisions + a machine-readable `layering:` yaml
  block), then decompose re-runs against the design. Config `architect: auto|on|off`
  (default `auto`); CLI `run --architect`/`--no-architect` override. The call is
  ledgered and budget-capped like every other provider call; resume never re-runs a
  completed architect stage (and re-arms it with a warning if `design.md` went
  missing). An unwritable `design.md` degrades with a warning — the in-memory design
  still drives the re-decompose.
- **Deterministic diagram validation.** Mermaid fences validate via optional
  `npx --yes @probelabs/maid` (already allowlisted); validator errors drive a bounded
  architect retry (2). No maid → stdlib sanity pre-check + SKIPPED with a visible
  warning — never a silent pass, never a hard run failure.
- **Architecture-conformance gate.** Declared `layering:` renders to `.importlinter`
  and appends an implicit `architecture` gate (`lint-imports`, `prove: false`) when
  the design declares layering AND the tool is on PATH AND the workspace is a Python
  package; any missing precondition logs a SKIPPED warning. The engine-rendered
  `.importlinter` is propagated into every attempt worktree.
- **archmap auto-diagrams.** Each successful promote regenerates
  `docs/architecture.mermaid.md` — a stdlib-`ast` module-dependency graph as Mermaid
  `graph TD` (renders on GitHub). Best-effort: write failures warn, never fail the run.

#### Fixed
- **Spend honesty on corrective-retry failures.** When a builder/reviewer format
  retry dies with a `ProviderError`, the completed first call's tokens/cost now ride
  the exception (`partial_result`) and the orchestrator ledgers them — paid calls are
  never lost to cost accounting.
- **claude_code cwd misattribution.** A `FileNotFoundError` caused by a missing
  working directory is now reported as `claude_code cwd does not exist: ...` instead
  of being blamed on the claude binary.

---

### M1 — gate-list engine + any-LLM providers

#### Added
- **Gate lists.** `gates:` in config is now a LIST of `{name, cmd, prove?, observe?}`
  entries the pipeline runs in order — fail-fast, with per-gate proof-of-test — per
  attempt and at finalization. Back-compat preserved: the 1.0 single-mapping form
  becomes a one-element list; an absent/empty section means the default unit gate.
- **`ui` and `architecture` gate kinds**: browser DOM-assertion tests (Playwright) and
  declared-architecture conformance (import-linter). A gate's `observe:` command is
  parsed and stored in M1; the failing-gate observer that executes it lands in M3.
- **Curated verification-allowlist additions + a warned escape hatch**: `npx playwright`,
  `npx playwright-cli`, `npx --yes @probelabs/maid`, and `lint-imports` join the bundled
  allowlist (curated entries — never a blanket `npx`). `guardrails.extra_verify_prefixes`
  extends it from config; every use logs a WARNING naming the extras (they run with gate
  authority), and a scalar value is rejected outright instead of being iterated
  per-character into a silently widened allowlist.
- **`openai_compat` provider** — any endpoint speaking OpenAI `/chat/completions`:
  OpenAI, Gemini (its OpenAI layer), Grok, DeepSeek, Mistral, Qwen, OpenRouter, Groq,
  Together, plus local runtimes (Ollama, llama.cpp server, LM Studio, vLLM). Stdlib-only;
  the API key is read from an env var NAME (`api_key_env`) so secrets never live in
  config, and key-less local runtimes work without an Authorization header.
- **Prompt/completion token split** reported by every provider (null-safe usage
  parsing), summed across format retries, so cost accounting works from real usage
  instead of a single opaque total.
- **Pricing-backed budget caps**: a bundled longest-prefix per-Mtok price table
  (`pricing.yaml`), overlaid by an optional `ledger.pricing_path` file and an inline
  `pricing:` map, prices every call so `run_cap_usd` binds even when the provider
  reports no cost. Residual tokens the provider didn't attribute to either side are
  charged at the OUTPUT rate — deliberately conservative, caps trip early rather than
  undercount. A model with no pricing row counts as $0 and logs a warning.
- **Format retries for builder and reviewer** (`format_retries`, default 1): a
  non-empty reply that matches neither the FILE-block format (builder) nor
  `FINDING:`/`NO FINDINGS` (reviewer) triggers up to N corrective re-calls restating
  the exact format; retry usage is summed into the round's accounting. Empty replies
  signal provider failure and are never retried.
- **Config-declared model families**: a `family:` key on the `builder`/`reviewer`
  sections satisfies the cross-model-review independence check for third-party models
  the family detector can't classify. Declared families are UNVERIFIED user
  assertions; same-family rejection semantics are unchanged.
- **Builder context-window warning**: the builder's window is resolved from the
  pricing table at wiring time and a WARNING naming the model, the estimated prompt
  tokens (chars//4), and the window is logged when a round's prompt exceeds 75% of it.

#### Changed
- **`claude_code` reclassified as a frontier/dispatcher provider** — not a pipeline
  builder: it edits files through its own tools and returns prose, not the FILE blocks
  the build pipeline parses. It also gained a `cwd` argument pinning the subprocess to
  a target workspace instead of inheriting the caller's directory.
- **Starter config** (`loop-engineer init` / `loop-engineer.example.yaml`) documents
  the 3.0 surface: an `openai_compat` builder alternative (with per-vendor `base_url`
  examples), a gate-LIST example, `format_retries`, `ledger.pricing_path`, and
  `guardrails.extra_verify_prefixes`.

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
- Master-Controller-as-session model: `/loopforge` skill drives review/iterate/report.
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
