"""brigade setup command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    p_setup = sub.add_parser("setup", help="Install pinned native Brigade components.")
    p_setup.add_argument("--dry-run", action="store_true", help="Report planned actions without writing.")
    p_setup.add_argument("--offline", action="store_true", help="Use verified cache only; fail if missing.")
    p_setup.add_argument("--rollback", action="store_true", help="Restore the previous installed manifest.")
    p_setup.add_argument("--manifest", type=Path, help="Use this already verified immutable component manifest.")
    p_setup.add_argument(
        "--allow-compatible-stable-manifest",
        metavar="VERSION",
        help=argparse.SUPPRESS,
    )
    p_setup.add_argument(
        "--manifest-source",
        choices=("auto", "standalone"),
        default="auto",
        help="Select exact release metadata, or the one-release standalone compatibility manifest.",
    )
    p_setup.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from ..component_install import setup_native_components

    kwargs = dict(
        dry_run=args.dry_run,
        offline=args.offline,
        rollback=args.rollback,
    )
    if args.manifest is not None:
        kwargs["manifest_path"] = args.manifest
    if args.allow_compatible_stable_manifest is not None:
        if args.manifest is None:
            args._brigade_parser.error("--allow-compatible-stable-manifest requires --manifest")
        kwargs["allow_compatible_stable_manifest"] = args.allow_compatible_stable_manifest
    if args.manifest_source != "auto":
        kwargs["manifest_source"] = args.manifest_source
    return setup_native_components(**kwargs)
