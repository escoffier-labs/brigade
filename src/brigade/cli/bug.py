"""Explicit, consent-driven bug reports."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    group = sub.add_parser("bug", help="Prepare and submit a redacted bug report.")
    verbs = group.add_subparsers(dest="bug_command", required=True)
    report = verbs.add_parser("report", help="Preview a complete redacted bundle before sharing it.")
    report.add_argument("--target", "-t", type=Path, default=Path("."))
    report.add_argument("--title", help="Title for a new report.")
    report.add_argument("--report-id", help="Reuse a saved preview without recollecting diagnostics.")
    summary = report.add_mutually_exclusive_group()
    summary.add_argument("--summary", help="What happened and what you expected.")
    summary.add_argument("--summary-file", type=Path, help="Read the summary from a local file.")
    report.add_argument("--include-output", type=Path, help="Include a redacted tail of a chosen file (200 lines).")
    report.add_argument("--kind", choices=("bug", "feedback", "security"), default="bug")
    report.add_argument("--via", choices=("auto", "gh", "url", "email", "file"), default="auto")
    report.add_argument("--comment-on", type=int, help="Comment on a duplicate shown by the fingerprint search.")
    report.add_argument("--json", action="store_true", help="Return the full preview and local receipt as JSON.")
    report.add_argument("--dry-run", action="store_true", help="Save and preview locally without network calls.")
    report.add_argument(
        "--yes", action="store_true", help="Submit --report-id after the user has approved its complete preview."
    )
    report.add_argument(
        "--not-security",
        action="store_true",
        help="Override vulnerability keyword matches for an ordinary bug. Required for non-interactive --yes.",
    )
    report.set_defaults(func=dispatch)


def dispatch(args: argparse.Namespace) -> int:
    from ..bug_cmd import report

    return report(args)
