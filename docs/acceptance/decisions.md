# Acceptance decisions

One entry per row of [proof-binding.md](proof-binding.md). Each records what the engine does today, read from the
code. Nothing here changes engine behavior; anything that looks unintended is under
[Open questions for Meet](#open-questions-for-meet), not decided here.

## D1 candidate bytes

BOUND. The gate runs in the attempt worktree, the reviewer snapshot is read from it, and `promote` copies that tree.
The promoted workspace is then gated again. By sequencing, not by hash: no digest of the gated bytes is recorded. See Q1, Q2.

## D2 workspace identity

BOUND. Worktrees are detached at HEAD, seeded from the workspace's uncommitted files, and gated with that cwd.
A builder commit is rewound; a HEAD that moved elsewhere raises. No worktree path or HEAD sha is stored in evidence. See Q3.

## D3 gate command

BOUND. Config supplies the command; the allowlist, metacharacter ban and PATH-outside-workspace lookup fix what runs.
`extra_verify_prefixes` widens the allowlist, logs a warning, and is set only by whoever writes the config.

## D4 configuration

MUTABLE-ON-PURPOSE. Engine YAML is read once, outside the builder's reach. Project test config is ordinary
builder-writable source, reverted by proof-of-test as a non-test file. See Q4.

## D5 environment

MUTABLE-ON-PURPOSE. A gate sees the engine's own environment minus credentials. All three proof runs share one
process, so they see the same one, but it is not pinned or recorded. See Q5.

## D6 dependencies

MUTABLE-ON-PURPOSE. Interpreter and packages come from the host; the engine neither locks nor hashes them. See Q6.

## D7 git config

MUTABLE-ON-PURPOSE. Every git call uses ambient user, system and repository config. See Q7.

## D8 plugins

MUTABLE-ON-PURPOSE. Isolation for Claude CLI roles is opt-in (`isolated`, `profile_dir`); pytest plugins autoload. See Q8.

## Open questions for Meet

Each is current behavior that looks unintended. Classified truthfully above and left undecided here.

- **Q1** `promote` applies no `WritePolicy`; harvest checked the builder's files before the gates ran. A file a test
  run creates in the worktree (say `.env` or `x.pem`) is copied into your workspace. Re-check at promote?
- **Q2** Evidence carries no digest of the gated bytes. Hashes exist only in `Workspace.fingerprint`, for the harvest
  diff. Record one at gate time and compare it at promote?
- **Q3** Proof-of-test runs `git stash push` then a bare `git stash pop`. The stash stack is shared by all worktrees of a
  repo, so a concurrent stash could be popped by mistake. Capture and pop by SHA?
- **Q4** Outside fix mode, a builder-written pytest.ini or conftest.py shapes the gate. Only the fix-mode repro gate
  disables them (`--noconftest -o addopts=`). Extend that to every gate?
- **Q5** `SCRUBBED_ENV_KEYS` says credentials are not forwarded to "a builder CLI", but the Claude, Codex and Gemini
  providers strip only a few named keys. A Bash-capable builder still sees `GITHUB_TOKEN` and similar. Apply the gate's scrub?
- **Q6** A Bash-capable builder can install packages into the interpreter the gates use, and nothing detects it.
  Pin dependencies, or accept the host toolchain as trusted?
- **Q7** `.git/config` and hooks sit in the common git dir shared by all worktrees. Harvest only reads `git status`
  paths, so a builder edit there is never seen or reverted. Run git with config isolation?
- **Q8** `claude_code` defaults to `isolated: false` and no profile, so builder and reviewer load your plugins, skills,
  MCP servers and CLAUDE.md, and a non-isolated reviewer starts in the engine's cwd. Make isolation the default?
