"""Administrative grants and opt-in action broker commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import fleet_action_runner as runner, fleet_client_actions as client
from ..fleet_action_auth import ActionError
from ..fleet_client import FleetClientError


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("actions", help="Scoped alert repair actions and node broker.")
    commands = parser.add_subparsers(dest="action_command", required=True)
    grant = commands.add_parser(
        "grant-operator", help="Allowlist a trusted login for exact destination and subject scopes."
    )
    grant.add_argument("--login", required=True)
    grant.add_argument("--scope-file", type=Path, required=True)
    revoke = commands.add_parser("revoke-operator", help="Revoke an operator grant and its sessions.")
    revoke.add_argument("grant_id")
    broker = commands.add_parser("grant-broker", help="Grant an enrolled node exact target and subject scopes.")
    broker.add_argument("--scope-file", type=Path, required=True)
    revoke_broker = commands.add_parser(
        "revoke-broker", help="Revoke a node's action authority, preserving telemetry enrollment."
    )
    revoke_broker.add_argument("node_id")
    targets = commands.add_parser("targets", help="Publish secret-free target registrations.")
    targets_sub = targets.add_subparsers(dest="targets_command", required=True)
    sync = targets_sub.add_parser("sync", help="Register targets from a protected local manifest.")
    sync.add_argument("--file", dest="manifest", type=Path, required=True)
    tick = commands.add_parser(
        "tick", help="Schedule at most one transient action job within a bounded telemetry tick."
    )
    tick.add_argument("--manifest", type=Path, required=True)
    tick.add_argument("--budget-seconds", type=float, default=15)
    tick.add_argument("--max-actions", type=int, default=1, choices=(1,))
    execute = commands.add_parser(
        "execute", help="Execute or reconcile one generation under its durable journal fence."
    )
    execute.add_argument("action_id")
    execute.add_argument("--generation", type=int, required=True)
    execute.add_argument("--manifest", type=Path, required=True)
    show = commands.add_parser("show", help="Read one action owned by this broker or the admin.")
    show.add_argument("action_id")
    show.add_argument("--admin", action="store_true")
    for command in (grant, revoke, broker, revoke_broker, sync, tick, execute, show):
        command.add_argument("--json", action="store_true", help="Emit a structured result.")
        command.set_defaults(func=dispatch)


def dispatch(args: argparse.Namespace) -> int:
    try:
        op = args.action_command
        if op == "grant-operator":
            scope = runner.read_protected(args.scope_file)
            result = client.request({"op": op, "login": args.login, "scopes": scope.get("scopes")}, admin=True)
        elif op == "revoke-operator":
            result = client.request({"op": op, "grant_id": args.grant_id}, admin=True)
        elif op == "revoke-broker":
            result = client.request({"op": op, "node_id": args.node_id}, admin=True)
        elif op == "grant-broker":
            result = client.request({"op": op, "scope": runner.read_protected(args.scope_file)}, admin=True)
        elif op == "targets":
            result = runner.sync_targets(args.manifest)
        elif op == "tick":
            result = runner.tick(args.manifest, budget_seconds=args.budget_seconds, max_actions=args.max_actions)
        elif op == "execute":
            result = runner.execute(args.manifest, args.action_id, args.generation)
        else:
            result = client.request({"op": "show", "action_id": args.action_id}, admin=args.admin)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except (ActionError, FleetClientError, OSError, ValueError) as exc:
        # Never echo configuration paths, prompt, raw provider output or credentials.
        reason = exc.reason if isinstance(exc, ActionError) else "action-command-failed"
        if args.json:
            print(json.dumps({"error": reason}))
        print("error: " + reason, file=sys.stderr)
        return 1
