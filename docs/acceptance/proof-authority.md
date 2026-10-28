# Proof authority

Can code the builder controls, running as the gate, change the proof or what the engine trusts? Each attack is a
test module in the candidate, run as the real gate in a real attempt worktree through `run_gates` (with
proof-of-test), `select_winner` and `promote`. Tested by `tests/test_guardrail_proof_authority.py`, which also
checks every outcome below against what its test observed.

- **REFUSED**: the attempt is not proven, not selected or not promoted.
- **ACCEPTED**: the attack's effect stands. Each one is a CRITICAL FINDING, recorded, not fixed here.
- **REFUSED (Windows; unconfined elsewhere)**: refused where the host confines gate processes (Windows); on Linux and
  macOS the gate runs as before, the attack is ACCEPTED and the gate's evidence ends `; unconfined`.

| Attack | Outcome | Decided by |
|---|---|---|
| A1 | REFUSED | Reverted run writes the source back so the gate stays green: `red_when_reverted` is False and the pop collides, so the proof fails (`src/kickass_loop_engineer/verifier.py::prove`). |
| A2 | ACCEPTED | Test fails only when the source file is absent and never calls it: the proof only sees the verdict flip on revert, so wrong code is proven, selected and promoted (`src/kickass_loop_engineer/verifier.py::prove`). PA-1. |
| B | REFUSED (Windows; unconfined elsewhere) | Gate writes a success `pipeline.json` into the main workspace. A confined gate runs at low integrity and cannot write there, so the gate fails and the engine loads no cursor (`src/kickass_loop_engineer/confine.py::run_confined`); unconfined, the engine loads it and moves on (`src/kickass_loop_engineer/cursor.py::load`, `src/kickass_loop_engineer/next.py::emit_next`). PA-2. |
| C | REFUSED | A background process left by the gate changes the candidate after its digest: the binding no longer matches (`src/kickass_loop_engineer/verifier.py::still_fresh`, `src/kickass_loop_engineer/worktree.py::promote`). |
| D | REFUSED (Windows; unconfined elsewhere) | Gate writes into the main workspace and an engine-source folder. A confined gate can write only inside its worktree and a private temp folder, so both writes fail and so does the gate (`src/kickass_loop_engineer/confine.py::run_confined`, called by `src/kickass_loop_engineer/guardrails.py::run`); unconfined, nothing stops either write. PA-2. |

## CRITICAL FINDING PA-1

Proof-of-test proves the gate's verdict depends on the source files being present, not on what they do. A test
that checks only that `impl.py` exists turns red on revert, so a wrong `impl.py` is proven and promoted. Only the
cross-model review stands between it and the workspace. Fix in its own scoped project.

## CRITICAL FINDING PA-2

A gate runs as the engine's user with the attempt worktree as its cwd and nothing more. It can find the main
workspace from the worktree's `.git` file and write there: run state the engine later reads (attack B; while a
run lasts the orchestrator's next cursor write replaces it, but a process the gate leaves behind can write after)
and arbitrary files, including the engine's own source when it is writable (attack D).

**Fixed on Windows, open on Linux and macOS.** On Windows every gate command runs as a separate low-integrity
process (`confine.run_confined`): it can write only inside its attempt worktree and a private temp folder, and the
worktree's own `.loop-engineer` keeps a medium label so run state stays out of reach there too. A confinement step
that fails fails the gate; it never falls back to an unconfined run. On a host without a confinement mechanism the
gate runs as before and every gate result says `unconfined` in its evidence and in the run report. Network access
is unchanged.
