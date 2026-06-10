"""brigade openclaw-fragments command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    # openclaw-fragments
    p_ocf = sub.add_parser("openclaw-fragments", help="Write OpenClaw config fragments for manual review.")
    p_ocf.add_argument("--out", "-o", type=Path, required=True, help="Output directory.")
    p_ocf.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import fragments as frag_mod

    return frag_mod.write_fragments(args.out, harness="openclaw")
