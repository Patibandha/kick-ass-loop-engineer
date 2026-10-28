# Proof binding inventory

What a gate pass is bound to today, read from the code (no engine change in this milestone).

- **BOUND**: the engine fixes the item in code, so a candidate cannot change it between the gate run and promotion.
- **MUTABLE-ON-PURPOSE**: nothing pins the item today and the design relies on it varying. The status records what the
  code does, not that Meet has endorsed it; where the behavior looks unintended the entry carries an open question.

Every row links to its entry in [decisions.md](decisions.md). Authority per actor: [authority-map.md](authority-map.md).

| Item | Status | Evidence (src file and function) | Decision |
|---|---|---|---|
| Candidate bytes | BOUND | The gate runs in the attempt worktree and `promote` copies that same tree (`src/kickass_loop_engineer/verifier.py::run_gates`, `src/kickass_loop_engineer/worktree.py::promote`); the promoted tree is gated again (`src/kickass_loop_engineer/orchestrator.py::_process_slices`). No digest is recorded. | [D1](decisions.md#d1-candidate-bytes) |
| Workspace identity | BOUND | Each attempt gets a detached worktree at HEAD (`src/kickass_loop_engineer/worktree.py::create`); the gate cwd is that worktree; a moved HEAD is refused (`src/kickass_loop_engineer/workspace.py::uncommit_to`); paths are link-resolved (`src/kickass_loop_engineer/workspace.py::contained`). | [D2](decisions.md#d2-workspace-identity) |
| Gate command | BOUND | The command comes from config, is allowlist-checked with no shell metacharacters (`src/kickass_loop_engineer/guardrails.py::validate`), and its program resolves only outside the workspace (`src/kickass_loop_engineer/guardrails.py::_resolve_program`). | [D3](decisions.md#d3-gate-command) |
| Configuration | MUTABLE-ON-PURPOSE | Engine YAML is read once (`src/kickass_loop_engineer/config.py::load_config`). Project test config (pytest.ini, pyproject.toml, conftest.py) is builder-writable and only the fix-mode repro neutralizes it (`src/kickass_loop_engineer/reproduce.py::repro_check_command`). | [D4](decisions.md#d4-configuration) |
| Environment | MUTABLE-ON-PURPOSE | Inherited from the engine process, minus credentials, plus `PYTHONPATH` and a fresh bytecode cache (`src/kickass_loop_engineer/guardrails.py::_gate_environment`). Builder CLIs scrub less (`src/kickass_loop_engineer/providers/claude_code.py::_run`). | [D5](decisions.md#d5-environment) |
| Dependencies | MUTABLE-ON-PURPOSE | Resolved from the host interpreter and PATH; no lockfile or hash check exists (`src/kickass_loop_engineer/guardrails.py::_resolve_program`). | [D6](decisions.md#d6-dependencies) |
| Git config | MUTABLE-ON-PURPOSE | Git runs with ambient config and no override flags (`src/kickass_loop_engineer/verifier.py::_git`, `src/kickass_loop_engineer/worktree.py::_git`). | [D7](decisions.md#d7-git-config) |
| Plugins | MUTABLE-ON-PURPOSE | Builder and reviewer CLIs use the caller's profile unless `isolated` or `profile_dir` is set (`src/kickass_loop_engineer/providers/claude_code.py::complete`); pytest plugins autoload. | [D8](decisions.md#d8-plugins) |

Sign-off rule: every row is one of the two statuses above, and every row links to an entry that exists.
Checked by `tests/test_acceptance_docs.py`.
