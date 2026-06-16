"""brigade completions command group."""

from __future__ import annotations

import argparse


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("completions", help="Print a shell completion script (bash, zsh, or fish).")
    p.add_argument("shell", choices=["bash", "zsh", "fish"], help="Shell to generate completions for.")
    p.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import completions as completions_mod

    return completions_mod.emit(shell=args.shell)
