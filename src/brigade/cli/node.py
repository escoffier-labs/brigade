"""brigade node command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    p_node = sub.add_parser(
        "node",
        help="Show or initialize this machine's Brigade node identity.",
    )
    p_node.add_argument("--target", "-t", type=Path, default=Path("."))
    p_node.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text.")
    p_node.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import node as node_mod

    return node_mod.run(target=args.target, json_output=args.json)
