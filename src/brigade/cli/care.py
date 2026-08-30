"""brigade care command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    p_care = sub.add_parser(
        "care",
        help="Install, audit, and remove operator-owned scheduled care entries.",
    )
    care_sub = p_care.add_subparsers(dest="care_command", metavar="<care-command>")
    care_sub.required = True

    p_install = care_sub.add_parser(
        "install",
        help="Write managed scheduler entries for the scheduled-care recipes.",
    )
    p_install.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to schedule against."
    )
    p_install.add_argument(
        "--backend",
        choices=["auto", "systemd", "launchd", "schtasks"],
        default="auto",
        help="Scheduler backend (default: systemd on Linux, launchd on macOS, schtasks on Windows).",
    )
    p_install.add_argument("--dry-run", action="store_true", help="Show the plan without writing.")
    p_install.add_argument(
        "--adopt",
        action="store_true",
        help="Replace a hand-edited managed block instead of reporting a conflict.",
    )
    p_install.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_install.add_argument(
        "--entry",
        dest="entry_ids",
        action="append",
        metavar="JOB_ID",
        help="Install one named care entry. Repeatable. Default: the atomic scheduled-care set.",
    )
    p_install.add_argument(
        "--schedule",
        dest="schedule_specs",
        action="append",
        metavar="JOB_ID=VALUE",
        help="Override one selected entry's schedule. Repeatable.",
    )

    p_status = care_sub.add_parser(
        "status",
        help="List installed care entries and the latest receipt for each runbook recipe.",
    )
    p_status.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_status.add_argument(
        "--backend",
        choices=["auto", "systemd", "launchd", "schtasks"],
        default="auto",
        help="Scheduler backend (default: systemd on Linux, launchd on macOS, schtasks on Windows).",
    )
    p_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_status.add_argument(
        "--entry",
        dest="entry_ids",
        action="append",
        metavar="JOB_ID",
        help="Report one named care entry. Repeatable. Default: installed registrations for the target.",
    )

    p_uninstall = care_sub.add_parser(
        "uninstall",
        help="Remove Brigade-managed care scheduler entries only.",
    )
    p_uninstall.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace the entries reference."
    )
    p_uninstall.add_argument(
        "--backend",
        choices=["auto", "systemd", "launchd", "schtasks"],
        default="auto",
        help="Scheduler backend (default: systemd on Linux, launchd on macOS, schtasks on Windows).",
    )
    p_uninstall.add_argument("--dry-run", action="store_true", help="Show the plan without writing.")
    p_uninstall.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_uninstall.add_argument(
        "--entry",
        dest="entry_ids",
        action="append",
        metavar="JOB_ID",
        help="Remove one named care entry. Repeatable. Default: the atomic scheduled-care set.",
    )

    p_care.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import care_cmd

    if args.care_command == "install":
        return care_cmd.install(
            target=args.target,
            backend=args.backend,
            dry_run=args.dry_run,
            adopt=args.adopt,
            json_output=args.json,
            entry_ids=args.entry_ids,
            schedule_specs=args.schedule_specs,
        )
    if args.care_command == "status":
        return care_cmd.status(
            target=args.target,
            backend=args.backend,
            json_output=args.json,
            entry_ids=args.entry_ids,
        )
    if args.care_command == "uninstall":
        return care_cmd.uninstall(
            target=args.target,
            backend=args.backend,
            dry_run=args.dry_run,
            json_output=args.json,
            entry_ids=args.entry_ids,
        )
    args._brigade_parser.error(f"unknown care command: {args.care_command}")
    return 2
