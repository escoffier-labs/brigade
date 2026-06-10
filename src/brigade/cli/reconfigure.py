"""brigade reconfigure command group."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    # reconfigure
    p_recon = sub.add_parser("reconfigure", help="Adjust an existing install to a new Selection.")
    p_recon.add_argument("--target", "-t", type=Path, default=Path("."))
    p_recon.add_argument("--depth", choices=["repo", "workspace"], default=None)
    p_recon.add_argument("--harnesses", default=None)
    p_recon.add_argument("--owner", default=None)
    p_recon.add_argument("--include", dest="includes", action="append", default=[])
    p_recon.add_argument("--prune", action="store_true", help="Remove files for harnesses no longer selected.")
    p_recon.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from ..config import load_config
    from ..reconfigure import reconfigure as _reconfigure
    from ..selection import Selection, KNOWN_HARNESSES, resolve_owner

    existing = load_config(args.target)
    if existing is None:
        print("error: no .brigade/config.json in target. Run `brigade init` first.", file=sys.stderr)
        return 2

    depth = args.depth or existing.selection.depth
    if args.harnesses is None:
        harnesses = list(existing.selection.harnesses)
    elif args.harnesses == "none":
        harnesses = []
    else:
        harnesses = [h.strip() for h in args.harnesses.split(",") if h.strip()]
    for h in harnesses:
        if h not in KNOWN_HARNESSES:
            print(f"error: unknown harness {h!r}", file=sys.stderr)
            return 2
    owner = resolve_owner(harnesses, override=args.owner)
    includes = list(args.includes) if args.includes else list(existing.selection.includes)
    new_sel = Selection(depth=depth, harnesses=harnesses, owner=owner, includes=includes)
    return _reconfigure(args.target, new_selection=new_sel, prune=args.prune)
