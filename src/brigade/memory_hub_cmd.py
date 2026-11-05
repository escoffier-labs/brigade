"""`brigade memory hub status|pull|push|import|card|proposals` (S14)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import memory_hub_client as hubc

COMMANDS = ("status", "pull", "push", "import", "card", "proposals")


def register(memory_sub: argparse._SubParsersAction) -> None:
    hub = memory_sub.add_parser(
        "hub", help="Read and publish hub memory: status, pull the replica, push local cards, import, read a card."
    )
    commands = hub.add_subparsers(dest="memory_hub_command", metavar="<memory-hub-command>", required=True)
    helps = {
        "status": "Show the hub memory mode, the hub's memory status, the replica, the spool and open proposals.",
        "pull": "Follow the hub log into this host's replica; a changed log_epoch rebuilds it.",
        "push": 'Publish changed local cards to the configured scope (needs [hub] mode = "sync").',
        "import": "Import cards into hub memory, one call per card (needs the import grant).",
        "card": "Read one hub card's text from the [hub] read scopes, in 30,000-character chunks.",
        "proposals": "List the writes waiting for review, or drop one so its card pushes again.",
    }
    for name in COMMANDS:
        command = commands.add_parser(name, help=helps[name])
        if name == "proposals":
            actions = command.add_subparsers(dest="memory_hub_proposals_command", metavar="<action>", required=True)
            listing = actions.add_parser("list", help="List open proposals for every hub.")
            drop = actions.add_parser(
                "drop",
                help="Drop one proposal after review; the replica's hub head becomes the card's base when known.",
            )
            drop.add_argument("proposal_id", help="Proposal id, as `proposals list` prints it.")
            for action in (listing, drop):
                action.add_argument(
                    "--target", "-t", type=Path, default=Path("."), help="Workspace with the [hub] config."
                )
                action.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
            continue
        command.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace with the [hub] config.")
        command.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
        if name == "push":
            command.add_argument(
                "--yes", action="store_true", help="Confirm the first publish into this scope after reading its count."
            )
        if name == "import":
            command.add_argument("paths", nargs="+", type=Path, help="Card files to import.")
            command.add_argument("--scope", help="Scope to import into (default: [hub] scope).")
        if name == "card":
            command.add_argument("card_id", help="Card id, [a-z0-9][a-z0-9._-]{0,127}.")
            command.add_argument("--scope", help="Scope to read from; one of the [hub] read scopes.")
            command.add_argument("--offset", type=int, default=0, help="Character offset of the chunk.")
            command.add_argument("--all", action="store_true", help="Read every chunk and print the whole text.")


def _emit(payload: dict[str, Any], *, json_output: bool, lines: list[str], code: int = 0) -> int:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        for line in lines:
            print(line)
        for warning in payload.get("warnings") or []:
            print(f"warning: {warning}")
    return code


def _refusal(exc: hubc.HubRefusal, *, json_output: bool) -> int:
    payload = {"ok": False, "status": exc.status, "code": exc.code, "error": exc.error}
    return _emit(
        payload, json_output=json_output, lines=[f"error: hub refused ({exc.status} {exc.code}): {exc.error}"], code=1
    )


NO_NODE_CREDENTIAL = (
    "no node token is configured (BRIGADE_FLEET_NODE_TOKEN or [fleet] node_token_file); "
    "hub memory never uses the shared admin token"
)


def _unavailable(exc: hubc.HubUnavailable, *, json_output: bool) -> int:
    payload = {"ok": False, "hub": "unavailable", "reason": exc.reason}
    detail = f": {NO_NODE_CREDENTIAL}" if exc.reason == "no-node-credential" else ""
    return _emit(
        payload, json_output=json_output, lines=[f"error: hub memory unavailable ({exc.reason}){detail}"], code=1
    )


def _card_scope(client: hubc.MemoryHubClient, settings: hubc.HubSettings, args: argparse.Namespace) -> tuple[str, dict]:
    """The read scope that holds the card, and the first chunk's answer. Never a scope outside the config."""
    if args.scope:
        if args.scope not in settings.read_scopes:
            raise hubc.HubConfigError(
                f"--scope must be one of the [hub] read scopes: {', '.join(settings.read_scopes)}"
            )
        candidates: tuple[str, ...] = (args.scope,)
    else:
        candidates = settings.read_scopes
    found: list[tuple[str, dict]] = []
    refusal: hubc.HubRefusal | None = None
    for scope in candidates:
        try:
            found.append((scope, client.call("card", card_id=args.card_id, scope=scope, offset=args.offset)))
        except hubc.HubRefusal as exc:
            if len(candidates) == 1 or exc.code not in {"memory_card_not_found", "memory_grant_missing"}:
                raise
            refusal = refusal or exc
    if len(found) > 1:
        scopes = [scope for scope, _ in found]
        raise hubc.HubRefusal(
            409,
            {
                "code": "memory_card_ambiguous",
                "error": f"the card is in more than one read scope; pass --scope ({', '.join(scopes)})",
                "scopes": scopes,
            },
        )
    if not found:
        raise refusal or hubc.HubRefusal(404, {"code": "memory_card_not_found", "error": "card not found"})
    return found[0]


