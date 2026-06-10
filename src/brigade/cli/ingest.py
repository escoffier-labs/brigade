"""brigade ingest command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    # ingest
    p_ing = sub.add_parser("ingest", help="Process writer memory-handoff inboxes into canonical memory.")
    p_ing.add_argument("--target", "-t", type=Path, default=Path("."))
    p_ing.add_argument("--dry-run", action="store_true")
    p_ing.add_argument(
        "--promote-cards",
        action="store_true",
        help="Auto-promote create-card / update-card handoffs (default off; opt-in).",
    )
    p_ing.add_argument(
        "--route-documents",
        action="store_true",
        help="Auto-route no-card handoffs to TOOLS.md/USER.md/rules/.learnings (default off; opt-in).",
    )
    p_ing.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import ingest as ingest_mod

    return ingest_mod.run(
        target=args.target,
        dry_run=args.dry_run,
        promote_cards=args.promote_cards,
        route_documents=args.route_documents,
    )
