"""brigade model command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    p_model = sub.add_parser("model", help="Inspect model performance across Brigade runs.")
    model_sub = p_model.add_subparsers(dest="model_command", metavar="<model-command>")
    model_sub.required = True

    p_scorecard = model_sub.add_parser(
        "scorecard",
        help="Aggregate run artifacts into a per-(cli, model) scorecard (read-only).",
    )
    p_scorecard.add_argument(
        "--target",
        "-t",
        type=Path,
        default=Path("."),
        help="Workspace whose .brigade/runs directory should be scanned (default: .).",
    )
    p_scorecard.add_argument(
        "--runs-dir",
        type=Path,
        action="append",
        default=None,
        dest="runs_dirs",
        help="Explicit runs directory (repeatable). When set, replaces the default under --target.",
    )
    p_scorecard.add_argument(
        "--since",
        default=None,
        help="Only include runs with started_at on or after this UTC date (YYYY-MM-DD).",
    )
    p_scorecard.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text.")
    p_scorecard.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="List skipped run directories and skip reasons.",
    )
    p_scorecard.set_defaults(func=_dispatch_scorecard)


def _dispatch_scorecard(args) -> int:
    from .. import model_scorecard

    return model_scorecard.scorecard(
        target=args.target,
        runs_dirs=args.runs_dirs,
        since=args.since,
        json_output=args.json,
        verbose=args.verbose,
    )
