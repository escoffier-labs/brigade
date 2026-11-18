"""brigade t3 command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def _run_arguments(parser: argparse.ArgumentParser, *, task_noun: str = "Task") -> None:
    parser.add_argument("run_id", help="Run identity reported by `brigade t3 open`.")
    parser.add_argument("task_id", help=f"{task_noun} identity inside that run.")
    parser.add_argument(
        "--target",
        "-t",
        type=Path,
        default=Path("."),
        help="Workspace that owns the run. Defaults to the current directory.",
    )


def register(sub: argparse._SubParsersAction) -> None:
    p_t3 = sub.add_parser("t3", help="Enroll and govern native T3 delegation for a parent session.")
    t3_sub = p_t3.add_subparsers(dest="t3_command", metavar="<t3-command>")
    t3_sub.required = True
    p_open = t3_sub.add_parser(
        "open",
        help="Enroll a validated batch for a T3 parent to delegate. Never launches work.",
    )
    p_open.add_argument("batch_file", type=Path, help="Versioned brigade.run_batch.v1 batch JSON file.")
    p_open.add_argument(
        "--target",
        "-t",
        type=Path,
        default=Path("."),
        help="Workspace to enroll against. Its clean HEAD becomes the source revision.",
    )
    p_open.add_argument(
        "--roster",
        type=Path,
        default=None,
        help="Roster TOML path. Defaults to the roster that applies to --target.",
    )
    p_claim = t3_sub.add_parser(
        "claim",
        help="Claim one launch and print the exact delegate_task arguments once. Never calls T3.",
    )
    _run_arguments(p_claim)
    p_claim.add_argument(
        "--parent-thread",
        required=True,
        help="Caller-declared T3 parent thread. Recorded as a declaration, not as proof.",
    )
    p_accepted = t3_sub.add_parser(
        "accepted",
        help="Record a returned delegate_task result as parent-reported correlation.",
    )
    _run_arguments(p_accepted)
    p_accepted.add_argument("--parent-thread", required=True, help="Same declared parent thread as the claim.")
    p_accepted.add_argument(
        "--native-json",
        required=True,
        help="Delegate result projection as a JSON file, or `-` for standard input.",
    )
    p_hold = t3_sub.add_parser(
        "hold-resolve",
        help="Resolve a held task by operator assertion or explicit abandon. Never re-grants launch.",
    )
    _run_arguments(p_hold)
    group = p_hold.add_mutually_exclusive_group(required=True)
    group.add_argument("--native-json", help="Delegate result projection as a JSON file, or `-` for standard input.")
    group.add_argument(
        "--abandon",
        action="store_true",
        help="Mark the attempt terminal-unknown. The dispatch unit stays charged.",
    )
    p_observe = t3_sub.add_parser(
        "observe",
        help="Record a status projection for an already-correlated task. Never creates a correlation.",
    )
    _run_arguments(p_observe)
    p_observe.add_argument("--parent-thread", required=True, help="Same declared parent thread as the claim.")
    p_observe.add_argument(
        "--native-json",
        required=True,
        help="task_status result projection as a JSON file, or `-` for standard input.",
    )
    p_cancel = t3_sub.add_parser(
        "cancel",
        help="Record a digests-only cancel request and print the task_cancel arguments.",
    )
    _run_arguments(p_cancel)
    p_cancel.add_argument("--parent-thread", required=True, help="Same declared parent thread as the claim.")
    p_ack = t3_sub.add_parser(
        "ack",
        help="Acknowledge one exact terminal result. Idempotent and safe after finalization.",
    )
    _run_arguments(p_ack)
    p_ack.add_argument("--result-id", required=True, help="Terminal result identity from `t3 observe`.")
    p_finalize = t3_sub.add_parser(
        "finalize",
        help="Explicitly close the run: completed only when every task completed and was acknowledged.",
    )
    p_finalize.add_argument("run_id", help="Run identity reported by `brigade t3 open`.")
    p_finalize.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace that owns the run.")
    p_status = t3_sub.add_parser(
        "status",
        help="Read-only per-task state tokens without task text, options values or child ids.",
    )
    p_status.add_argument("run_id", help="Run identity reported by `brigade t3 open`.")
    p_status.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace that owns the run.")
    p_status.add_argument("--json", action="store_true", help="Print the machine-readable report.")
    p_t3.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import native_t3_dispatch, native_t3_enrollment, native_t3_lifecycle

    if args.t3_command == "open":
        return native_t3_enrollment.open_cli(args)
    if args.t3_command == "claim":
        return native_t3_dispatch.claim_cli(args)
    if args.t3_command == "accepted":
        return native_t3_dispatch.accepted_cli(args)
    if args.t3_command == "hold-resolve":
        return native_t3_dispatch.hold_resolve_cli(args)
    if args.t3_command == "observe":
        return native_t3_lifecycle.observe_cli(args)
    if args.t3_command == "cancel":
        return native_t3_lifecycle.cancel_cli(args)
    if args.t3_command == "ack":
        return native_t3_lifecycle.ack_cli(args)
    if args.t3_command == "finalize":
        return native_t3_lifecycle.finalize_cli(args)
    if args.t3_command == "status":
        return native_t3_lifecycle.status_cli(args)
    return 2
