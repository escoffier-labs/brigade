"""brigade node command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    p_node = sub.add_parser(
        "node",
        help="Show or initialize this machine's Brigade node identity.",
    )
    identity = p_node.add_mutually_exclusive_group()
    identity.add_argument("--target", "-t", type=Path, default=Path("."))
    identity.add_argument(
        "--machine",
        action="store_true",
        help=(
            "Show or initialize the per-user home machine identity (the node.toml under BRIGADE_HOME, "
            "default ~/.brigade; the identity the fleet hub knows this machine by (#1161)). "
            "Without --machine, --target selects a workspace-local identity."
        ),
    )
    p_node.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text.")
    p_node.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import fleet_client
    from .. import node as node_mod

    target = fleet_client.home_identity_target() if args.machine else args.target
    return node_mod.run(target=target, json_output=args.json)
