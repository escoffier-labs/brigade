"""brigade add command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    # add
    p_add = sub.add_parser("add", help="Install and wire a station's managed tools.")
    p_add.add_argument("station", help="Station name or local path containing station.json.")
    p_add.add_argument("--target", "-t", type=Path, default=Path("."))
    p_add.add_argument("--install", action="store_true", help="Run install commands from a station.json manifest.")
    p_add.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import add as add_mod

    return add_mod.run(target=args.target, station=args.station, install_manifest=args.install)
