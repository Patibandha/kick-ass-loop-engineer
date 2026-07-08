# kickAssLoopEngineer

A model-agnostic, multi-agent **build / review loop** that runs as a **Claude Code
skill**. You type `/loop-engineer <objective>` in a Claude Code session; a local model
(Ollama / Kimi) does the building each round, and the Claude Code session orchestrates:
it reviews, verifies, auto-fixes, and reports every iteration — with full visibility.

The heavy iteration runs on a **local/cloud builder model (cheap)**; the **Claude Code
session** (your flat-rate subscription) provides the high-judgment review and verification.

---

## Table of contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation (step by step)](#installation-step-by-step)
- [Usage (step by step)](#usage-step-by-step)
- [Configuration](#configuration)
- [Backends](#backends)
- [CLI reference](#cli-reference)
- [Verify gates & proof-of-test](#verify-gates--proof-of-test)
- [Terminal-state taxonomy](#terminal-state-taxonomy)
- [Transparency & state](#transparency--state)
- [Security](#security)
- [Tests](#tests)
- [Roadmap](#roadmap)
- [License](#license)

---

## How it works

```
You ──/loop-engineer──▶ Claude Code session (orchestrator)
                              │  owns the loop, reviews, verifies, reports
                              ▼
                    loop-engineer build  ──▶  builder model (Ollama/Kimi) writes files
                              │                          │
                              ◀───── files + progress ───┘
                              │
                    loop-engineer verify --gate <gate> --cmd "<allowlisted>"
                              │            emits JSON evidence record
                              │
              ┌───────────────┼───────────────┐
          auto-fix          ask you         done + report
        (re-loop)        (ambiguous)
```

**Fixed policy (the session always follows this):**

- **Auto-fix** anything objectively verifiable and re-loop without interrupting;
  **ask you** only for ambiguous requirements or design/scope decisions.
- **Verify** by running tests/build/lint through gate-specific verification; verdicts
  come from real artifacts, not the chat transcript.
- **Cap** at 5 auto-correct rounds before a mandatory check-in.

---

## Requirements

- **Python ≥ 3.9** (only runtime dependency is `pyyaml`).
- **Claude Code** — to run the `/loop-engineer` skill (the orchestrator).
- A **builder model** reachable via one of the [backends](#backends). The default is
  [Ollama](https://ollama.com) serving a Kimi coder model (local daemon, optionally
  proxying a `:cloud` tag).

---

## Installation (step by step)

### 1. Clone the repo

```bash
git clone https://github.com/Patibandha/kick-ass-loop-engineer.git
cd loop-engineer
```

### 2. Install the Python package (editable)

```bash
pip install -e . --break-system-packages
```

Verify the CLI is on your PATH:

```bash
loop-engineer --help        # should list: init, build, verify, run
```

### 3. Create your config

```bash
loop-engineer init          # writes ./loop-engineer.yaml from the template
```

### 4. Point the builder at your model

Edit `loop-engineer.yaml` and set the builder `model` (and `host` if remote). Example
using **Kimi K2.7 Code** served through the Ollama cloud proxy:

```yaml
builder:
  provider: ollama
  model: kimi-k2.7-code:cloud   # any pulled Ollama tag
  host: http://localhost:11434
```

If you use an Ollama `:cloud` tag, register it once with the local daemon:

```bash
ollama pull kimi-k2.7-code:cloud
ollama list | grep kimi          # confirm it's available
```

> The local Ollama daemon at `localhost:11434` proxies `:cloud` tags to ollama.com,
> so no `OLLAMA_HOST` change is needed. For a fully local model, use a non-`:cloud`
> tag (e.g. `qwen2.5-coder:7b`) that you've pulled.

### 5. Install the skill so `/loop-engineer` works in Claude Code

```bash
# Personal — available in every project:
mkdir -p ~/.claude/skills
cp -r .claude/skills/loop-engineer ~/.claude/skills/

# OR project-scoped — this repo already ships it at .claude/skills/loop-engineer/
```

Start a **new** Claude Code session so the skill is loaded, then confirm it appears
when you type `/loop-engineer`.

---

## Usage (step by step)

### A. Recommended — drive it from Claude Code (`/loop-engineer`)

This is the intended workflow. The session is a **dispatcher**: the engine decides
what's next, artifacts on disk decide state, and the builder model does the heavy
lifting inside `loop-engineer run`.

1. Open a Claude Code session in the directory where you want the work to live.
2. Invoke the skill with a goal **and** a checkable "done when":

   ```
   /loop-engineer build a CLI todo app in ./todo with add/list/done;
   done when `python -m unittest discover -s todo/tests` passes
   ```

3. The session then:
   - **refines** the objective via `loop-engineer refine --interview` — a fixed,
     six-slot think-tank interview (purpose, users, constraints, success metrics,
     anti-goals, risks) asked one question at a time until every slot is filled or
     explicitly deferred, plus a measurability lint that rejects vague `done when`
     criteria like "feels fast",
   - **enumerates its toolkit** — the skills/agents actually installed — to build
     the `--available` list,
   - **loops on `loop-engineer next`**, executing each envelope it emits, starting
     with a mandatory **research** stage — no build proceeds on unverified facts
     (see [the deep-research subsection](#e-deep-research-the-provenance-gate)) —
     then `plan` / `engine` / `review` / `qa` / `security` / `ship`:
     `invoke_skill` / `invoke_agent` (dispatch the named tool, write the stage
     artifact), `verify_citations` (re-fetch a source and record it for the
     research gate), `run_engine` (the full verified pipeline), or `ask_user` (one
     question, then resume).
4. It stops on the `terminal` envelope and writes a final report: outcome state,
   gate evidence, any `skipped_stages`, and any `failed_verdicts`.

The envelope format and artifact contract are described in
[section D](#d-the-dispatcher-loop-loop-engineer-next); the full session protocol
lives in `.claude/skills/loop-engineer/SKILL.md`.

**Tip:** always give a concrete, runnable `done when` (a test command, a build, a
lint). The loop cannot converge on a vague target and will ask you to make it
checkable.

### B. Manual / scripted — the CLI directly

Run a single guarded builder round (JSON on stdout, progress on stderr):

```bash
loop-engineer build \
  --objective "Create a Fibonacci module" \
  --done-when "fib(10) == 55 and tests pass" \
  --workspace ./fib \
  --config loop-engineer.yaml \
  --round 1 --max-rounds 5
```

Verify with a gate and an allowlisted command:

```bash
loop-engineer verify \
  --gate unit \
  --cmd "python -m unittest discover -s fib/tests" \
  --workspace ./fib
```

### C. Full verified pipeline (no Claude Code session)

`loop-engineer run` executes the complete pipeline — decompose → ensemble in
isolated worktrees → gates + proof → cross-model review → promote → governance —
and prints one JSON `RunOutcome` on exit:

```bash
loop-engineer run \
  --objective "Create a CLI calculator" \
  --done-when "add/sub/mul/div work and tests pass" \
  --workspace ./calc \
  --config loop-engineer.yaml
```

Exit code mirrors the terminal state: `0` on `success`, `2` on any other terminal
state (`stalled`, `blocked`, `budget_exceeded`, `approval_required`, `oscillation`),
`1` on a config/setup error (e.g. a builder/reviewer model-family clash).

### D. The dispatcher loop (`loop-engineer next`)

2.0-M2 adds a session-level dispatcher on top of `run`. `loop-engineer next` reads the
run's artifacts on disk and emits exactly **one** JSON envelope telling the calling
session (the Claude Code `/loop-engineer` skill) exactly which skill, agent, or
engine-command to dispatch — the session never guesses the next step itself, and the
stage is derived from artifacts, never from the transcript, so the loop resumes
cleanly after a crash or context reset:

```bash
loop-engineer next --workspace ./calc --available gsd-planner,superpowers,gstack \
  --objective "Create a CLI calculator"
```

The envelope's `next_action.type` is a switch the session dispatches on:

| `type` | Session does |
|---|---|
| `invoke_skill` / `invoke_agent` | Call the Skill or Agent tool named in `next_action.name` (fall back to `next_action.fallback` if it errors as missing), then write the result to `next_action.expects.artifact` in the `expects.schema` format and call `next` again. |
| `verify_citations` | For each `{url, quote}` in `next_action.args.citations`, fetch the url and write the fetched text to `.loop-engineer/citations.md`, then call `next` again — part of the research gate, see [section E](#e-deep-research-the-provenance-gate). |
| `run_engine` | Run `loop-engineer run` (the verified pipeline), then call `next` again. |
| `ask_user` | Ask the one question in the envelope, apply the answer, resume the loop. |
| `terminal` | Stop — report the outcome, evidence, `skipped_stages`, and any `failed_verdicts`. A `research_blocked` outcome means the research gate never converged — see section E. |

Stages run in order — **`research`** (first, mandatory), then `plan`, `engine`,
`review`, `qa`, `security`, `ship`. Each has a versioned artifact schema (`research_v1`,
`plan_v1`, `review_v1`, …) validated deterministically — file exists, non-empty,
carries the required marker (e.g. a `- [ ]` task checkbox for `plan_v1`, `VERDICT:
PASS|FAIL` for `qa_v1`/`security_v1`). An artifact that fails validation re-emits the
same envelope with `retry: 1`, then `retry: 2` and a `validation_error`; a fourth
attempt at the same invalid stage escalates to `ask_user` instead of looping forever
or silently skipping a mandatory stage. Optional stages (`qa`, `security`, `ship`) are
skipped explicitly — named in `skipped_stages` — only when no owner or fallback skill
is available for them. `research` is mandatory but never `ask_user`s or skips: an
unavailable research skill/agent or a non-converging research doc goes straight to the
`research_blocked` terminal state (section E). `loop-engineer next` always exits `0`;
the outcome lives in the envelope, not the process exit code.

### E. Deep research (the provenance gate)

Before `plan` ever runs, the loop runs a mandatory **research** stage: the session
writes `.loop-engineer/RESEARCH.md`, tagging every claim `[VERIFIED]`, `[CITED]`, or
`[ASSUMED]`. Claims under `## Decisions` are **load-bearing** (stack/API/version
choices) and can never be `[ASSUMED]`; speculative, non-load-bearing claims go under
`## Notes`:

```markdown
## Decisions
- [VERIFIED] Use Textual >= 0.60 for the TUI (source: https://textual.textualize.io "Rapid Application Development framework for Python")
- [CITED] Piper runs on CPU with no GPU (source: https://github.com/rhasspy/piper "does not require a GPU")

## Notes
- [ASSUMED] The user prefers a dark theme by default
```

`loop-engineer next` gates this deterministically, in two parts, purely from artifacts
on disk (the engine itself never touches the network):

1. **Tag coverage** (`research.check_tag_coverage`) — every `## Decisions`/`## Notes`
   bullet must carry a valid tag; any `[ASSUMED]` claim under `## Decisions` fails.
2. **Citation spot-check** (`research.select_citations` / `check_citations`) — up to
   five cited claims are selected; the session re-fetches each source via a
   `verify_citations` envelope and writes the fetched text to
   `.loop-engineer/citations.md`; the engine only compares the normalized claimed
   quote against the fetched text — a fabricated or stale citation is caught, not
   trusted.

A research doc that never clears the gate — a load-bearing `[ASSUMED]` claim, or a
citation that survives the retry budget without matching — ends the run in the
**`RESEARCH_BLOCKED`** terminal state instead of proceeding on an unverified guess.
Passing research is remembered: `research-ingest` records `## Decisions` claims to the
workspace memory (`kind="research"`), and a successful run promotes them from
`working` to `canonical` so later runs reuse settled facts instead of re-researching
them.

---

## Configuration

`loop-engineer.yaml` (created by `loop-engineer init`), following the committed
template `loop-engineer.example.yaml`:

```yaml
builder:
  provider: ollama            # claude_code | ollama | anthropic
  model: kimi-k2.7-code:cloud  # any pulled Ollama tag
  host: http://localhost:11434

reviewer:                      # cross-model reviewer for `loop-engineer run` (family must differ from the builder)
  provider: ollama
  model: qwen2.5

guardrails:
  max_file_bytes: 1000000
  max_files_per_round: 50
  allow_overwrite: true

# gates:                       # verifier gate for the pipeline (defaults shown)
#   name: unit                 # unit|security|data_leak|performance|smoke
#   cmd: "python3 -m pytest -q"  # must be on the verification allowlist
#   prove: true                # proof-of-test: revert->red->green
```

`loop-engineer.example.yaml` also documents the optional 1.0+ sections
(`ensemble`, `models`, `ledger`, `notify`) as commented-out examples with their
defaults — uncomment to change them; the pipeline runs with sane defaults even if
you delete them entirely.

> `loop-engineer.yaml` is gitignored — it's machine-specific. `loop-engineer.example.yaml`
> is the committed template; run `loop-engineer init` to generate your own.

---

## Backends

| Provider      | Use it for                                        | Billing                     |
|---------------|---------------------------------------------------|-----------------------------|
| `ollama`      | Local or cloud-proxied models (Kimi, Qwen, Llama) | Free local / cloud compute  |
| `claude_code` | Headless Claude Code (`claude -p`)                | Subscription credit / API   |
| `anthropic`   | Anthropic Messages API                            | Pay-as-you-go API           |

---

## CLI reference

- `loop-engineer init [--path PATH]` — write a starter config.
- `loop-engineer build --objective <goal> --done-when <criteria> [--workspace DIR]
  [--config FILE] [--constraints STR] [--round N] [--max-rounds N]
  [--feedback STR | --feedback-file PATH]` — one guarded builder round; JSON on
  stdout, progress bar on stderr.
- `loop-engineer verify --gate <unit|security|data_leak|performance|smoke> --cmd "<allowlisted>" [--prove] --workspace <git-dir>` — gate-based verification that emits a JSON evidence record `{gate, command, passed, evidence, proof}`.
- `loop-engineer refine --build <text> [--done-when <criterion>] [--purpose STR]
  [--users STR] [--constraints STR] [--success-metric STR (repeatable)]
  [--anti-goals STR] [--risks STR] [--consider STR] [--defer <slot1,slot2,...>]
  [--interview] [--workspace DIR]` — assess an `IdeaSpec` against the six-slot
  think-tank gate (`build` + a `done_when` that passes the measurability lint +
  every slot filled or explicitly deferred) and write `SPEC.md`/`GOAL.md` when
  ready. Not ready → `{"ready": false, "gaps": [...]}` exit 3 (add `--interview`
  for `{"ready": false, "questions": [{"slot", "question", "hint"}, ...]}`
  instead); ready → `{"ready": true, "spec": <path>, "goal": <path>}` exit 0. Each
  `success_metrics` entry is rendered as a `## Checks` bullet in `GOAL.md`
  alongside `done_when`. `--exclude` is a deprecated alias for `--anti-goals`. An
  unknown slot name in `--defer` exits 1 with `{"error": ...}`.
- `loop-engineer run --objective <goal> --done-when <criteria> [--workspace DIR]
  [--config FILE] [--constraints STR]` — standalone autonomous loop
  (builder + internal reviewer).
- `kickass <cmd>` — alias for `loop-engineer <cmd>`.

---

## Verify gates & proof-of-test

`loop-engineer verify` is the M1 verification primitive. It runs an allowlisted command
under a specific **gate category** and emits a structured JSON evidence record:

```json
{
  "gate": "unit",
  "command": "python -m unittest discover -s tests",
  "passed": true,
  "evidence": "... captured stdout/stderr ...",
  "proof": null
}
```

### The 5 gate categories

| Gate          | What it checks                                      |
|---------------|-----------------------------------------------------|
| `unit`        | Unit tests pass                                     |
| `security`    | Security scan / audit command exits 0               |
| `data_leak`   | No secrets or PII leaked into output                |
| `performance` | Benchmark or timing assertion passes                |
| `smoke`       | Basic integration / end-to-end smoke command passes |

### Proof-of-test (`--prove`)

When `--prove` is passed, Loop Engineer runs a **proof-of-test** cycle:

1. Confirms the test suite is currently green (passes).
2. Reverts the implementation file(s) to force a red state.
3. Re-runs the suite — asserts it goes red.
4. Restores the implementation — asserts it goes green again.

The gate only counts as passed if the full **green → red → green** sequence completes.
This prevents tests that always pass regardless of the code from counting as real
coverage. Verdicts come from real artifact captures, not from the chat transcript.

> **Note:** proof-of-test stashes untracked files while it reverts/restores the
> implementation. Target repos must gitignore build caches (`__pycache__/`,
> `.pytest_cache/`, etc.) or a proof will abort safely rather than risk touching
> unexpected untracked state.

---

## Terminal-state taxonomy

When the orchestrator loop exits, it reports one of seven terminal states:

| State              | Meaning                                                      |
|--------------------|--------------------------------------------------------------|
| `success`          | Goal criteria verified; loop complete.                       |
| `stalled`          | No progress across N consecutive rounds; requires human input. |
| `blocked`          | A hard blocker (missing dependency, permission denied, etc.) prevents forward progress. |
| `budget_exceeded`  | Cost or iteration budget exhausted before success.           |
| `approval_required`| A decision outside the auto-fix policy scope requires human sign-off. |
| `oscillation`      | The loop is cycling between two or more states without converging. |
| `research_blocked` | Research could not be verified: a load-bearing claim was `[ASSUMED]` or a citation could not be confirmed; the run stops rather than build on unverified facts (see [section E](#e-deep-research-the-provenance-gate)). |

---

## Transparency & state

Every run writes to `<workspace>/.loop-engineer/`:

- `STATE.md` — shared, human/model-readable context (objective, round history,
  decisions) that keeps context intact across rounds and models.
- `events.jsonl` — structured progress events.
- `rounds.json`, `feedback.md` — round records and the orchestrator's feedback.

---

## Security

Guardrails are enforced **in code, not just prompts**: workspace containment,
protected paths (`.git`, `.env`, keys, secrets), file size/count caps, and a
verification command allowlist with metacharacter rejection and a scrubbed
environment. Generated output is treated as **untrusted data** — never executed and
never followed as instructions. See
[`.claude/skills/loop-engineer/references/security.md`](.claude/skills/loop-engineer/references/security.md).

---

## Tests

```bash
python -m unittest discover -s tests -v
```

---

## Roadmap

**Shipped in 1.0 (M1–M5):**
- ✅ **Verifier spine** — executable gates + proof-of-test (revert→red→green), artifacts not transcript.
- ✅ **Prompt Writer** — idea → checkable SPEC/GOAL or "not ready".
- ✅ **Multi-role decomposition + interface contracts** and **isolated git worktrees** per role.
- ✅ **Ensemble (N=2) + verified selection** (gate-passing winner, not voting/merging).
- ✅ **Cross-model review** — reviewer model family distinct from builder (Kimi→Qwen→Claude) + oscillation stop.
- ✅ **SQLite memory + cross-run-playbook** (promote after 3 successes), **loop-auditor**, **skill-router**.
- ✅ **L3-gated autonomy** (readiness checklist + auto-drop to L2) and **escalation denylist**; `$10`/run cap + notify hook.

**Phase 2 (separate repo):** the observability/control plane — multi-channel notifier
(Telegram / WhatsApp / Slack), web dashboard, and a mobile PWA — all consumers of the
1.0 event stream.

---

## License

MIT — see [LICENSE](LICENSE).
