"""Local master-roster validation commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def register(fleet_sub: argparse._SubParsersAction) -> None:
    parser = fleet_sub.add_parser("roster", help="Validate or synchronize the fleet master roster.")
    commands = parser.add_subparsers(dest="fleet_roster_command", metavar="<roster-command>")
    commands.required = True
    validate = commands.add_parser("validate", help="Validate a master-roster JSON document locally.")
    validate.add_argument("--file", type=Path, required=True, help="Path to the master-roster JSON file.")
    validate.add_argument("--json", action="store_true", help="Emit JSON instead of a summary line.")
    validate.set_defaults(func=dispatch_validate)


def dispatch_validate(args: argparse.Namespace) -> int:
    from .. import fleet_master_roster

    try:
        with args.file.open("rb") as roster_file:
            raw = roster_file.read(fleet_master_roster.MAX_DOCUMENT_BYTES + 1)
        document = fleet_master_roster.parse(raw)
    except OSError as exc:
        print(f"error: cannot read roster file: {exc.strerror or 'I/O error'}", file=sys.stderr)
        return 1
    except fleet_master_roster.MasterRosterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    output = {
        "schema": document["schema"],
        "revision": document["revision"],
        "sha256": document["sha256"],
        "seats": len(document["seats"]),
    }
    if args.json:
        print(json.dumps(output, sort_keys=True))
    else:
        print(f"valid master roster revision {output['revision']}: {output['seats']} seats ({output['sha256']})")
    return 0
