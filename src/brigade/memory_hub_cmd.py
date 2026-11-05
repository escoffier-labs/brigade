"""`brigade memory hub status|pull|push|import|card` (S14)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import memory_hub_client as hubc

COMMANDS = ("status", "pull", "push", "import", "card")


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
        "card": "Read one hub card's text, in 30,000-character chunks.",
    }
    for name in COMMANDS:
        command = commands.add_parser(name, help=helps[name])
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
            command.add_argument("--scope", help="Scope, when the id is readable in more than one.")
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


def _unavailable(exc: hubc.HubUnavailable, *, json_output: bool) -> int:
    payload = {"ok": False, "hub": "unavailable", "reason": exc.reason}
    return _emit(payload, json_output=json_output, lines=[f"error: hub memory unavailable ({exc.reason})"], code=1)


def dispatch(args: argparse.Namespace, *, transport: hubc.Transport | None = None) -> int:
    from . import memory_hub_sync as sync

    target = args.target.expanduser().resolve()
    name = args.memory_hub_command
    try:
        settings = hubc.load_settings(target)
        if name != "status" and not settings.enabled:
            raise hubc.HubConfigError(
                'hub memory is off; set [hub] mode = "read" or "sync" in .brigade/memory-care.toml'
            )
        try:
            client = hubc.client_for(settings, transport=transport) if settings.enabled else None
        except hubc.HubUnavailable:
            if name != "status":
                raise
            client = None
        if name == "status":
            report = sync.status(target, settings, client)
            hub = report["hub"]
            lines = [f"memory hub: mode {report['mode']}" + (f", scope {report['scope']}" if report["scope"] else "")]
            if hub.get("available"):
                lines.append(
                    f"hub: {hub.get('cards')} card(s), seq {hub.get('seq')}, indexed {hub.get('indexed_seq')}, "
                    f"retrieval {hub.get('default_mode')}"
                )
            else:
                lines.append(f"hub: unavailable ({hub.get('reason')})")
            replica = report["replica"]
            lines.append(f"replica: {replica['cards']} card(s) at seq {replica['seq']}" if replica else "replica: none")
            if report["replica_rebuilding"]:
                lines.append("replica: rebuild in progress (search uses local cards only)")
            lines.append(f"spool: {report['spool']} entr{'y' if report['spool'] == 1 else 'ies'}")
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
        fields: dict[str, Any] = {"card_id": args.card_id}
        if args.scope:
            fields["scope"] = args.scope
        offset: int | None = args.offset
        parts: list[str] = []
        card: dict[str, Any] = {}
        body: dict[str, Any] = {}
        while offset is not None:
            body = client.call("card", offset=offset, **fields)
            card = hubc.obj(body.get("card"))
            parts.append(str(card.get("text", "")))
            following = card.get("next_offset")
            offset = following if args.all and isinstance(following, int) and following > offset else None
            if args.all and offset is not None:
                # Pin every later chunk to the revision the first one came from.
                fields.update(scope=card.get("scope"), revision=card.get("revision"))
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
        print(f"error: hub memory state under .brigade/memory is unusable: {exc}", file=sys.stderr)
        return 2
