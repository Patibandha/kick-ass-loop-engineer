# Using Loop Engineer 3.2 with Jev: the staffing controller

This guide shows how to switch on the 3.2 **staffing controller**, connect it to
[TypeSafe Jev](https://docs.typesafe.ai), run with it, and read what it decided. It
assumes you already have Loop Engineer working (see the [README](../README.md)
quick start).

**Contents**

1. [What the staffing controller does](#1-what-the-staffing-controller-does)
2. [How it decides: the model proposes, the engine disposes](#2-how-it-decides-the-model-proposes-the-engine-disposes)
3. [Install or upgrade to 3.2](#3-install-or-upgrade-to-32)
4. [Get access to Jev](#4-get-access-to-jev)
5. [Configure it](#5-configure-it)
6. [Run it](#6-run-it)
7. [What happens during a run](#7-what-happens-during-a-run)
8. [Quality floors, in full](#8-quality-floors-in-full)
9. [Human sign-off (L4)](#9-human-sign-off-l4)
10. [The model-tier cascade](#10-the-model-tier-cascade)
11. [Reading the decision journal](#11-reading-the-decision-journal)
12. [Cost, privacy and security](#12-cost-privacy-and-security)
13. [Failure modes and fallbacks](#13-failure-modes-and-fallbacks)
14. [Troubleshooting](#14-troubleshooting)
15. [Limitations, stated plainly](#15-limitations-stated-plainly)

---

## 1. What the staffing controller does

Before 3.2, every run paid for the same team whatever the objective: `ensemble.n`
competing attempts per slice, a QA pass, a security review, a ship stage, and one fixed
review depth. A README typo and a payment flow got identical treatment.

The staffing controller asks a **decision model** to size the team for each run. Jev
doesn't generate text. It picks one of the answers you allow, and returns a probability
for every option plus a confidence score. Each call takes a few hundred milliseconds and
only input tokens are billed.

| Decision | Options | Asked when |
|---|---|---|
| Risk tier | `low` · `medium` · `high` · `critical` | every run |
| Security depth | `L1` · `L2` · `L3` · `L4` | every run |
| QA pass | `run` · `skip` | every run |
| DevOps / ship stage | `run` · `skip` | every run |
| Review depth | `light` · `standard` · `strict` | every run |
| Builders must write tests | `gates_only` · `write_tests` | every run |
| Architect stage | `run` · `skip` | only with `architect: auto` |
| Attempts for each slice | `1` … `max_attempts` | after decomposition |
| Starting model tier for each slice | `t0` … `tN` | only when `decision.tiers` is set |
| Retry once after a stall? | `retry` · `stop` | only when a slice has no passing attempt |

The security levels mean:

| Level | What runs |
|---|---|
| **L1** | Only the security gates you configured. The session's security stage is skipped. |
| **L2** | Adds the session's AI security-review stage (`security.md`). |
| **L3** | L2, plus strict code review: HIGH and CRITICAL findings block promotion. |
| **L4** | L3, plus a human must sign off before the ship stage. |

The review depths map onto `review.block_on`:

| Depth | Findings that block promotion |
|---|---|
| `light` | none (advisory only) |
| `standard` | `CRITICAL` |
| `strict` | `HIGH` and `CRITICAL` |

---

## 2. How it decides: the model proposes, the engine disposes

1. **Floors first.** Deterministic code works out the minimum this objective needs. A
   payment change can never have a lighter review than the floor (see
   [section 8](#8-quality-floors-in-full)).
2. **The model chooses.** Jev answers all the questions in **one batched call**.
3. **Clamp.** The engine applies `max(floor, choice)` to every answer. The model can raise
   a floor and can never lower one.
4. **Confidence gate.** An answer below `min_confidence` is discarded and the question's
   **default** is used instead. The defaults are exactly what 3.1 did, so a
   low-confidence run behaves like 3.1, never like a cheaper and sloppier one.
5. **Record.** The plan is written to `.loop-engineer/staffing.json` and every decision
   to `.loop-engineer/decisions.jsonl`.

Some things are **never delegated**: gate verdicts, proof-of-test, the escalation
denylist, the budget cap, the research honesty gate, and winner selection among attempts.
The decision model chooses how much effort to spend. It never decides whether the work is
correct.

---

## 3. Install or upgrade to 3.2

```bash
# fresh install
git clone https://github.com/Patibandha/kick-ass-loop-engineer.git
cd kick-ass-loop-engineer
pip install -e . --break-system-packages

# or upgrade an existing checkout
git pull && pip install -e . --break-system-packages

loop-engineer --help        # sanity check
python -c "import kickass_loop_engineer as k; print(k.__version__)"   # 3.2.0
```

Update the Claude Code skill as well, so `/loop-engineer` knows about staffing plans and
sign-off:

```bash
cp -r .claude/skills/loop-engineer ~/.claude/skills/
```

The engine still depends only on `pyyaml`. The Jev client uses Python's standard library.

---

## 4. Get access to Jev

There are two routes. Both reach the same model, priced at $0.042 per million input
tokens with output free.

### Option A: Cloudflare Workers AI (recommended)

Cloudflare hosts Jev as `typesafe/jev` and lists it with **zero data retention**. It
works today, has a free tier, and doesn't need a TypeSafe account.

1. Create a free account at <https://dash.cloudflare.com>.
2. Copy your **Account ID** from the account overview page.
3. Go to **My Profile → API Tokens → Create Token**. Use the **Workers AI** template, or
   grant the **Workers AI: Read** and **Workers AI: Edit** permissions.
4. Export both values on the machine that runs the loop. Never put them in config or
   commit them.

   ```bash
   export CLOUDFLARE_ACCOUNT_ID=your-account-id
   export CLOUDFLARE_API_TOKEN=your-token
   ```

### Option B: TypeSafe directly

1. Get an API key at <https://console.typesafe.ai/keys>. TypeSafe was running an
   early-access waitlist as of 2026-09.
2. Either `export TYPESAFE_API_KEY=your-key`, or keep the key in your OS credential store
   and point the engine at it, so the key never sits in an environment variable:

   ```bash
   pip install 'kick-ass-loop-engineer[keyring]'
   python -c "import keyring, getpass; keyring.set_password('loop-engineer', 'TYPESAFE_API_KEY', getpass.getpass())"
   ```

   ```yaml
   decision:
     route: typesafe
     api_key_keyring: loop-engineer/TYPESAFE_API_KEY
   ```

On the direct route, zero data retention is offered only on TypeSafe's enterprise plans.
Read [section 12](#12-cost-privacy-and-security) before choosing it.

### Check your access

This sends one tiny question through the same client the engine uses:

```bash
python - <<'EOF'
from kickass_loop_engineer.decision.base import Question
from kickass_loop_engineer.decision.jev import JevBackend
q = Question("ok", "Is this a connectivity test?", ("yes", "no"), "yes")
b = JevBackend(route="cloudflare").decide({"objective": "connectivity test"}, [q])
print(b.backend, b.model, b.answers["ok"], f"${b.cost_usd:.8f}")
EOF
```

Use `route="typesafe"` for option B. A `DecisionError` names what's missing, such as an
unset environment variable or an HTTP 401.

---

## 5. Configure it

Add a `decision:` section to `loop-engineer.yaml`. **If the section is absent, the
controller is off** and every run is exactly the same as 3.1.

### Minimal

```yaml
decision:
  backend: jev
  route: cloudflare
```

### Recommended

```yaml
decision:
  backend: jev                     # jev (falls back to rules) | rules (offline, no calls)
  route: cloudflare                # cloudflare | typesafe
  model: jev-1.13.0                # pinned (typesafe route); aliases move between releases
  min_confidence: 0.7              # below this, the safe default is used
  max_attempts: 3                  # ceiling on attempts per slice the model may choose
  floors:
    security: L1                   # global minimum security level
    review: light                  # global minimum review depth
  signoff_domains: [payments, infra, auth, secrets]   # force L4 human sign-off
  tiers:                           # optional cheap -> strong builder ladder
    - model: qwen2.5-coder:7b
    - model: kimi-k2.7-code:cloud
```

### Key reference

| Key | Default | Meaning |
|---|---|---|
| `backend` | `jev` | `jev` asks Jev and falls back to rules. `rules` makes no network calls and always uses the defaults. |
| `route` | `typesafe` | `typesafe` (needs `TYPESAFE_API_KEY`) or `cloudflare` (needs `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`). |
| `model` | `jev-1.13.0` | Pinned Jev version for the TypeSafe route. |
| `base_url` | route default | Override the API root, for example for a proxy. |
| `api_key_env` | route default | **Name** of the environment variable holding the key. The key itself never goes in config. |
| `api_key_keyring` | unset | `"<service>/<username>"` in the OS credential store (Windows Credential Manager, macOS Keychain, Secret Service). The key is read at call time, held only for the request, and never written to the environment. Needs `pip install 'kick-ass-loop-engineer[keyring]'`. |
| `account_id_env` | `CLOUDFLARE_ACCOUNT_ID` | Name of the environment variable holding the Cloudflare account ID. |
| `timeout_s` | `5` | Timeout per request, in seconds. |
| `retries` | `1` | Extra attempts on 429, 5xx, 529 or a network error. |
| `min_confidence` | `0.7` | Answers below this use the question's default. |
| `max_attempts` | `3` | Upper bound for "attempts per slice". |
| `floors.security` | `L1` | Global minimum security level. |
| `floors.review` | `light` | Global minimum review depth. |
| `signoff_domains` | `[]` | Domains that force L4: any of `auth`, `payments`, `secrets`, `data`, `infra`. |
| `tiers` | `[]` | Builder ladder from cheapest to strongest. Each entry takes the same keys as `ensemble.attempts`: `model`, `temperature`, `provider`, `family`. |

Configuration is validated **before any spend**. An unknown backend, route, level or
domain, or a number out of range, stops the run with a clear error.

The cross-model rule still holds: every tier's model family is checked against the
reviewer at configuration time.

---

## 6. Run it

### From Claude Code (recommended)

```
/loop-engineer add a password-reset endpoint; done when `python -m pytest -q` passes
```

The skill runs the usual dispatcher loop. Three things are new in 3.2:

- The engine plans the team inside `loop-engineer run`.
- Optional stages the plan chose not to pay for are reported as **skipped by plan**.
- For L4, the session stops with `status: "signoff_required"` and asks you before ship
  (see [section 9](#9-human-sign-off-l4)).

### From the CLI

```bash
loop-engineer run \
  --objective "add a password-reset endpoint" \
  --done-when "python -m pytest -q passes" \
  --workspace ./my-project \
  --config loop-engineer.yaml
```

### Offline or deterministic runs

Set `backend: rules`. The controller still writes `staffing.json` and applies the
floors, but every model choice is the default. This is useful in CI, or to compare
against a Jev-staffed run.

---

## 7. What happens during a run

```
decompose ─▶ staffing (1 Jev call) ─▶ [architect?] ─▶ staffing:slices (1 Jev call)
          ─▶ for each slice: attempts on chosen tier ─▶ gates ─▶ (stall? recover:<slice>, 1 call)
          ─▶ review (block_on = config ∪ depth) ─▶ promote ─▶ final gates ─▶ outcome logged
```

1. **`staffing`.** After the first decomposition, Jev sees an allowlisted summary: the
   objective, done-when, constraints, mode, slice list, detected sensitive domains, and
   your last few run outcomes. It answers the run-level questions. Floors are applied and
   `staffing.json` is written.
2. **Effects applied right away:**
   - `review.block_on` becomes the configured severities plus the chosen depth.
   - `write_tests` adds a "write tests" constraint to every builder brief.
   - The architect stage follows the plan (only with `architect: auto`).
3. **`staffing:slices`.** For every slice, Jev chooses the number of attempts and,
   with tiers configured, the starting tier.
4. **Stall recovery.** If no attempt at a slice passes, Jev is asked once whether to
   retry. It defaults to `stop`, which is the 3.1 behavior. A retry runs **one** extra
   attempt, on the next tier when tiers exist, with the failure observations included.
5. **Outcome.** At the end, the terminal state, total spend and attempt counts are added
   to the decision journal next to the plan.
6. **Session stages.** `loop-engineer next` reads `staffing.json`:
   - `qa: false` skips QA.
   - `security: L1` skips the AI security stage.
   - `devops: false` skips ship.
   - `L4` adds the sign-off step before ship.

   The terminal envelope carries a `staffing` block:

   ```json
   "staffing": {"skipped_by_plan": ["qa", "ship"], "risk": "low",
                "security": "L1", "review": "light", "qa": false,
                "devops": false, "domains": []}
   ```

---

## 8. Quality floors, in full

The engine computes these floors before it reads any model answer. Domains are detected
by whole-word keyword match over the objective, done-when, constraints and slice
objectives.

| Domain | Example keywords |
|---|---|
| `auth` | auth, login, password, oauth, jwt, session, permission, rbac, sso |
| `payments` | payment, billing, invoice, checkout, stripe, paypal, refund, subscription |
| `secrets` | secret, credential, api key, private key, encryption, crypto, .env |
| `data` | migration, schema, database, sql, delete, drop, backup, pii, gdpr |
| `infra` | deploy, docker, kubernetes, k8s, terraform, ci, pipeline, nginx, systemd, dns |

| Condition | Floor |
|---|---|
| Any sensitive domain | security ≥ L2, review ≥ standard, QA runs, risk is never `low` |
| `auth`, `payments` or `secrets`, or risk `critical` | security ≥ L3, review `strict` |
| Risk `high` | review ≥ standard, `write_tests` forced on |
| Risk `critical` | review `strict`, `write_tests` forced on |
| A domain listed in `signoff_domains` | security = L4 |
| `floors.security` / `floors.review` | global minimums |
| Configured `review.block_on` | never removed, only added to |

The keyword match leans toward more scrutiny. For example, "schema" in a JSON-schema task
raises the floor too. That costs some extra tokens, never quality.

---

## 9. Human sign-off (L4)

When the plan is L4, after QA and security, `loop-engineer next` returns:

```json
{"stage": "signoff", "status": "signoff_required",
 "next_action": {"type": "ask_user", "question": "Security sign-off required (L4) ..."}}
```

1. Review `.loop-engineer/security.md` and the change.
2. To approve, reply **APPROVE**. The session writes `.loop-engineer/signoff.md`
   containing `APPROVED: <your name>`. If something must change, describe it instead.
3. `next` validates the sign-off and moves on to ship.

The skill is told never to write a sign-off without your explicit approval.

---

## 10. The model-tier cascade

Without tiers, the chosen number of attempts uses your configured `ensemble` specs,
trimmed or extended along the temperature ladder. The gates pick the winner, with the
fewest files breaking ties.

With `decision.tiers`:

- Attempt *i* uses tier `min(start + i, last)`.
- The ensemble **stops at the first passing attempt**. The cheapest model that passes
  wins, and stronger tiers are never paid for when a cheap one succeeds.
- Stall recovery climbs to the next tier above the highest one used. If you're already on
  the top tier, recovery isn't offered.

Order tiers from cheapest to strongest. A local 7B model first and a cloud model last is
a common ladder.

---

## 11. Reading the decision journal

`.loop-engineer/decisions.jsonl` is append-only, with one JSON object per line.

**A decision line:**

```json
{"kind": "decision", "run_id": "run-1a2b3c4d", "site": "staffing",
 "backend": "jev", "model": "jev-1.13.0", "latency_ms": 241.7, "cost_usd": 4.2e-05,
 "fallbacks": [],
 "answers": {"qa": {"choice": "skip", "applied": "skip", "accepted": true,
                    "confidence": 0.86, "probabilities": {"run": 0.07, "skip": 0.93}}}}
```

- `choice` is what the model said. `applied` is what the engine used after the confidence
  gate. Floors are applied afterwards and show up in `staffing.json` under `floors`.
- `accepted: false` means the answer was below `min_confidence`.
- `fallbacks` lists backends that failed before the one that answered, with the reason.

**An outcome line:**

```json
{"kind": "outcome", "run_id": "run-1a2b3c4d", "state": "success", "usd": 0.31,
 "attempts": 2, "losing_attempts": 0,
 "plan": {"risk": "low", "security": "L1", "review": "light", "qa": false,
          "devops": false, "slices": {"core": {"attempts": 1, "tier": 0}}}}
```

**Useful queries:**

```bash
# applied plan for the last run
cat .loop-engineer/staffing.json

# every decision: site, backend, applied choices
jq -c 'select(.kind=="decision") | {site, backend, applied: (.answers | map_values(.applied))}' \
  .loop-engineer/decisions.jsonl

# answers the confidence gate rejected
jq -c 'select(.kind=="decision") | .answers | to_entries[] | select(.value.accepted==false)' \
  .loop-engineer/decisions.jsonl

# spend and outcome per run
jq -c 'select(.kind=="outcome") | {run_id, state, usd, attempts}' .loop-engineer/decisions.jsonl
```

**Replay on resume.** Each decision is keyed by a hash of its input. A resumed run finds
the recorded decision and reuses it without calling Jev, so resuming stays deterministic
even though the model isn't.

---

## 12. Cost, privacy and security

**Cost**
- A run makes two staffing calls, plus one per stalled slice. With an allowlisted state of
  a few thousand tokens, that's fractions of a cent.
- Jev spend goes through the same cost ledger as builder calls, as `decide:<site>`, so
  your run cap includes it.

**What leaves your machine**
- Only allowlisted, typed facts: the objective, done-when, constraints, mode, slice roles
  and objectives (each truncated), detected domains, configured limits, and recent run
  outcomes.
- Never sent: code, diffs, gate output, logs, file contents, paths outside the slice plan,
  or environment values.
- Strings are truncated, lists are capped, and the whole state is capped at 16 KB.

**Secret guard**
- If the state contains anything shaped like a secret (`sk-…`, `AKIA…`, a PEM header, a
  JWT, a GitHub or Slack token, or `password=…`), the call is **not made**. Rules answer
  instead, and a `decision_state_rejected` event is logged.

**Keys**
- `TYPESAFE_API_KEY` and `CLOUDFLARE_API_TOKEN` are removed from the environment of every
  verification subprocess and every agentic builder CLI.
- They're read only inside the Jev client, and config holds only the environment variable
  **names**.

**Retention**
- Cloudflare lists `typesafe/jev` with zero data retention.
- On TypeSafe's direct API, zero data retention is enterprise-only. Use the Cloudflare
  route if your objectives describe anything sensitive.

**Validation**
- Every answer is strictly validated: the choice must be a declared option, probabilities
  must be in [0, 1] and sum to about 1, and confidence must be in [0, 1].
- A malformed answer is never acted on.

---

## 13. Failure modes and fallbacks

| Situation | What happens |
|---|---|
| Key or account ID not set | Rules answer, the run continues, and the failure is listed under `fallbacks`. |
| Timeout, 429, 5xx, 529 | One retry with backoff, then rules answer. |
| 3 consecutive failures | Circuit breaker opens, and Jev isn't called again this run. |
| Malformed or invalid answer | Rejected, and rules answer. |
| Low confidence | That question uses its default (`accepted: false`). |
| Secret-shaped state | No call is made, and rules answer. |
| Resume after a crash | Recorded decisions are replayed, with no new calls. |

In every case the run continues with the 3.1 behavior for whatever couldn't be decided.
A Jev problem never fails a run.

---

## 14. Troubleshooting

| Symptom | Check |
|---|---|
| Every decision shows `"backend": "rules"` | `fallbacks` in `decisions.jsonl` names the reason, usually an unset key or 401. Run the access check in [section 4](#check-your-access). |
| `RuntimeError: decision.…` at start | An invalid config value. The message names the key and the allowed values. |
| QA or security never runs | `staffing.json` shows `qa: false` or `security: "L1"`. That was the plan's choice, and it's reported as `skipped_by_plan`. Raise `floors` to force them. |
| A run stops at `signoff_required` | Expected for L4. See [section 9](#9-human-sign-off-l4). Remove the domain from `signoff_domains` if you don't want sign-off for it. |
| Too much scrutiny for simple tasks | Domain keywords matched, so check `staffing.json` → `domains` and `floors`. The floors lean toward safety on purpose. |
| Stronger tier never used | That's the cascade working: a cheaper tier passed first. |
| Want to turn it off | Remove the `decision:` section, or set `backend: rules`. |

---

## 15. Limitations, stated plainly

- **Jev's confidence isn't calibrated on your data.** Independent tests found it can be
  confidently wrong, for example on inputs that fit none of the options, or on genuinely
  random outcomes. That's why the floors are code and low confidence falls back.
  Calibrating thresholds from your own `decisions.jsonl` is the next milestone.
- **Jev is hosted only and in early access.** Outages are handled by the fallback, but
  they do happen.
- **Domain detection uses keywords.** It's deliberately conservative and can
  over-trigger. It never under-triggers in a way that lowers quality, because a missed
  domain only means the model's own choice applies above the global floors.
- **Measured savings aren't published yet.** They depend on your objectives and model
  ladder. Compare runs with `backend: rules` against `backend: jev` using the outcome
  lines in the journal.
