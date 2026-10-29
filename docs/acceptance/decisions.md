# Acceptance decisions

One entry per row of [proof-binding.md](proof-binding.md). Each records what the engine enforces in `v3.5.0`,
including Meet's 2026-10-29 sign-off on [Open questions for Meet](#open-questions-for-meet).

## D1 candidate bytes

BOUND. `run_gates` binds a SHA-256 digest of the candidate's paths, bytes and write times (`GatesResult.candidate_digest`).
`promote` verifies `expected_digest` against the exact bytes read and re-checks every candidate path against `WritePolicy`
before writing, then the promoted workspace is gated again. Resolved in Q1 and Q2 (2026-10-29).

## D2 workspace identity

BOUND. Worktrees are detached at HEAD, seeded from the workspace's uncommitted files, and gated with that cwd.
A builder commit is rewound; a HEAD that moved elsewhere raises. Proof-of-test (`prove`) records the exact `refs/stash`
commit SHA immediately after `git stash push` and verifies `refs/stash` matches before `git stash pop`. Resolved in Q3 (2026-10-29).

## D3 gate command

BOUND. Config supplies the command; the allowlist, metacharacter ban and PATH-outside-workspace lookup fix what runs.
`extra_verify_prefixes` widens the allowlist, logs a warning, and is set only by whoever writes the config.

## D4 configuration

MUTABLE-ON-PURPOSE. Engine YAML is read once, outside the builder's reach. Project test config is ordinary
builder-writable source, reverted by proof-of-test as a non-test file (and `conftest.py` as test-support). Confirmed by Meet in Q4 (2026-10-29).

## D5 environment

MUTABLE-ON-PURPOSE. Gates and all three CLI providers (`claude_code`, `codex_cli`, `gemini_cli`) scrub every environment
variable matching `is_credential_env` (`SCRUBBED_ENV_KEYS` plus `(?i)(API_?KEY|TOKEN|SECRET|PASSWORD)$`). Confirmed and hardened in Q5 (2026-10-29).

## D6 dependencies

MUTABLE-ON-PURPOSE. Interpreter and packages come from the host PATH outside the workspace; the host toolchain is
accepted as trusted. Confirmed by Meet in Q6 (2026-10-29).

## D7 git config

BOUND. `WorktreeManager` records a deterministic SHA-256 digest of `.git/config` and all files under `.git/hooks/` when
creating a worktree, and `promote` refuses if either changed during the attempt. Resolved in Q7 (2026-10-29).

## D8 plugins

MUTABLE-ON-PURPOSE. Reviewer `claude_code` defaults to `isolated=True` in `build_orchestrator` (no session persistence,
strict MCP, disabled slash commands, project-only settings, fresh scratch cwd), while builders keep `isolated=False` by
default and pytest plugins autoload. Resolved in Q8 (2026-10-29).

## Open questions for Meet

All eight questions (Q1–Q8) were reviewed and resolved by Meet on 2026-10-29 (`v3.5.0`):

- **Q1** (**Resolved 2026-10-29 — Enforced**): `WorktreeManager.promote` now re-checks every candidate path against
  `WritePolicy.check_path` before copying, refusing any protected file (such as `.env` or `id_rsa`) created in the worktree after harvest.
- **Q2** (**Resolved 2026-10-29 — Enforced**): `run_gates` binds a SHA-256 `candidate_digest` and `select_winner`,
  `still_fresh`, and `WorktreeManager.promote(expected_digest=...)` refuse any candidate modified after its gate passed.
- **Q3** (**Resolved 2026-10-29 — Enforced**): `verifier.prove` captures the `refs/stash` SHA immediately after
  `git stash push` and verifies `refs/stash` matches before `git stash pop`, aborting the proof if the stash stack changed.
- **Q4** (**Resolved 2026-10-29 — Confirmed `MUTABLE-ON-PURPOSE`**): Outside fix mode, project test config (`pytest.ini`,
  `conftest.py`) remains builder-writable and is reverted during proof-of-test (`conftest.py` carries a `support_only` review note).
- **Q5** (**Resolved 2026-10-29 — Enforced**): `guardrails.is_credential_env` is now applied across `claude_code`,
  `codex_cli`, and `gemini_cli` so `GITHUB_TOKEN`, `AWS_SECRET_ACCESS_KEY`, `DB_PASSWORD`, and all matching credential vars are stripped.
- **Q6** (**Resolved 2026-10-29 — Confirmed `MUTABLE-ON-PURPOSE`**): The host toolchain and interpreter outside the
  workspace are accepted as trusted.
- **Q7** (**Resolved 2026-10-29 — Enforced**): `WorktreeManager` hashes `.git/config` and `.git/hooks/` at worktree
  creation and refuses `promote` if the shared git configuration or hooks were modified during the attempt.
- **Q8** (**Resolved 2026-10-29 — Enforced**): `build_orchestrator` now defaults reviewer `claude_code` to
  `isolated=True` while keeping builder `isolated=False` by default.

## Proof authority findings

Recorded by `tests/test_guardrail_proof_authority.py`; details in [proof-authority.md](proof-authority.md). Each fix
goes into its own scoped project; nothing here changes engine behavior.

- **CRITICAL FINDING PA-1** (attack A2): proof-of-test proves the gate depends on the source files being present, not
  on their behavior. A test that only checks `impl.py` exists makes wrong code proven, selected and promoted.
- **CRITICAL FINDING PA-2** (attacks B, D): gate writes are not confined to the worktree. The gate can write a success
  `pipeline.json` into the main workspace that `next` trusts, and can write any file the engine's user can.
  **Fixed on Windows** (2026-10-28): every gate runs as a low-integrity process that can write only inside its
  worktree and a private temp folder (`confine.run_confined`); a failed confinement step fails the gate. **Open on
  Linux and macOS**: there gates run unconfined, as the owner decided, and every gate's evidence says `unconfined`.