def _proposals(args: argparse.Namespace, target: Path) -> int:
    from . import memory_hub_sync as sync

    if args.memory_hub_proposals_command == "list":
        rows = sync.proposal_rows(target)
        lines = [f"proposals: {len(rows)} waiting for review"]
        lines += [f"- {row['proposal_id']}  {row['scope']}/{row['card_id']}  ({row['reason']})" for row in rows]
        return _emit({"ok": True, "proposals": rows}, json_output=args.json, lines=lines)
    dropped = sync.drop_proposal(target, args.proposal_id)
    if dropped is None:
        payload = {"ok": False, "error": "no such proposal", "proposal_id": args.proposal_id}
        return _emit(payload, json_output=args.json, lines=[f"error: no proposal {args.proposal_id}"], code=1)
    line = f"dropped {dropped['proposal_id']} ({dropped['scope']}/{dropped['card_id']})"
    if dropped["rebased_on"] is not None:
        line += f"; the next push writes on top of hub revision {dropped['rebased_on']}"
    else:
        line += "; no replicated hub head to rebase on, so pull first or the next push may conflict again"
    return _emit({"ok": True, **dropped}, json_output=args.json, lines=[line])


def dispatch(args: argparse.Namespace, *, transport: hubc.Transport | None = None) -> int:
    from . import memory_hub_sync as sync

    target = args.target.expanduser().resolve()
    name = args.memory_hub_command
    try:
        if name == "proposals":
            return _proposals(args, target)
        settings = hubc.load_settings(target)
        if name != "status" and not settings.enabled:
            raise hubc.HubConfigError(
                'hub memory is off; set [hub] mode = "read" or "sync" in .brigade/memory-care.toml'
            )
        missing: str | None = None
        try:
            client = hubc.client_for(settings, transport=transport) if settings.enabled else None
        except hubc.HubUnavailable as exc:
            if name != "status":
                raise
            client, missing = None, exc.reason
        if name == "status":
            report = sync.status(target, settings, client)
            if missing == "no-node-credential":
                report["hub"] = {"available": False, "reason": missing}
                report["warnings"].append(NO_NODE_CREDENTIAL)
            hub = report["hub"]
            lines = [f"memory hub: mode {report['mode']}" + (f", scope {report['scope']}" if report["scope"] else "")]
            if hub.get("available"):
                lines.append(
                    f"hub: {hub.get('cards')} card(s), seq {hub.get('seq')}, indexed {hub.get('indexed_seq')}, "
                    f"retrieval {hub.get('default_mode')}, as {hub.get('principal')}"
                )
            else:
                lines.append(f"hub: unavailable ({hub.get('reason')})")
            replica = report["replica"]
            lines.append(f"replica: {replica['cards']} card(s) at seq {replica['seq']}" if replica else "replica: none")
            if report["replica_rebuilding"]:
                lines.append("replica: rebuild in progress (search uses local cards only)")
            lines.append(f"spool: {report['spool']} entr{'y' if report['spool'] == 1 else 'ies'}")
            if report.get("spool_other_hubs"):
                lines.append(f"spool: {report['spool_other_hubs']} more for other hubs (not sent to this one)")
            lines.append(f"proposals: {len(report['proposals'])} waiting for review")
            for item in report["proposals"]:
                lines.append(f"- {item['proposal_id']}  {item['card_id']}  ({item['reason']})")
            return _emit(report, json_output=args.json, lines=lines)
        assert client is not None
        if name == "pull":
            report = sync.pull(target, client, settings)
            lines = [
                f"memory hub pull: {report['cards']} card(s) at seq {report['seq']}"
                + (" (rebuilt under a new log_epoch)" if report["rebuilt"] else "")
            ]
            return _emit(report, json_output=args.json, lines=lines)
        if name == "push":
            report = sync.push(target, client, settings, yes=args.yes)
            if report.get("needs_confirmation"):
                lines = [
                    f"memory hub push: first publish to {report['scope']} would publish {report['card_count']} card(s).",
                    "Run again with --yes to publish them.",
                ]
                return _emit(report, json_output=args.json, lines=lines, code=1)
            if report.get("hub_memory") is False:
                return _emit(report, json_output=args.json, lines=[f"error: {report['error']}"], code=1)
            lines = [
                f"memory hub push: {len(report['published'])} published, {len(report['spooled'])} spooled, "
                f"{len(report['proposals'])} proposal(s), {len(report['skipped'])} skipped"
            ]
            lines += [f"- refused {item['card_id']}: {item['code']}" for item in report["refused"]]
            return _emit(report, json_output=args.json, lines=lines)
        if name == "import":
            scope = args.scope or settings.scope
            if not hubc.valid_scope(scope):
                raise hubc.HubConfigError("import needs --scope or [hub] scope")
            report = sync.import_cards(target, client, scope=str(scope), paths=list(args.paths))
            lines = [f"memory hub import into {report['scope']}:"]
            for item in report["results"]:
                detail = item.get("code") or item.get("reason") or item.get("revision") or ""
                lines.append(f"- {item['path']}  {item['status']}  {detail}".rstrip())
            return _emit(report, json_output=args.json, lines=lines, code=0 if report["ok"] else 1)
        scope, body = _card_scope(client, settings, args)
        fields: dict[str, Any] = {"card_id": args.card_id, "scope": scope}
        offset: int | None = args.offset
        parts: list[str] = []
        card: dict[str, Any] = {}
        first = True
        while offset is not None:
            if not first:
                body = client.call("card", offset=offset, **fields)
            first = False
            card = hubc.obj(body.get("card"))
            parts.append(str(card.get("text", "")))
            following = card.get("next_offset")
            offset = following if args.all and isinstance(following, int) and following > offset else None
            if args.all and offset is not None:
                # Pin every later chunk to the revision the first one came from.
                fields["revision"] = card.get("revision")
        if args.all:
            card = {**card, "text": "".join(parts), "next_offset": None}
        payload = {"ok": True, "card": card, "log_epoch": body.get("log_epoch")}
        return _emit(payload, json_output=args.json, lines=[str(card.get("text", ""))])
    except hubc.HubConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except hubc.HubRefusal as exc:
        return _refusal(exc, json_output=args.json)
    except hubc.HubUnavailable as exc:
        return _unavailable(exc, json_output=args.json)
    except OSError as exc:
        print(f"error: hub memory state under .brigade/memory/hub is unusable: {exc}", file=sys.stderr)
        return 2
