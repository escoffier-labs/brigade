"""brigade doctor command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    # doctor
    p_doctor = sub.add_parser(
        "doctor",
        help="Verify a target workspace (target-scoped checks only unless --operator).",
    )
    p_doctor.add_argument("--target", "-t", type=Path, default=Path("."))
    p_doctor.add_argument(
        "--harness",
        choices=["generic", "openclaw", "hermes"],
        default="generic",
    )
    p_doctor.add_argument(
        "--operator",
        action="store_true",
        help="Include host-global operator checks (managed tools, components, OpenClaw, content guard).",
    )
    p_doctor.add_argument(
        "--full",
        action="store_true",
        help="Show every check instead of condensing long reports.",
    )
    p_doctor.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text.")
    p_doctor.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import doctor as doctor_mod

    return doctor_mod.run(
        target=args.target,
        harness=args.harness,
        json_output=args.json,
        full=args.full,
        operator=args.operator,
    )
