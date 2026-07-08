<div align="center">

# 🔥 kick-ass-loop-engineer

**A model-agnostic, multi-agent build/review loop. A local model does the building; a verified pipeline makes sure nothing ships until it's _proven_.**

[![License: MIT](https://img.shields.io/badge/License-MIT-FF7A45.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-6FA8FF.svg)](https://www.python.org)
[![Engine](https://img.shields.io/badge/engine-stdlib--only%20%C2%B7%20no%20network-9CA2B4.svg)](#why-its-different)
[![Tests](https://img.shields.io/badge/tests-passing-35D6A4.svg)](#tests)

![The kick-ass-loop-engineer verified pipeline: objective → decompose → ensemble → gate + proof-of-test → cross-model review → promote → a proven RunOutcome](assets/pipeline.svg)

</div>

You hand it an **objective** and a checkable **done-when**. A local/cloud builder model
(Ollama / Kimi) writes the code each round — cheaply. A verified pipeline decomposes the
work, races competing attempts in throwaway git worktrees, **proves** the tests actually
test the change, has a _different_ model family review the winner, and promotes only the
one attempt that survived. You can drive it from a **Claude Code session** (`/loop-engineer`)
or run the pipeline standalone from the CLI.

---

## The problem: you're the loop

Work with an AI model the usual way and **you are the loop.** You prompt, read the output,
spot the wrong import or the invented API, re-type the correction, and go again — holding all
the state in your head and rebuilding context from scratch every session. The model does the
thirty-second part; **you** run the iteration by hand, forever. Call it the **re-prompting tax.**

Every workaround people reach for — snippet files, a "here's our stack" preamble pasted each
morning, a doc of prompts that "worked" — is really an attempt to escape the single turn.

## What changes: you give it a task, it decides what it needs

kick-ass-loop-engineer takes the loop off your hands. You describe an **outcome** and a
**checkable done-when** — and the machine works out the rest:

- it **interviews you only on the genuinely ambiguous parts** — a six-slot think-tank
  (purpose, users, constraints, success metrics, anti-goals, risks), one question at a time —
  and infers the rest (indentation, test runner, file layout) from your repo;
- it **researches** the facts it will rely on, and refuses to build on an unverified guess;
- it **builds, verifies against real artifacts, and has a different model family review** the
  result — then reports an outcome you can act on.

You stop babysitting turns and start reviewing outcomes. That's the whole pitch: **describe
the destination, not every step.**

![Hand it a task — it runs the build → verify → review loop.](assets/hero-loop.gif)

## Who it's for

- **Solo devs & indie hackers** who want a second (and third) set of hands that works while
  they don't — without a frontier-model bill for every iteration.
- **Teams** that want *cheap* iteration and *high-judgment* review: the grind runs on a local
  model; the expensive model only arbitrates.
- **Anyone running local models** (via Ollama — Llama, Qwen, Kimi, DeepSeek) who wants real
  verification instead of "the tests pass, trust me."
- **Tinkerers & researchers** exploring autonomous build/verify loops who want the guardrails
  enforced in code, not in a polite prompt.

If you've ever thought *"I've explained this to the model four times already"* — that's the
itch this scratches.

---

## Why it's different

Most "AI writes code" loops stop at _"the tests pass."_ This one doesn't trust that.

| | What most loops do | What kick-ass-loop-engineer does |
|---|---|---|
| **Verification** | Trusts a green check | **Proof-of-test**: reverts the change, confirms tests go **red**, restores, confirms **green** — a test that can't fail isn't coverage |
| **Selection** | One attempt, hope it's right | **Ensemble** of _N_ attempts in isolated git worktrees; the gate-passing one wins |
| **Review** | The same model marks its own work | **Cross-model review** — the reviewer is a _different model family_ (Kimi → Qwen → Claude) |
| **Cost** | Burns frontier tokens every round | **$0 on local models** — the builder is Ollama/Kimi; the frontier session only judges |
| **Trust** | Prompt-level "please don't" | Guardrails enforced **in code** — workspace jail, protected paths, command allowlist, scrubbed env |
| **Provenance** | Builds on model guesses | A mandatory **research gate** — load-bearing claims can't be `[ASSUMED]`; citations are re-fetched and checked |
| **Footprint** | Heavy dependency tree | **stdlib-only engine**, zero network calls inside the pipeline (only `pyyaml` for config) |

Every merge carries its own proof. That's the whole idea.

---

## How it works

The session is a **dispatcher**; the engine decides what's next from artifacts on disk; the
builder model does the heavy lifting. A single run flows through:

```
objective + done-when
        │
        ▼
  ① decompose      split into independently verifiable slices
        │
        ▼
  ② ensemble       race N attempts, each in its own throwaway git worktree
        │
        ▼
  ③ gate + PROOF   run the gate — then prove it: green → revert → red → restore → green
        │
        ▼
  ④ cross-model    a different model family audits the winning attempt
     review
        │
        ▼
  ⑤ promote        merge the one proven, reviewed attempt; discard the rest
        │
        ▼
   RunOutcome  { state, reason, evidence }   ← one JSON object, with proof
```

When driven from Claude Code, a session-level **dispatcher loop** (`loop-engineer next`)
wraps this with staged skills — a mandatory **research** gate first, then `plan` → `engine`
→ `review` → `qa` → `security` → `ship` — each emitting one JSON envelope the session acts
on. Stage is derived from artifacts, never the transcript, so the loop resumes cleanly after
a crash or context reset.

---

## Quick start

```bash
# 1. install (Python 3.9+; only runtime dep is pyyaml)
git clone https://github.com/Patibandha/kick-ass-loop-engineer.git
cd kick-ass-loop-engineer
pip install -e . --break-system-packages

# 2. point it at a builder model (default: Ollama serving a Kimi coder tag)
loop-engineer init                 # writes ./loop-engineer.yaml
ollama pull kimi-k2.7-code:cloud   # or any local tag you've pulled

# 3. run the full verified pipeline (workspace must be a git repo)
loop-engineer run \
  --objective "add a rate limiter to the API" \
  --done-when "python3 -m pytest -q passes" \
  --workspace ./my-project \
  --config loop-engineer.yaml
```

Exit code mirrors the terminal state: `0` = `success`, `2` = any other terminal state, `1` = a
config/setup error (e.g. a builder/reviewer same-family clash). The full `RunOutcome`
(`{state, reason, evidence}`) is printed as JSON on stdout.

**Prefer to drive it from Claude Code?** Install the skill and use `/loop-engineer`:

```bash
mkdir -p ~/.claude/skills && cp -r .claude/skills/loop-engineer ~/.claude/skills/
```

Then, in a Claude Code session, give it a goal **and** a runnable done-when:

```
/loop-engineer add a CLI todo app in ./todo with add/list/done;
done when `python -m unittest discover -s todo/tests` passes
```

> **Always give a concrete, runnable done-when** (a test command, a build, a lint). The loop
> cannot converge on a vague target ("feels fast") and will ask you to make it checkable.

---

## The two engines

| | Role | Runs on | Cost |
|---|---|---|---|
| **The muscle** | Writes the code each round, inside the guarded pipeline | Local/cloud model via Ollama (Kimi, Qwen, Llama…) | **$0** on local models |
| **The conductor** | Orchestrates the loop, dispatches skills, arbitrates on risk | Your Claude Code session (flat-rate) | subscription |

The engine itself makes **no network calls** — it shells out to your configured provider and
otherwise runs on the Python standard library.

### Bring your own model — stated accurately

Model-agnostic is the whole point, so here's the honest state of it. **Today there are three
native backends:** `ollama` (any local or cloud-proxied model — Meta **Llama**, **Qwen**,
**Kimi**, **DeepSeek**), `claude_code`, and `anthropic`. Use any of them as the builder or the
reviewer; the one rule the engine enforces is that **the reviewer is a different model family
than the builder**, so no model grades its own homework.

ChatGPT / Gemini and other vendors are reachable **only through an OpenAI-compatible or Ollama
shim** right now — there's no first-class adapter yet. Native adapters for every major web AI
are on the [roadmap](#roadmap); the core is built so that's a matter of adding an adapter, not
a rewrite.

![Any model as the muscle; a different family reviews.](assets/model-agnostic.gif)

---

## Configuration

`loop-engineer init` writes `loop-engineer.yaml` from the committed
[`loop-engineer.example.yaml`](loop-engineer.example.yaml) template:

```yaml
builder:
  provider: ollama            # claude_code | ollama | anthropic
  model: kimi-k2.7-code:cloud  # any pulled Ollama tag
  host: http://localhost:11434

reviewer:                      # cross-model reviewer — family MUST differ from the builder
  provider: ollama
  model: qwen2.5

guardrails:
  max_file_bytes: 1000000
  max_files_per_round: 50
  allow_overwrite: true

# gates:                       # verifier gate for the pipeline (defaults shown)
#   name: unit                 # unit | security | data_leak | performance | smoke
#   cmd: "python3 -m pytest -q"  # must be on the verification allowlist
#   prove: true                # proof-of-test: revert → red → green

# ledger:  { run_cap_usd: 10, month_cap_usd: 200 }   # $ caps + notify hook (optional)
# ensemble: { n: 2 }                                  # competing attempts per hard slice
```

`loop-engineer.yaml` is gitignored (machine-specific); the `.example.yaml` is the committed
template. The pipeline runs with sane defaults even if you delete the optional sections.

### Backends

| Provider | Use it for | Billing |
|---|---|---|
| `ollama` | Local or cloud-proxied models (Kimi, Qwen, Llama) | Free local / cloud compute |
| `claude_code` | Headless Claude Code (`claude -p`) | Subscription credit |
| `anthropic` | Anthropic Messages API | Pay-as-you-go API |

---

## CLI reference

| Command | What it does |
|---|---|
| `loop-engineer init [--path PATH]` | Write a starter `loop-engineer.yaml`. |
| `loop-engineer run --objective … --done-when … [--workspace DIR] [--config FILE]` | The **full verified pipeline** (decompose → ensemble → gates+proof → cross-model review → promote). Prints one JSON `RunOutcome`. |
| `loop-engineer refine --build "<idea>" [--done-when …] [--interview]` | Assess an idea against a six-slot think-tank gate + a measurability lint; write `SPEC.md`/`GOAL.md` when ready, or return the gaps/questions. |
| `loop-engineer next --workspace DIR [--available csv]` | Emit **one** dispatcher envelope (`invoke_skill` / `invoke_agent` / `run_engine` / `verify_citations` / `ask_user` / `terminal`) for the calling session. Always exits 0; state lives in the envelope. |
| `loop-engineer verify --gate <gate> --cmd "<allowlisted>" [--prove] --workspace DIR` | Run a named gate and emit a JSON evidence record `{gate, command, passed, evidence, proof}`. |
| `loop-engineer build --objective … --done-when … [--round N] [--max-rounds N]` | One guarded builder round (low-level; the pipeline runs this internally). JSON on stdout, progress on stderr. |

`kickass <cmd>` is an alias for `loop-engineer <cmd>`.

---

## Proof-of-test

![green → revert → red → restore → green.](assets/proof-of-test.gif)

The feature the whole design is built around. When a gate runs with `prove: true`, the engine
doesn't just check that the suite is green — it **proves the test exercises the change**:

1. Confirm the suite is currently **green**.
2. Revert the implementation file(s) to force a **red** state.
3. Re-run — assert it goes **red** (if it stays green, the test is theater → the gate fails).
4. Restore the implementation — assert it goes **green** again.

The gate only counts as passed if the full **green → red → green** sequence completes.
Verdicts come from real artifact captures, not the chat transcript.

### Gate categories

| Gate | Checks |
|---|---|
| `unit` | Unit tests pass |
| `security` | Security scan / audit exits 0 |
| `data_leak` | No secrets or PII leaked into output |
| `performance` | Benchmark / timing assertion passes |
| `smoke` | Basic integration / end-to-end command passes |

---

## Terminal states

Every run ends in exactly one of seven states (the process exit code is `0` for `success`,
`2` for everything else):

| State | Meaning |
|---|---|
| `success` | Goal verified — with proof. |
| `stalled` | No attempt passed its gate; needs human input. |
| `blocked` | A hard blocker (missing dep, permission, final gate) stops progress. |
| `budget_exceeded` | The `$`/iteration budget was exhausted first. |
| `approval_required` | A risky action hit the escalation denylist and needs sign-off. |
| `oscillation` | The reviewer keeps returning the same finding — no convergence. |
| `research_blocked` | A load-bearing claim was `[ASSUMED]`, or a citation couldn't be confirmed — the run stops rather than build on a guess. |

---

## Security

Guardrails are enforced **in code, not just prompts**:

- **Workspace jail** — writes can't escape the workspace; protected paths (`.git`, `.env`,
  keys, `*secret*`, `*credentials*`) are refused; file size/count caps.
- **Verification allowlist** — only known test/lint/build commands run, with shell
  metacharacters rejected and `ANTHROPIC_API_KEY`/`AUTH_TOKEN` scrubbed from the child env.
- **Escalation denylist** — risky verbs (`deploy`, `drop table`, `rm -rf`, force-push) and
  sensitive paths (`secrets/`, `*/auth/`, `*payment*`) park the run for human approval.
- Generated output is treated as **untrusted data** — never executed, never followed as
  instructions. See [`.claude/skills/loop-engineer/references/security.md`](.claude/skills/loop-engineer/references/security.md).

---

## Roadmap

The model-agnostic core is built so that **adding a provider is an adapter, not a rewrite** —
which is exactly where this is headed:

- **Universal provider support** — native adapters for **every major web AI agent on the
  market**: OpenAI / ChatGPT, Google **Gemini**, hosted Meta **Llama**, Anthropic **Claude**,
  and **Japanese models** (Sakana AI, ELYZA, Preferred Networks **PLaMo**, Rakuten AI, NTT
  tsuzumi) — plus anything reachable over an OpenAI-compatible endpoint. The goal: pick
  *literally any* AI agent, anywhere, as your builder or reviewer, and mix families freely for
  cross-model review.
- **Observability & control plane** — a separate layer for multi-channel notifications and a
  live dashboard, so you can watch and steer long autonomous runs without babysitting a terminal.

Shipped today are the three native backends above (`ollama`, `claude_code`, `anthropic`);
everything in this section is honestly labeled **not yet built.**

---

## Version history

Full detail in [CHANGELOG.md](CHANGELOG.md). The short story:

| Version | Milestone | What it added |
|---|---|---|
| **2.0.0** | Deep research + GA _(current)_ | Mandatory research/provenance gate (tag coverage + citation re-fetch), `research_blocked` state, and the `2.0` line's GA. |
| `2.0.0-alpha.3` | Think-tank interview (2.0-M3) | Six-slot `refine --interview` (purpose/users/constraints/metrics/anti-goals/risks) + a measurability lint that rejects vague done-whens. |
| `2.0.0-alpha.2` | Next protocol (2.0-M2) | The session **dispatcher loop** — `loop-engineer next` emits one artifact-derived envelope per step; crash/reset-safe. |
| `2.0.0-alpha.1` | Pipeline core (2.0-M1) | The standalone verified pipeline: decompose → ensemble in worktrees → gates+proof → cross-model review → promote, as one `RunOutcome`. |
| **1.0.0** | First stable | Verifier spine + proof-of-test, ensemble (N=2) with verified selection, cross-model review, SQLite memory + cross-run playbook, loop-auditor, L3-gated autonomy + escalation denylist, `$`/run cap. |
| `1.0.0-alpha.1…4` | 1.0 M1–M4 | Rebrand + verifier core → single-loop brain → multi-model + ensemble → memory + governance. |
| `0.1 – 0.2` | Origin (as `loopforge`) | Initial model-agnostic build/review loop with an internal reviewer. |

---

## Tests

```bash
python3 -m pytest -q        # the engine's own suite
```

The engine is Python 3.12-tested, `>= 3.9` compatible, and depends only on `pyyaml` at runtime.

---

## License

[MIT](LICENSE) © Alpha AI. Build freely — just keep the proof.
