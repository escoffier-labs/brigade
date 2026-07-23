"""User-scoped harness onboarding commands."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import harness_profiles

USER_SCOPE_TARGETS = harness_profiles.USER_SCOPE_TARGETS


def _write_mode(parser: argparse.ArgumentParser, *, default_dry: bool = True) -> None:
    mode = parser.add_mutually_exclusive_group()
    if default_dry:
        mode.add_argument("--dry-run", action="store_true", help="Preview changes without writing (default).")
        mode.add_argument("--write", action="store_true", help="Apply the planned changes.")
    else:
        mode.add_argument("--write", action="store_true", help="Apply the planned changes.")
        mode.add_argument("--dry-run", action="store_true", help="Preview changes without writing (default).")


def _common_slice1(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--target",
        choices=USER_SCOPE_TARGETS,
        required=True,
        help="Harness to configure (claude, codex, openclaw, kimi, grok, cursor, opencode, or all).",
    )
    parser.add_argument("--scope", choices=["user"], required=True, help="Configuration scope.")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace the profile is bound to (default: current directory).",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")


def _common_cursor(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("harness", choices=["cursor"], help="Harness to configure.")
    parser.add_argument("--scope", choices=["user"], required=True, help="Configuration scope.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("harness", help="Install, inspect, or remove narrow harness onboarding profiles.")
    commands = parser.add_subparsers(dest="harness_command", metavar="<harness-command>")
    commands.required = True

    sync = commands.add_parser("sync", help="Plan or apply user-scope harness onboarding (dry-run default).")
    _common_slice1(sync)
    _write_mode(sync)
    sync.add_argument(
        "--allow-global-stdio",
        action="store_true",
        help="Permit stdio MCP servers projected into the user home.",
    )
    sync.add_argument(
        "--adopt",
        action="store_true",
        help="Adopt a foreign managed block or generated file instead of reporting a conflict.",
    )

    uninstall = commands.add_parser("uninstall", help="Remove only Brigade-owned harness configuration.")
    uninstall_target = uninstall.add_mutually_exclusive_group(required=True)
    uninstall_target.add_argument(
        "--target",
        choices=USER_SCOPE_TARGETS,
        help="User-scope harness target to uninstall.",
    )
    uninstall_target.add_argument(
        "harness",
        nargs="?",
        choices=["cursor"],
        help="Legacy Cursor uninstall (positional harness).",
    )
    uninstall.add_argument("--scope", choices=["user"], required=True, help="Configuration scope.")
    uninstall.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace the profile is bound to (default: current directory).",
    )
    uninstall.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    _write_mode(uninstall)

    doctor = commands.add_parser("doctor", help="Check a harness onboarding profile.")
    doctor_target = doctor.add_mutually_exclusive_group(required=True)
    doctor_target.add_argument(
        "--target",
        choices=USER_SCOPE_TARGETS,
        help="User-scope harness target to inspect.",
    )
    doctor_target.add_argument(
        "harness",
        nargs="?",
        choices=["cursor"],
        help="Legacy Cursor doctor (positional harness).",
    )
    doctor.add_argument("--scope", choices=["user"], required=True, help="Configuration scope.")
    doctor.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace the profile is bound to (default: current directory).",
    )
    doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    doctor.add_argument(
        "--verify-mcp",
        action="store_true",
        help="Also verify native MCP projections for the selected targets.",
    )

    install = commands.add_parser("install", help="Legacy Cursor user-scope install (use sync for user profiles).")
    _common_cursor(install)
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

    parser.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import cursor_user_cmd, harness_profile_cmd
    from ..install import ensure_surface_installable
    from ..selection import SurfaceInstallRefusal, SurfaceRecord

    if args.harness_command == "sync":
        return harness_profile_cmd.sync(
            harness=args.target,
            workspace=args.workspace,
            write=bool(args.write),
            allow_global_stdio=bool(getattr(args, "allow_global_stdio", False)),
            adopt=bool(getattr(args, "adopt", False)),
            json_output=args.json,
        )

    if args.harness_command == "uninstall":
        if getattr(args, "target", None):
            return harness_profile_cmd.uninstall(
                harness=args.target,
                workspace=args.workspace,
                write=bool(args.write),
                json_output=args.json,
            )
        return cursor_user_cmd.uninstall(write=args.write, json_output=args.json)

    if args.harness_command == "doctor":
        if getattr(args, "target", None):
            return harness_profile_cmd.doctor(
                harness=args.target,
                workspace=args.workspace,
                verify_mcp=bool(getattr(args, "verify_mcp", False)),
                json_output=args.json,
            )
        return cursor_user_cmd.doctor(json_output=args.json)

    if args.harness_command == "install":
        if args.harness != "cursor":
            args._brigade_parser.error("use `brigade harness sync --target <harness|all> --scope user`")
            return 2
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

    args._brigade_parser.error(f"unknown harness command: {args.harness_command}")
    return 2
