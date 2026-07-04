"""brigade outcome command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    p_outcome = sub.add_parser("outcome", help="Inspect the verified outcome ledger.")
    outcome_sub = p_outcome.add_subparsers(dest="outcome_command", metavar="<outcome-command>")
    outcome_sub.required = True

    p_score = outcome_sub.add_parser("score", help="Show verified outcome scores for learned cards and skills.")
    p_score.add_argument("artifact_id", nargs="?", default=None, help="Limit to a single artifact id.")
    p_score.add_argument("--target", "-t", type=Path, default=Path("."))
    p_score.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text.")
    p_score.set_defaults(func=_dispatch_score)

    p_explain = outcome_sub.add_parser("explain", help="Show the per-signal trail behind an artifact's score.")
    p_explain.add_argument("artifact_id", help="Artifact id (card or skill) to explain.")
    p_explain.add_argument("--target", "-t", type=Path, default=Path("."))
    p_explain.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text.")
    p_explain.set_defaults(func=_dispatch_explain)

    p_capture = outcome_sub.add_parser("capture", help="Record a verify run's outcome for a learned artifact.")
    p_capture.add_argument("artifact_id", help="Artifact id (card or skill) the run exercised.")
    p_capture.add_argument("--kind", default="skill", choices=["skill", "card"], help="Artifact kind.")
    p_capture.add_argument("--task-id", default=None, help="Task id to correlate the signal with.")
    p_capture.add_argument("--run-id", default="latest", help="Verify run id to read (default: latest).")
    p_capture.add_argument("--target", "-t", type=Path, default=Path("."))
    p_capture.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text.")
    p_capture.set_defaults(func=_dispatch_capture)

    p_reconcile = outcome_sub.add_parser(
        "reconcile", help="Apply verified promote/rollback decisions (dry-run by default)."
    )
    p_reconcile.add_argument(
        "--apply", action="store_true", help="Write decisions and advance status (default: dry-run)."
    )
    p_reconcile.add_argument("--target", "-t", type=Path, default=Path("."))
    p_reconcile.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text.")
    p_reconcile.set_defaults(func=_dispatch_reconcile)

    p_rank = outcome_sub.add_parser("rank", help="Rank learned skills by verified outcome, most-proven first.")
    p_rank.add_argument("--target", "-t", type=Path, default=Path("."))
    p_rank.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text.")
    p_rank.set_defaults(func=_dispatch_rank)

    p_rebuild = outcome_sub.add_parser(
        "rebuild-status", help="Rebuild status.json from decision receipts and report any drift."
    )
    p_rebuild.add_argument(
        "--check", action="store_true", help="Exit non-zero if the persisted status.json has drifted."
    )
    p_rebuild.add_argument("--target", "-t", type=Path, default=Path("."))
    p_rebuild.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text.")
    p_rebuild.set_defaults(func=_dispatch_rebuild_status)

    p_fork = outcome_sub.add_parser(
        "fork", help="Project what the ratchet would decide under a hypothetical config (read-only)."
    )
    p_fork.add_argument("--out", type=Path, required=True, help="Path to write the fork projection JSON.")
    p_fork.add_argument("--install-min-helped", type=int, default=None, help="Override install_min_helped.")
    p_fork.add_argument("--revert-min-hurt", type=int, default=None, help="Override revert_min_hurt.")
    p_fork.add_argument("--bump-min-helped", type=int, default=None, help="Override bump_min_helped.")
    p_fork.add_argument("--z", type=float, default=None, help="Override the Wilson z score.")
    p_fork.add_argument("--target", "-t", type=Path, default=Path("."))
    p_fork.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text.")
    p_fork.set_defaults(func=_dispatch_fork)

    p_diff = outcome_sub.add_parser("diff", help="Compare two outcome fork projections.")
    p_diff.add_argument("fork_a", type=Path, help="First fork projection JSON.")
    p_diff.add_argument("fork_b", type=Path, help="Second fork projection JSON.")
    p_diff.add_argument("--target", "-t", type=Path, default=Path("."))
    p_diff.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text.")
    p_diff.set_defaults(func=_dispatch_diff)

    p_record = outcome_sub.add_parser("record", help="Record an explicit (non-verify) outcome signal for an artifact.")
    p_record.add_argument("artifact_id", help="Artifact id (card or skill) the signal is about.")
    p_record.add_argument("--source", required=True, help="Signal source, e.g. friction or learnings.")
    p_record.add_argument("--status", required=True, help="Signal status, e.g. cleared or recurred.")
    p_record.add_argument("--evidence", default="", help="Reference to the evidence (path, scan id, ...).")
    p_record.add_argument("--kind", default="skill", choices=["skill", "card"], help="Artifact kind.")
    p_record.add_argument("--task-id", default=None, help="Task id to correlate the signal with.")
    p_record.add_argument("--target", "-t", type=Path, default=Path("."))
    p_record.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text.")
    p_record.set_defaults(func=_dispatch_record)


def _dispatch_score(args) -> int:
    from .. import outcome_cmd

    return outcome_cmd.score(target=args.target, artifact_id=args.artifact_id, json_output=args.json)


def _dispatch_explain(args) -> int:
    from .. import outcome_cmd

    return outcome_cmd.explain(target=args.target, artifact_id=args.artifact_id, json_output=args.json)


def _dispatch_capture(args) -> int:
    from .. import outcome_cmd

    return outcome_cmd.capture(
        target=args.target,
        artifact_id=args.artifact_id,
        artifact_kind=args.kind,
        task_id=args.task_id,
        run_id=args.run_id,
        json_output=args.json,
    )


def _dispatch_reconcile(args) -> int:
    from .. import outcome_cmd

    return outcome_cmd.reconcile(target=args.target, apply=args.apply, json_output=args.json)


def _dispatch_rank(args) -> int:
    from .. import outcome_cmd

    return outcome_cmd.rank(target=args.target, json_output=args.json)


def _dispatch_rebuild_status(args) -> int:
    from .. import outcome_cmd

    return outcome_cmd.rebuild_status(target=args.target, check=args.check, json_output=args.json)


def _dispatch_fork(args) -> int:
    from .. import outcome, outcome_cmd

    defaults = outcome.ReconcileConfig()
    config = outcome.ReconcileConfig(
        install_min_helped=args.install_min_helped
        if args.install_min_helped is not None
        else defaults.install_min_helped,
        revert_min_hurt=args.revert_min_hurt if args.revert_min_hurt is not None else defaults.revert_min_hurt,
        bump_min_helped=args.bump_min_helped if args.bump_min_helped is not None else defaults.bump_min_helped,
        cooldown_seconds=defaults.cooldown_seconds,
        z=args.z if args.z is not None else defaults.z,
    )
    return outcome_cmd.fork(target=args.target, out=args.out, config=config, json_output=args.json)


def _dispatch_diff(args) -> int:
    from .. import outcome_cmd

    return outcome_cmd.diff(target=args.target, fork_a=args.fork_a, fork_b=args.fork_b, json_output=args.json)


def _dispatch_record(args) -> int:
    from .. import outcome_cmd

    return outcome_cmd.record(
        target=args.target,
        artifact_id=args.artifact_id,
        source=args.source,
        status=args.status,
        evidence_ref=args.evidence,
        artifact_kind=args.kind,
        task_id=args.task_id,
        json_output=args.json,
    )
