"""brigade setup command group."""

from __future__ import annotations

import argparse


def register(sub: argparse._SubParsersAction) -> None:
    p_setup = sub.add_parser("setup", help="Install pinned native Brigade components.")
    p_setup.add_argument("--dry-run", action="store_true", help="Report planned actions without writing.")
    p_setup.add_argument("--offline", action="store_true", help="Use verified cache only; fail if missing.")
    p_setup.add_argument("--rollback", action="store_true", help="Restore the previous installed manifest.")
    p_setup.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from ..component_install import setup_native_components

    return setup_native_components(
        dry_run=args.dry_run,
        offline=args.offline,
        rollback=args.rollback,
    )
