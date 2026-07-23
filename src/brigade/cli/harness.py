"""User-scoped harness onboarding commands."""

from __future__ import annotations

import argparse
import sys


def _write_mode(parser: argparse.ArgumentParser) -> None:
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview changes without writing (default).")
    mode.add_argument("--write", action="store_true", help="Apply the planned changes.")


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("harness", choices=["cursor"], help="Harness to configure.")
    parser.add_argument("--scope", choices=["user"], required=True, help="Configuration scope.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("harness", help="Install, inspect, or remove narrow harness onboarding profiles.")
    commands = parser.add_subparsers(dest="harness_command", metavar="<harness-command>")
    commands.required = True

    install = commands.add_parser("install", help="Plan or apply a harness onboarding profile.")
    _common(install)
    _write_mode(install)
    install.add_argument(
        "--surface",
        choices=["cursor-cli", "cursor-gui"],
        help="Install Brigade's cursor projection for this explicitly selected vendor surface.",
    )
    install.add_argument(
        "--projection-only",
        action="store_true",
        help="Allow a runtime-absent surface to receive Brigade projections without claiming a native runtime.",
    )

    uninstall = commands.add_parser("uninstall", help="Remove only Brigade-owned harness configuration.")
    _common(uninstall)
    _write_mode(uninstall)

    doctor = commands.add_parser("doctor", help="Check a harness onboarding profile.")
    _common(doctor)

    parser.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import cursor_user_cmd
    from ..install import ensure_surface_installable
    from ..selection import SurfaceInstallRefusal, SurfaceRecord

    if args.harness_command == "install":
        if args.projection_only and not args.surface:
            print("error: --projection-only requires --surface", file=sys.stderr)
            return 2
        try:
            if args.surface:
                surface = SurfaceRecord.resolve_known(args.surface)
                ensure_surface_installable(surface, projection_only=args.projection_only)
            return cursor_user_cmd.install(write=args.write, json_output=args.json)
        except SurfaceInstallRefusal as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.harness_command == "uninstall":
        return cursor_user_cmd.uninstall(write=args.write, json_output=args.json)
    if args.harness_command == "doctor":
        return cursor_user_cmd.doctor(json_output=args.json)
    args._brigade_parser.error(f"unknown harness command: {args.harness_command}")
    return 2
