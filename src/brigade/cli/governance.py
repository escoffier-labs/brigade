"""``brigade governance`` command group."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("governance", help="Export a privacy-bounded Brigade scope inventory.")
    governance_sub = parser.add_subparsers(dest="governance_command", metavar="<governance-command>")
    governance_sub.required = True

    inventory = governance_sub.add_parser(
        "inventory",
        help="Export the configured Brigade workspace scope, without provider queries.",
    )
    inventory.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace to inspect.")
    inventory.add_argument("--since", default=None, help="RFC 3339 lower bound for observed run facts.")
    inventory.add_argument("--until", default=None, help="RFC 3339 upper bound for observed run facts.")
    inventory.add_argument(
        "--output-dir", type=Path, default=None, help="Empty directory to atomically publish artifacts."
    )
    inventory.add_argument("--cyclonedx", action="store_true", help="Also emit an opt-in CycloneDX 1.7 BOM.")
    inventory.add_argument("--json", action="store_true", help="Emit JSON output when publishing artifacts.")
    inventory.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import governance_inventory

    try:
        if args.governance_command != "inventory":
            args._brigade_parser.error(f"unknown governance command: {args.governance_command}")
            return 2
        if args.output_dir is None:
            inventory = governance_inventory.build_inventory(target=args.target, since=args.since, until=args.until)
            if args.cyclonedx:
                inventory["cyclonedx"] = governance_inventory.cyclonedx_bom(inventory)
            print(json.dumps(inventory, indent=2, sort_keys=True))
            return 0
        artifacts = governance_inventory.write_artifacts(
            target=args.target,
            output_dir=args.output_dir,
            since=args.since,
            until=args.until,
            cyclonedx=args.cyclonedx,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("error: governance inventory input or output is inaccessible", file=sys.stderr)
        return 2
    payload = {"artifacts": artifacts}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("governance inventory: " + ", ".join(artifacts))
    return 0
