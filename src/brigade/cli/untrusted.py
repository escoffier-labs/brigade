"""brigade untrusted command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    # untrusted
    p_untrusted = sub.add_parser("untrusted", help="Wrap and scan untrusted context.")
    untrusted_sub = p_untrusted.add_subparsers(dest="untrusted_command", metavar="<untrusted-command>")
    untrusted_sub.required = True
    p_untrusted_scan = untrusted_sub.add_parser("scan", help="Scan text for prompt-injection-style instructions.")
    p_untrusted_scan.add_argument("text", nargs="*", help="Text to scan.")
    p_untrusted_scan.add_argument("--from-file", type=Path, default=None, help="Read text from a file.")
    p_untrusted_scan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_untrusted_wrap = untrusted_sub.add_parser("wrap", help="Frame external content as untrusted data.")
    p_untrusted_wrap.add_argument("text", nargs="*", help="Text to wrap.")
    p_untrusted_wrap.add_argument("--from-file", type=Path, default=None, help="Read text from a file.")
    p_untrusted_wrap.add_argument(
        "--source-kind", choices=["web", "tool-output", "retrieved-doc", "memory", "skill", "handoff"], required=True
    )
    p_untrusted_wrap.add_argument("--goal", default=None, help="Trusted goal to include outside the untrusted block.")
    p_untrusted_wrap.add_argument(
        "--max-chars", type=int, default=None, help="Explicitly truncate content before wrapping."
    )
    p_untrusted_wrap.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_untrusted.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import untrusted_cmd

    if args.untrusted_command == "scan":
        return untrusted_cmd.scan(text=args.text, from_file=args.from_file, json_output=args.json)
    if args.untrusted_command == "wrap":
        return untrusted_cmd.wrap(
            text=args.text,
            from_file=args.from_file,
            source_kind=args.source_kind,
            goal=args.goal,
            max_chars=args.max_chars,
            json_output=args.json,
        )
    args._brigade_parser.error(f"unknown untrusted command: {args.untrusted_command}")
    return 2
