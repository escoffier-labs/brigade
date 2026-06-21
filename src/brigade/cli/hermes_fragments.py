"""brigade hermes-fragments command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    # hermes-fragments
    p_hf = sub.add_parser("hermes-fragments", help="Write Hermes adapter fragments.")
    p_hf.add_argument("--out", "-o", type=Path, required=True, help="Output directory.")
    p_hf.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import fragments as frag_mod

    return frag_mod.write_fragments(args.out, harness="hermes")
