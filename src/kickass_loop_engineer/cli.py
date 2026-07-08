"""Command-line interface for kickass_loop_engineer.

Commands:
    * ``init``   writes a starter config file.
    * ``build``  runs ONE guarded builder round (the unit the Claude Code skill
                 drives), printing a JSON result on stdout and a progress bar on
                 stderr. The orchestrating session owns the review/iterate loop.
    * ``verify`` runs an allowlisted verification command in the workspace and
                 reports pass/fail as JSON.
    * ``run``    runs the full verified pipeline (decompose → ensemble → gates →
                 cross-model review → promote), printing one JSON ``RunOutcome``.
    * ``refine`` assesses an idea spec across the six think-tank slots (purpose,
                 users, constraints, success_metrics, anti_goals, risks) and
                 writes ``SPEC.md`` / ``GOAL.md`` to the workspace, exiting 0
                 when ready or 3 when the spec needs more input (``--interview``
                 adds fixed follow-up questions to the not-ready output).
    * ``next``   emits ONE JSON envelope telling the session which skill/agent/
                 engine-command to dispatch next (the ``next`` dispatcher protocol).
    * ``research-ingest`` validates ``.loop-engineer/RESEARCH.md`` against the
                 provenance gate and records its ``## Decisions`` claims into the
                 workspace memory as ``kind="research"``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime

from .config import EXAMPLE_CONFIG, build_builder, build_orchestrator, load_config
from .guardrails import VerificationPolicy, WritePolicy
from .interview import SLOTS, next_questions
from .next import emit_next
from .objective import Objective
from .progress import ProgressReporter
from .promptwriter import IdeaSpec, PromptWriter
from .review import CrossModelReviewError
from .session import BuildSession
from .state import RunState
from .terminal import TerminalState
from .workspace import Workspace

def _read_feedback(args: argparse.Namespace) -> str:
    """Resolve feedback from an inline string or a file path."""
    if getattr(args, "feedback_file", ""):
        try:
            with open(args.feedback_file, "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError as exc:
            print(f"could not read feedback file: {exc}", file=sys.stderr)
            return ""
    return getattr(args, "feedback", "") or ""


def _cmd_init(args: argparse.Namespace) -> int:
    """Write a starter configuration file to the given path."""
    try:
        with open(args.path, "x", encoding="utf-8") as handle:
            handle.write(EXAMPLE_CONFIG)
    except FileExistsError:
        print(f"refusing to overwrite existing file: {args.path}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"failed to write config: {exc}", file=sys.stderr)
        return 1
    print(f"wrote starter config to {args.path}")
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    """Run one guarded builder round and emit a JSON result on stdout."""
    try:
        config = load_config(args.config) if args.config else {}
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1

    guard = config.get("guardrails", {})
    workspace = Workspace(
        root=args.workspace,
        policy=WritePolicy(
            max_file_bytes=int(guard.get("max_file_bytes", 1_000_000)),
            max_files_per_round=int(guard.get("max_files_per_round", 50)),
            allow_overwrite=bool(guard.get("allow_overwrite", True)),
        ),
    )
    objective = Objective(goal=args.objective, done_when=args.done_when, constraints=args.constraints)
    session = BuildSession(
        builder=build_builder(config),
        workspace=workspace,
        objective=objective,
        total_rounds=args.max_rounds,
        progress=ProgressReporter(args.max_rounds, events_path=f"{workspace.root}/.loop-engineer/events.jsonl"),
        state=RunState(workspace.root, {"goal": objective.goal, "done_when": objective.done_when}),
    )

    try:
        result = session.build_round(args.round, _read_feedback(args))
    except Exception as exc:  # surface any provider failure as structured output
        print(json.dumps({"error": f"build failed: {exc}", "round_no": args.round}))
        return 1

    payload = result.to_dict()
    payload.pop("builder_text", None)
    payload["state_md"] = session.state.state_md
    payload["events"] = session.state.events_path
    print(json.dumps(payload, indent=2))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    """Run a named gate and emit an evidence record as JSON."""
    from .verifier import run_gate
    record = run_gate(args.gate, args.cmd, workspace=args.workspace, with_proof=args.prove)
    print(json.dumps(record.to_dict(), indent=2))
    return 0 if record.passed else 2


def _cmd_run(args: argparse.Namespace) -> int:
    """Run the full verified pipeline and print the RunOutcome as JSON."""
    try:
        config = load_config(args.config) if args.config else {}
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    objective = Objective(goal=args.objective, done_when=args.done_when, constraints=args.constraints)
    try:
        orchestrator = build_orchestrator(
            config, workspace=args.workspace, month=datetime.now().strftime("%Y-%m"),
            objective=objective, run_id=args.run_id,
        )
    except CrossModelReviewError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    outcome = orchestrator.run()
    print(json.dumps(outcome.to_dict(), indent=2))
    return 0 if outcome.state is TerminalState.SUCCESS else 2


def _cmd_refine(args: argparse.Namespace) -> int:
    """Assess an IdeaSpec through the six-slot gate; write SPEC.md/GOAL.md when ready.

    Not ready → ``{"ready": false, "gaps": [...]}`` (plus ``"questions": [...]``
    with ``--interview``), exit 3. Ready → writes the documents, exit 0. An
    unknown slot name in ``--defer`` short-circuits with ``{"error": "..."}``,
    exit 1, before assessment ever runs.

    Args:
        args: Parsed CLI arguments from the ``refine`` subparser.

    Returns:
        0 on success, 1 on an unknown ``--defer`` slot, 3 when the spec is not
        ready.
    """
    spec = IdeaSpec(
        build=args.build,
        done_when=args.done_when,
        purpose=args.purpose,
        users=args.users,
        constraints=args.constraints,
        success_metrics=args.success_metric or [],
        anti_goals=args.anti_goals or args.exclude,
        risks=args.risks,
        consider=args.consider,
        deferred=[s.strip() for s in args.defer.split(",") if s.strip()],
    )
    unknown = [s for s in spec.deferred if s not in SLOTS]
    if unknown:
        print(json.dumps({"error": f"unknown slot(s) in --defer: {unknown}; known: {list(SLOTS)}"}))
        return 1
    pw = PromptWriter()
    res = pw.assess(spec)
    if not res.ready:
        payload = {"ready": False, "gaps": res.gaps}
        if args.interview:
            payload["questions"] = next_questions(spec)
        print(json.dumps(payload, indent=2))
        return 3
    paths = pw.write(spec, args.workspace)
    print(json.dumps({"ready": True, **paths}))
    return 0


def _cmd_next(args: argparse.Namespace) -> int:
    """Emit the next-action envelope for the session dispatcher loop.

    Always returns 0 — state lives in the envelope, not the exit code.
    """
    available = ({s.strip() for s in args.available.split(",") if s.strip()}
                 if args.available else None)
    envelope = emit_next(args.workspace, available=available, objective=args.objective)
    print(json.dumps(envelope, indent=2))
    return 0


def _cmd_research_ingest(args: argparse.Namespace) -> int:
    """Validate <workspace>/.loop-engineer/RESEARCH.md and record its decisions.

    Gate failure → ``{"error": ...}`` exit 2; success → ``{"ingested": N}`` exit 0.
    """
    from .memory import Memory
    from .research import check_tag_coverage, ingest_research
    path = os.path.join(args.workspace, ".loop-engineer", "RESEARCH.md")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            research_md = handle.read()
    except OSError as exc:
        print(json.dumps({"error": f"cannot read RESEARCH.md: {exc}"}))
        return 2
    ok, reason = check_tag_coverage(research_md)
    if not ok:
        print(json.dumps({"error": reason}))
        return 2
    db = os.path.join(args.workspace, ".loop-engineer", "memory.db")
    with Memory(db) as mem:
        n = ingest_research(research_md, mem, run_id=args.run_id or "research-ingest")
    print(json.dumps({"ingested": n}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for the CLI."""
    parser = argparse.ArgumentParser(prog="loop-engineer", description="Iterative multi-agent build/review loop")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write a starter config file")
    init.add_argument("--path", default="loop-engineer.yaml", help="config output path")
    init.set_defaults(func=_cmd_init)

    build = sub.add_parser("build", help="run one guarded builder round (JSON out)")
    build.add_argument("--objective", required=True, help="what to build or accomplish")
    build.add_argument("--done-when", required=True, dest="done_when", help="completion criteria")
    build.add_argument("--constraints", default="", help="optional rules for the builder")
    build.add_argument("--workspace", default="./workspace", help="output directory")
    build.add_argument("--config", default="", help="path to a loop-engineer.yaml config")
    build.add_argument("--round", type=int, default=1, help="round number (orchestrator-managed)")
    build.add_argument("--max-rounds", type=int, default=5, dest="max_rounds", help="max rounds for progress")
    build.add_argument("--feedback", default="", help="reviewer feedback for this round")
    build.add_argument("--feedback-file", default="", dest="feedback_file", help="path to feedback text")
    build.set_defaults(func=_cmd_build)

    verify = sub.add_parser("verify", help="run a named gate and emit an evidence record")
    verify.add_argument("--gate", default="unit",
                        help="gate name (unit|security|data_leak|performance|smoke)")
    verify.add_argument("--cmd", required=True, help="allowlisted gate command (e.g. 'pytest -q')")
    verify.add_argument("--prove", action="store_true",
                        help="require proof-of-test (revert->red->green)")
    verify.add_argument("--workspace", default="./workspace", help="git workspace to run in")
    verify.set_defaults(func=_cmd_verify)

    run = sub.add_parser("run", help="run the full verified pipeline")
    run.add_argument("--objective", required=True, help="what to build or accomplish")
    run.add_argument("--done-when", required=True, dest="done_when", help="completion criteria")
    run.add_argument("--constraints", default="", help="optional rules for the builder")
    run.add_argument("--workspace", default="./workspace", help="output directory")
    run.add_argument("--config", default="", help="path to a loop-engineer.yaml config")
    run.add_argument("--run-id", default="", dest="run_id",
                     help="resume/stable run id (auto-generated when empty)")
    run.set_defaults(func=_cmd_run)

    refine = sub.add_parser("refine", help="assess an idea spec and write SPEC.md / GOAL.md")
    refine.add_argument("--build", required=True, help="description of what to build")
    refine.add_argument("--done-when", default="", dest="done_when", help="measurable completion criterion")
    refine.add_argument("--purpose", default="", help="the job this does and the outcome it produces")
    refine.add_argument("--users", default="", help="who (or what) uses it")
    refine.add_argument("--constraints", default="", help="hard constraints the build must respect")
    refine.add_argument("--success-metric", default=None, action="append", dest="success_metric",
                        help="repeatable; each becomes a verifier check")
    refine.add_argument("--anti-goals", default="", dest="anti_goals", help="explicit non-goals")
    refine.add_argument("--exclude", default="", help="deprecated alias for --anti-goals")
    refine.add_argument("--risks", default="", help="known unknowns / what could go wrong")
    refine.add_argument("--consider", default="", help="additional considerations or constraints")
    refine.add_argument("--defer", default="", help="comma-separated slot names to explicitly defer")
    refine.add_argument("--interview", action="store_true",
                        help="emit the fixed interview questions for every unfilled slot")
    refine.add_argument("--workspace", default="./workspace", help="directory to write SPEC.md / GOAL.md into")
    refine.set_defaults(func=_cmd_refine)

    next_ = sub.add_parser("next", help="emit the next-action envelope for the session dispatcher loop")
    next_.add_argument("--workspace", default="./workspace", help="workspace directory to read artifacts from")
    next_.add_argument("--available", default="",
                       help="comma-separated installed skill/agent name prefixes the session "
                            "enumerated; empty = assume stage owners are installed")
    next_.add_argument("--objective", default="", help="objective goal passed through to dispatch args")
    next_.set_defaults(func=_cmd_next)

    research_ingest = sub.add_parser(
        "research-ingest",
        help="validate RESEARCH.md against the provenance gate and record its decisions")
    research_ingest.add_argument("--workspace", default="./workspace",
                                 help="workspace directory holding .loop-engineer/RESEARCH.md")
    research_ingest.add_argument("--run-id", default="", dest="run_id",
                                 help="run id to tag recorded memory facts with")
    research_ingest.set_defaults(func=_cmd_research_ingest)

    return parser


def main(argv: list = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument list; defaults to ``sys.argv``.

    Returns:
        A process exit code.
    """
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
