"""brigade friction command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    p_friction = sub.add_parser("friction", help="Capture and scan workflow friction.")
    friction_sub = p_friction.add_subparsers(dest="friction_command", metavar="<friction-command>")
    friction_sub.required = True
    p_friction_scan = friction_sub.add_parser("scan", help="Scan local logs and notes for candidate friction.")
    p_friction_scan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_friction_scan.add_argument("--days", type=int, default=30, help="Lookback window in days.")
    p_friction_scan.add_argument(
        "--include-agent-logs",
        action="store_true",
        help="Also scan local Codex and Claude Code session/log directories.",
    )
    p_friction_scan.add_argument("--max-files", type=int, default=5000, help="Maximum source files to scan.")
    p_friction_scan.add_argument("--max-candidates", type=int, default=200, help="Maximum candidates to record.")
    p_friction_scan.add_argument(
        "--output",
        type=Path,
        default=None,
        help="JSON output path. Defaults to .brigade/friction/latest.json.",
    )
    p_friction_scan.add_argument(
        "--markdown",
        type=Path,
        default=None,
        help="Markdown output path. Defaults to .brigade/friction/latest.md.",
    )
    p_friction_scan.add_argument(
        "--import-candidates",
        action="store_true",
        help="Append candidates to the Brigade work import inbox for review.",
    )
    p_friction_scan.add_argument("--dry-run", action="store_true", help="Report without writing artifacts or imports.")
    p_friction_scan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_friction_scan.set_defaults(func=dispatch)

    p_friction_add = friction_sub.add_parser("add", help="Manually add a friction item to the work import inbox.")
    p_friction_add.add_argument("text", nargs="+", help="Friction note text.")
    p_friction_add.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_friction_add.add_argument("--type", default="manual", help="Friction type.")
    p_friction_add.add_argument("--severity", choices=["low", "medium", "high"], default="medium", help="Severity.")
    p_friction_add.add_argument("--workflow", default="manual", help="Workflow or system area.")
    p_friction_add.add_argument("--evidence", default=None, help="Optional evidence reference.")
    p_friction_add.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_friction_add.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import friction_cmd

    if args.friction_command == "scan":
        return friction_cmd.scan(
            target=args.target,
            days=args.days,
            include_agent_logs=args.include_agent_logs,
            max_files=args.max_files,
            max_candidates=args.max_candidates,
            output=args.output,
            markdown=args.markdown,
            import_candidates=args.import_candidates,
            dry_run=args.dry_run,
            json_output=args.json,
        )
    if args.friction_command == "add":
        return friction_cmd.add(
            target=args.target,
            text=" ".join(args.text),
            friction_type=args.type,
            severity=args.severity,
            workflow=args.workflow,
            evidence=args.evidence,
            json_output=args.json,
        )
    parser = getattr(args, "_brigade_parser", None)
    if parser is not None:
        parser.error(f"unknown friction command: {args.friction_command}")
    return 2

