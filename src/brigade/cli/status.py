"""brigade status command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    # status
    p_status = sub.add_parser("status", help="Show which stations are present and healthy.")
    p_status.add_argument("--target", "-t", type=Path, default=Path("."))
    p_status.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import status as status_mod

    return status_mod.run(target=args.target)
