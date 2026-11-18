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
    p_t3.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import native_t3_dispatch, native_t3_enrollment

    if args.t3_command == "open":
        return native_t3_enrollment.open_cli(args)
    if args.t3_command == "claim":
        return native_t3_dispatch.claim_cli(args)
    if args.t3_command == "accepted":
        return native_t3_dispatch.accepted_cli(args)
    if args.t3_command == "hold-resolve":
        return native_t3_dispatch.hold_resolve_cli(args)
    return 2
