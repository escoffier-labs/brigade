"""brigade handoff-template command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    # handoff-template
    p_ht = sub.add_parser("handoff-template", help="Print the handoff TEMPLATE.md.")
    p_ht.add_argument(
        "--target",
        "-t",
        type=Path,
        default=None,
        help="Prefer the target's installed TEMPLATE.md when present.",
    )
    p_ht.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import handoff as handoff_mod

    return handoff_mod.run(target=args.target)
