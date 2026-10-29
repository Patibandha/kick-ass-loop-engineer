# Proof binding inventory

What a gate pass is bound to today, read from the code (`v3.5.0`).

- **BOUND**: the engine fixes the item in code, so a candidate cannot change it between the gate run and promotion.
- **MUTABLE-ON-PURPOSE**: nothing pins the item today and the design relies on it varying (confirmed by Meet, 2026-10-29).

Every row links to its entry in [decisions.md](decisions.md). Authority per actor: [authority-map.md](authority-map.md).

| Item | Status | Evidence (src file and function) | Decision |
|---|---|---|---|
| Candidate bytes | BOUND | `run_gates` binds a SHA-256 digest of candidate paths, bytes and mtimes (`src/kickass_loop_engineer/verifier.py::run_gates`, `src/kickass_loop_engineer/workspace.py::candidate_digest`); `promote` verifies `expected_digest` and re-checks `WritePolicy` (`src/kickass_loop_engineer/worktree.py::promote`, `src/kickass_loop_engineer/guardrails.py::check_path`); the promoted tree is gated again (`src/kickass_loop_engineer/orchestrator.py::_process_slices`). | [D1](decisions.md#d1-candidate-bytes) |
| Workspace identity | BOUND | Each attempt gets a detached worktree at HEAD (`src/kickass_loop_engineer/worktree.py::create`); a moved HEAD is refused (`src/kickass_loop_engineer/workspace.py::uncommit_to`); paths are link-resolved (`src/kickass_loop_engineer/workspace.py::contained`); `prove` records the `refs/stash` SHA and verifies it before popping (`src/kickass_loop_engineer/verifier.py::prove`). | [D2](decisions.md#d2-workspace-identity) |
| Gate command | BOUND | The command comes from config, is allowlist-checked with no shell metacharacters (`src/kickass_loop_engineer/guardrails.py::validate`), and its program resolves only outside the workspace (`src/kickass_loop_engineer/guardrails.py::_resolve_program`). | [D3](decisions.md#d3-gate-command) |
| Configuration | MUTABLE-ON-PURPOSE | Engine YAML is read once (`src/kickass_loop_engineer/config.py::load_config`). Project test config (pytest.ini, pyproject.toml, conftest.py) is builder-writable and reverted by proof-of-test; fix-mode repro neutralizes it (`src/kickass_loop_engineer/reproduce.py::repro_check_command`). | [D4](decisions.md#d4-configuration) |
| Environment | MUTABLE-ON-PURPOSE | Inherited from the engine process minus all credentials matching `is_credential_env`, plus `PYTHONPATH` and a fresh bytecode cache (`src/kickass_loop_engineer/guardrails.py::_gate_environment`). All CLI providers apply the same full credential scrub (`src/kickass_loop_engineer/providers/claude_code.py::_run`, `src/kickass_loop_engineer/providers/codex_cli.py::_run`, `src/kickass_loop_engineer/providers/gemini_cli.py::_run`). | [D5](decisions.md#d5-environment) |
| Dependencies | MUTABLE-ON-PURPOSE | Resolved from the host interpreter and PATH outside the workspace; the host toolchain is trusted by design (`src/kickass_loop_engineer/guardrails.py::_resolve_program`). | [D6](decisions.md#d6-dependencies) |
| Git config | BOUND | `WorktreeManager` records a SHA-256 digest of `.git/config` and `.git/hooks/` at worktree creation and refuses `promote` if either was modified during the attempt (`src/kickass_loop_engineer/worktree.py::_git_config_digest`, `src/kickass_loop_engineer/worktree.py::promote`). | [D7](decisions.md#d7-git-config) |
| Plugins | MUTABLE-ON-PURPOSE | Reviewer `claude_code` defaults to `isolated=True` (`src/kickass_loop_engineer/config.py::build_orchestrator`, `src/kickass_loop_engineer/providers/claude_code.py::complete`); builder CLIs default to `isolated=False` and pytest plugins autoload. | [D8](decisions.md#d8-plugins) |

Sign-off rule: every row is one of the two statuses above, and every row links to an entry that exists.
Checked by `tests/test_acceptance_docs.py`.
