"""brigade stations command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    p_stations = sub.add_parser(
        "stations", help="Inspect the built-in station catalog and discover station.json files."
    )
    stations_sub = p_stations.add_subparsers(dest="stations_command", metavar="<stations-command>")
    stations_sub.required = True
    p_stations_list = stations_sub.add_parser("list", help="List stations for a built-in profile.")
    p_stations_list.add_argument("--profile", default="repo", help="Built-in profile name or alias to compare against.")
    p_stations_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_discover = stations_sub.add_parser(
        "discover",
        help="Find external station.json catalogs (schema brigade.station.v1) under local roots.",
    )
    p_discover.add_argument(
        "--root",
        action="append",
        type=Path,
        default=None,
        help="Root directory to scan (repeatable). Defaults to cwd plus ~/repos, ~/src, ~/code when present.",
    )
    p_discover.add_argument(
        "--max-depth",
        type=int,
        default=2,
        help="How deep under each root to look for station.json (default 2: root and one level of children).",
    )
    p_discover.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_stations.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import stations_cmd

    if args.stations_command == "list":
        return stations_cmd.list_stations(profile_name=args.profile, json_output=args.json)
    if args.stations_command == "discover":
        return stations_cmd.discover(roots=args.root, max_depth=args.max_depth, json_output=args.json)
    args._brigade_parser.error(f"unknown stations command: {args.stations_command}")
    return 2
