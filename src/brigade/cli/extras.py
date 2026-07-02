"""brigade extras command group: toggle the wider operator-suite surface."""

from __future__ import annotations

import argparse
import sys

from .. import extras as extras_mod


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("extras", help="Enable or disable the extras command surface.")
    extras_sub = p.add_subparsers(dest="extras_command", metavar="<extras-command>")
    extras_sub.required = True
    extras_sub.add_parser("on", help="Enable the extras commands for this user.")
    extras_sub.add_parser("off", help="Disable the extras commands (BRIGADE_EXTRAS still overrides).")
    extras_sub.add_parser("status", help="Show whether extras commands are enabled and why.")
    p.set_defaults(func=dispatch)


def dispatch(args) -> int:
    if args.extras_command == "on":
        path = extras_mod.enable()
        print(f"extras: enabled ({path})")
        print(f"commands added: {', '.join(extras_mod.EXTRAS_COMMANDS)}")
        return 0
    if args.extras_command == "off":
        extras_mod.disable()
        if extras_mod.enabled():
            print("extras: still enabled via BRIGADE_EXTRAS in the environment", file=sys.stderr)
            return 1
        print("extras: disabled")
        return 0
    if args.extras_command == "status":
        if extras_mod.enabled():
            source = "environment (BRIGADE_EXTRAS)" if extras_mod.marker_path().is_file() is False else "marker file"
            print(f"extras: enabled ({source})")
        else:
            print("extras: disabled")
            print("enable with: brigade extras on")
        print(f"gated commands: {', '.join(extras_mod.EXTRAS_COMMANDS)}")
        return 0
    return 2


def register_stub(sub: argparse._SubParsersAction, name: str) -> None:
    """Register a disabled extras command so it fails with guidance, not a parse error."""
    p = sub.add_parser(name, help="(extras, disabled) enable with `brigade extras on`")
    p.add_argument("stub_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    p.set_defaults(func=lambda args, _name=name: _stub_dispatch(_name))


def _stub_dispatch(name: str) -> int:
    print(
        f"error: '{name}' is part of the brigade extras surface, which is disabled.\n"
        f"Enable it once with: brigade extras on\n"
        f"Or per invocation:   BRIGADE_EXTRAS=1 brigade {name} ...",
        file=sys.stderr,
    )
    return 2
