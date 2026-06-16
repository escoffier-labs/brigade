"""brigade scrub command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    # scrub
    p_scrub = sub.add_parser("scrub", help="Run content-guard against a target.")
    p_scrub.add_argument("--target", "-t", type=Path, default=Path("."))
    p_scrub.add_argument(
        "--policy",
        default="public-repo",
        help="Policy file name (looks under .brigade/policies, then content-guard/policies) or path.",
    )
    p_scrub.add_argument("--dry-run", action="store_true")
    p_scrub.add_argument("--json", action="store_true", help="Print a machine-readable summary (no matched snippets).")
    p_scrub.add_argument(
        "--no-receipt", action="store_true", help="Do not write the .brigade/scrub/latest.json receipt."
    )
    p_scrub.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import scrub as scrub_mod

    return scrub_mod.run(
        target=args.target,
        policy=args.policy,
        dry_run=args.dry_run,
        json_output=args.json,
        write_receipt=not args.no_receipt,
    )
