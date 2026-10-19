"""brigade fleet owners command group (hub ownership map)."""

from __future__ import annotations

import argparse

from .. import fleet_client_policy
from .fleet_policy import _emit, _fail


def register(fleet_sub: argparse._SubParsersAction) -> None:
    p_owners = fleet_sub.add_parser("owners", help="Read the fleet ownership map: charters and what each owner holds.")
    owners_sub = p_owners.add_subparsers(dest="owners_command", metavar="<owners-command>")
    owners_sub.required = True

    p_show = owners_sub.add_parser("show", help="Print the hub ownership map as JSON (brigade.fleet_owners.v1).")
    p_show.set_defaults(func=_dispatch_show)


def _dispatch_show(args: argparse.Namespace) -> int:
    try:
        payload = fleet_client_policy.show_owners()
    except Exception as exc:
        return _fail(exc)
    return _emit(payload, json_out=True)
