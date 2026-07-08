# Skill-router reference — governance

The single most important governance rule: **`/loop-engineer` is the ONLY skill that
auto-invokes.** Every framework it composes (GSD, gstack, Superpowers, role agents) is
called **by exact name at a fixed stage** — never by keyword/similarity selection, which
is what causes the documented skill-conflict failures (duplicate reviewers, description-
budget starvation, mis-routing).

## Rules
- Owned sub-skills set `disable-model-invocation: true` (works for user skills) so only
  the router triggers them.
- One owner per stage. Deterministic resolution: use the owner if present, else the
  named fallback — no fuzzy matching.
- Each route carries a **`kind`** — `skill` or `agent` — so the session knows whether to
  reach it with the **Skill tool** or the **Agent tool**. The `next` envelope maps this to
  `next_action.type`: `invoke_skill` for skills, `invoke_agent` for agents.
- The router (`kickass_loop_engineer.router.SkillRouter`) encodes the map below;
  `resolve(stage, *, available)` returns the owner or fallback `StageRoute(name, kind)`,
  `owner(stage)` raises on an unknown stage, `is_sole_auto_invoked(name)` is True only for
  `"loop-engineer"`.

## Stage → owner (fallback), with kind

| Stage | Owner | kind | Fallback | kind |
|-------|-------|------|----------|------|
| refine | `superpowers:brainstorming` | skill | `gsd-discuss-phase` | skill |
| plan | `gsd-planner` | **agent** | `superpowers:writing-plans` | skill |
| build | `superpowers:test-driven-development` | skill | `backend-developer` | **agent** |
| verify | `gsd-verify-work` | skill | `superpowers:verification-before-completion` | skill |
| review | `gsd-code-reviewer` | **agent** | `gstack:/review` | skill |
| qa | `gstack:/qa` | skill | — | — |
| security | `gstack:/cso` | skill | `security-auditor` | **agent** |
| ship | `gstack:/ship` | skill | — | — |

## Availability — the leading-segment rule
A route is *available* when the leading segment of its `name` (everything before the
first `:`) is in the session's `--available` set. So `gstack:/review` is available when
`gstack` is installed; `gsd-planner` when `gsd-planner` is. The session enumerates its own
installed Skill/Agent inventory into that set; when `--available` is omitted, each stage's
owner is assumed installed.

## Mandatory vs optional stages (the `next` dispatcher)
The session-level `next` state machine drives only the M2 stages, deriving each from
artifacts on disk. It splits them:

- **Mandatory — `plan`, `engine`, `review`.** These never skip. `engine` is the
  `loop-engineer run` step (build + verify happen inside it). If a mandatory skill/agent
  stage has neither owner nor fallback installed, `next` emits `ask_user` naming the stage
  and the skills to install — never a silent skip.
- **Optional — `qa`, `security`, `ship`.** When neither owner nor fallback is available
  (checkable only when `--available` is passed), `next` records an **explicit skip** in
  its ledger and names the stage in the terminal envelope's `skipped_stages`. Optional
  stages are skipped honestly, never silently dropped.

(`refine`, `build`, `verify` remain in the router table for the engine and for manual use;
the session dispatcher reaches `build`/`verify` only through the `engine` run.)

Assign exactly one owner per stage and silence the rest — this resolves the inventory's
conflict hotspots (4 code reviewers, 3 planners, 2 coordinators).

## One interviewer (M3)
Today `refine` is a SKILL.md precondition the session runs by hand before the loop. The
single **interview** stage that replaces it — one owner, one entry point, no competing
question-askers — lands in **2.0-M3**; `research` inserts in M4.
