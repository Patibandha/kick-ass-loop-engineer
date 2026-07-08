# Verifier reference — gates, proof-of-test, evidence

The verifier is the load-bearing wall of Loop Engineer. "Done" = required gates pass,
**proven real**, judged from artifacts on disk — never from the conversation.

## The rule: artifacts, not transcript

> Use the worktree and external state as authoritative. Do not rely on intent,
> partial progress, memory of earlier work, or a plausible final answer as proof of
> completion.

Every gate runs a real command and is judged by its exit code and output. A confident
paragraph cannot talk a gate out of its verdict.

## Gates (M1 categories)

| Gate | Category | Example command | Proves |
|------|----------|-----------------|--------|
| unit | `unit` | `pytest -q` | behavior via tests + a pass/fail exit code |
| security | `security` | `bandit -r .` | no SAST findings (bandit/semgrep) |
| data_leak | `data_leak` | `gitleaks detect` | no secrets/PII (gitleaks/detect-secrets) |
| performance | `performance` | `pytest-benchmark` | latency/throughput within budget |
| smoke | `smoke` | `make test` | scripted happy-path runs end-to-end |

Commands must pass the verification allowlist (no shell metacharacters, scrubbed env,
timeout). Run a gate with:

```
loop-engineer verify --gate unit --cmd "pytest -q" --prove --workspace <dir>
```

The result is an **evidence record** (JSON): `{gate, command, passed, evidence, proof}`.
`passed` is true only when the command exits zero (and, with `--prove`, proof-of-test
holds). An errored or allowlist-rejected command is **never** `passed`.

## Proof-of-test (`--prove`): revert → red → green

A passing test is only evidence if it would fail without the change. `--prove` runs
the gate three times around the working-tree change:

1. **green_before** — gate passes with the change in place.
2. **red_when_reverted** — `git stash` the change; the gate must now **fail**.
3. **green_after** — restore the change; the gate passes again.

`proven = green_before AND red_when_reverted AND green_after`. A test that stays green
when the change is reverted is **not** proof of anything (fake done) → `proven=False`.

### Workspace requirements
- The workspace must be a **git** working tree.
- Build caches must be **gitignored** (e.g. `__pycache__/`, `*.pyc`) so the stash
  scopes to the real change and `git stash pop` cannot collide with regenerated caches.

### Safety
If `git stash pop` cannot auto-restore the change, the verifier does **not** corrupt
the tree: the change is preserved in `stash@{0}`, the record is flagged `aborted`, and
the error tells the operator to run `git stash pop` to recover. Git calls are bounded
by a timeout.

## Terminal states
A run ends in exactly one of `success · stalled · blocked · budget_exceeded ·
approval_required · oscillation`, each carrying evidence. `success` is refused without
evidence — by construction, "done" must be proven.
