"""Canonical lifecycle for hosted MCP connectors."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ..dot import connector_registry as registry
from ..dot import lifecycle
from ..dot.contracts import DotError


def register(sub: argparse._SubParsersAction) -> None:
    hub = sub.add_parser("hub", help="Manage hub connectors.")
    groups = hub.add_subparsers(dest="hub_command", required=True)
    connector = groups.add_parser("connector", help="Manage isolated MCP connector profiles.")
    commands = connector.add_subparsers(dest="connector_command", required=True)
    for name in ("list", "show", "setup", "doctor", "canary", "install-service", "update", "remove", "serve"):
        parser = commands.add_parser(name, help=f"{name.capitalize()} a connector.")
        parser.add_argument("--target", type=Path, default=Path("."))
        if name != "list":
            parser.add_argument("--id", required=True)
        if name in {"setup", "serve"}:
            parser.add_argument("--bind")
            parser.add_argument("--allow-host", action="append", dest="allowed_hosts")
            parser.add_argument("--allow-origin", action="append", dest="allowed_origins")
        if name == "setup":
            parser.add_argument("--runtime-path", type=Path, required=True)
        if name in {"setup", "update", "remove", "install-service"}:
            parser.add_argument("--apply", action="store_true")
        if name == "remove":
            parser.add_argument("--unit-dir", type=Path)
        if name == "install-service":
            parser.add_argument("--out-dir", type=Path)
            parser.add_argument("--force", action="store_true")
        parser.set_defaults(func=dispatch)


def dispatch(args: argparse.Namespace) -> int:
    result: Any
    try:
        name, target = args.connector_command, args.target
        if name == "list":
            result = registry.list_packs(target)
        elif name == "show":
            result = registry.show_pack(args.id, target)
        elif name == "setup":
            action = registry.apply_setup if args.apply else registry.preview_setup
            result = action(
                target,
                args.id,
                bind=args.bind,
                allowed_hosts=args.allowed_hosts,
                allowed_origins=args.allowed_origins,
                runtime_path=args.runtime_path,
            )
        elif name in {"update", "remove"}:
            action = getattr(registry, ("apply_" if args.apply else "preview_") + name)
            result = action(target, args.id, **({"unit_dir": args.unit_dir} if name == "remove" else {}))
        elif name == "install-service":
            if not args.apply:
                print(registry.render_install_service(target, args.id), end="")
                return 0
            if args.out_dir is None:
                raise registry.PackError("out-dir-required")
            result = registry.apply_install_service(target, args.id, out_dir=args.out_dir, force=args.force)
        elif name == "serve":
            registry.show_pack(args.id, target)
            config, tools, runtime = lifecycle.build_listener_from_target(
                target,
                pack_id=args.id,
                bind=args.bind,
                allowed_hosts=args.allowed_hosts,
                allowed_origins=args.allowed_origins,
            )
            lifecycle.run_listener(config, tools, runtime)
            return 0
        else:
            result = getattr(registry, name)(target, args.id)
        print(json.dumps(result, indent=2, sort_keys=True))
        if name == "doctor":
            return int(any(c["status"] == "fail" for c in result))
        if name == "canary":
            return int(not result["ok"])
        return 0
    except (registry.PackError, DotError, OSError, ValueError):
        print("error: connector configuration or operation refused", file=sys.stderr)
        return 2
