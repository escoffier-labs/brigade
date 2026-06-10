"""brigade doctor command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    # doctor
    p_doctor = sub.add_parser("doctor", help="Verify a target workspace.")
    p_doctor.add_argument("--target", "-t", type=Path, default=Path("."))
    p_doctor.add_argument(
        "--harness",
        choices=["generic", "openclaw", "hermes"],
        default="generic",
    )
    p_doctor.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import doctor as doctor_mod

    return doctor_mod.run(target=args.target, harness=args.harness)
