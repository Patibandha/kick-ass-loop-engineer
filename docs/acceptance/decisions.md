# Acceptance decisions

One entry per row of [proof-binding.md](proof-binding.md). Each records what the engine enforces in `v3.5.0`,
and links the proposed answers to [Open questions for Meet](#open-questions-for-meet). None of Q1-Q8 has
Meet's recorded decision yet: every answer below is a proposal, awaiting Meet.

## D1 candidate bytes

BOUND. `run_gates` binds a SHA-256 digest of the candidate's paths, bytes and write times (`GatesResult.candidate_digest`).
`promote` verifies `expected_digest` against the exact bytes read and re-checks every candidate path against `WritePolicy`
before writing, then the promoted workspace is gated again. Proposed in Q1 and Q2, awaiting Meet.

## D2 workspace identity

BOUND. Worktrees are detached at HEAD, seeded from the workspace's uncommitted files, and gated with that cwd.
A builder commit is rewound; a HEAD that moved elsewhere raises. Proof-of-test (`prove`) records the exact `refs/stash`
commit SHA immediately after `git stash push` and verifies `refs/stash` matches before `git stash pop`. Proposed in Q3, awaiting Meet.

## D3 gate command

BOUND. Config supplies the command; the allowlist, metacharacter ban and PATH-outside-workspace lookup fix what runs.
`extra_verify_prefixes` widens the allowlist, logs a warning, and is set only by whoever writes the config.

## D4 configuration

MUTABLE-ON-PURPOSE. Engine YAML is read once, outside the builder's reach. Project test config is ordinary
builder-writable source, reverted by proof-of-test as a non-test file (and `conftest.py` as test-support). Proposed in Q4, awaiting Meet.

## D5 environment

MUTABLE-ON-PURPOSE. Gates and all three CLI providers (`claude_code`, `codex_cli`, `gemini_cli`) scrub every environment
variable matching `is_credential_env` (`SCRUBBED_ENV_KEYS` plus `(?i)(API_?KEY|TOKEN|SECRET|PASSWORD)$`). Proposed and implemented in Q5, awaiting Meet.

## D6 dependencies

MUTABLE-ON-PURPOSE. Interpreter and packages come from the host PATH outside the workspace; the host toolchain is
accepted as trusted. Proposed in Q6, awaiting Meet.

## D7 git config

PARTIAL. Engine git calls run with `core.fsmonitor=false`, an empty `core.hooksPath` and submodules ignored
(`workspace.git_argv`), but honour the rest of the repository's config. `WorktreeManager` records a deterministic SHA-256 digest of the common git dir's `config` and all files under
`hooks/` (resolved with `git rev-parse --git-common-dir`, so a linked-worktree workspace is covered) when creating a worktree, and `promote` refuses if either changed during the attempt. Proposed in Q7, awaiting Meet.

## D8 plugins

MUTABLE-ON-PURPOSE. Reviewer `claude_code` defaults to `isolated=True` in `build_orchestrator` (no session persistence,
strict MCP, disabled slash commands, project-only settings, fresh scratch cwd), while builders keep `isolated=False` by
default and pytest plugins autoload. Proposed in Q8, awaiting Meet.

## Open questions for Meet

Q1–Q8 are still open. Each entry says what this branch proposes; Meet has not decided any of them yet.

- **Q1** (**Proposed, awaiting Meet** — implemented): `WorktreeManager.promote` now re-checks every candidate path against
  `WritePolicy.check_path` before copying, refusing any protected file (such as `.env` or `id_rsa`) created in the worktree after harvest.
- **Q2** (**Proposed, awaiting Meet** — implemented): `run_gates` binds a SHA-256 `candidate_digest` and `select_winner`,
  `still_fresh`, and `WorktreeManager.promote(expected_digest=...)` refuse any candidate modified after its gate passed.
- **Q3** (**Proposed, awaiting Meet** — implemented): `verifier.prove` captures the `refs/stash` SHA immediately after
  `git stash push` and verifies `refs/stash` matches before `git stash pop`, aborting the proof if the stash stack changed.
- **Q4** (**Proposed, awaiting Meet** — keep `MUTABLE-ON-PURPOSE`, no code change): Outside fix mode, project test config (`pytest.ini`,
  `conftest.py`) remains builder-writable and is reverted during proof-of-test (`conftest.py` carries a `support_only` review note).
- **Q5** (**Proposed, awaiting Meet** — implemented): `guardrails.is_credential_env` is now applied across `claude_code`,
  `codex_cli`, and `gemini_cli` so `GITHUB_TOKEN`, `AWS_SECRET_ACCESS_KEY`, `DB_PASSWORD`, and all matching credential vars are stripped.
- **Q6** (**Proposed, awaiting Meet** — keep `MUTABLE-ON-PURPOSE`, no code change): The host toolchain and interpreter outside the
  workspace are accepted as trusted.
- **Q7** (**Proposed, awaiting Meet** — partly implemented, detection at promote only): `WorktreeManager` hashes `.git/config` and `.git/hooks/` at worktree
  creation and refuses `promote` if the shared git configuration or hooks were modified during the attempt.
- **Q8** (**Proposed, awaiting Meet** — implemented): `build_orchestrator` now defaults reviewer `claude_code` to
  `isolated=True` while keeping builder `isolated=False` by default.

## Proof authority findings

Recorded by `tests/test_guardrail_proof_authority.py`; details in [proof-authority.md](proof-authority.md). Each fix
goes into its own scoped project; nothing here changes engine behavior.

- **CRITICAL FINDING PA-1** (attack A2): proof-of-test proves the gate depends on the source files being present, not
  on their behavior. A test that only checks `impl.py` exists makes wrong code proven, selected and promoted.
- **CRITICAL FINDING PA-2** (attacks B, D): gate writes are not confined to the worktree. The gate can write a success
  `pipeline.json` into the main workspace that `next` trusts, and can write any file the engine's user can.
  **Fixed on Windows** (2026-10-28): every gate runs as a low-integrity process that can write only inside its
  worktree and a private temp folder (`confine.run_confined`); the final gate runs in a throwaway worktree, never
  in the workspace; a failed confinement step fails the gate. **Open on Linux and macOS**: there gates run
  unconfined (no sandbox ships in 3.5.0), and every gate's evidence says `unconfined`.
