"""`brigade memory hub status|pull|push|import|card|proposals` (S14)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import memory_hub_client as hubc
from .inbox_lock import InboxLockTimeout

COMMANDS = ("status", "pull", "push", "import", "card", "proposals")
# Another push, pull or proposal change holds the hub state lock (EX_TEMPFAIL).
EXIT_BUSY = 75
BUSY = "another memory hub push or pull is running in this workspace; try again when it finishes"


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
        "card": (
            "Read one hub card's text from the [hub] read scopes, in 30,000-character chunks; "
            "while the hub is unavailable, from this host's replica."
        ),
        "proposals": "List the writes waiting for review, or drop one, keeping the local text or the hub's.",
    }
    for name in COMMANDS:
        command = commands.add_parser(name, help=helps[name])
        if name == "proposals":
            actions = command.add_subparsers(dest="memory_hub_proposals_command", metavar="<action>", required=True)
            listing = actions.add_parser("list", help="List open proposals for every hub.")
            drop = actions.add_parser(
                "drop",
                help=(
                    "Close one proposal after review. Needs the hub and a current replica (pull first). "
                    "The closed record is kept under proposals/dropped/."
                ),
            )
            drop.add_argument("proposal_id", help="Proposal id, as `proposals list` prints it.")
            choice = drop.add_mutually_exclusive_group(required=True)
            choice.add_argument(
                "--keep-local",
                dest="keep",
                action="store_const",
                const="keep-local",
                help=(
                    "Keep the local card: the hub head becomes its base, so the next push overwrites "
                    "the other writer's text on the hub."
                ),
            )
            choice.add_argument(
                "--keep-hub",
                dest="keep",
                action="store_const",
                const="keep-hub",
                help=(
                    "Keep the hub's text: the local card file is rewritten with the hub head and the "
                    "next push sends nothing for it. The replaced local text is kept with the record."
                ),
            )
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


class _CardUnavailable(hubc.HubUnavailable):
    """The hub became unavailable during a card read, after it refused the card in `refused` scopes."""

    def __init__(self, exc: hubc.HubUnavailable, refused: tuple[str, ...]) -> None:
        super().__init__(exc.reason, status=exc.status)
        self.refused = refused


def _card_scope(client: hubc.MemoryHubClient, settings: hubc.HubSettings, args: argparse.Namespace) -> tuple[str, dict]:
    """The read scope that holds the card, and the first chunk's answer. Never a scope outside the config.

    When the hub turns unavailable after it refused the card in one scope,
    the HubUnavailable carries that scope, so the replica never serves it.
    """
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
    refused: list[str] = []
    for scope in candidates:
        try:
            found.append((scope, client.call("card", card_id=args.card_id, scope=scope, offset=args.offset)))
        except hubc.HubUnavailable as exc:
            raise _CardUnavailable(exc, tuple(refused)) from exc
        except hubc.HubRefusal as exc:
            if len(candidates) == 1 or exc.code not in {"memory_card_not_found", "memory_grant_missing"}:
                raise
            refusal = refusal or exc
            refused.append(scope)
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


def _proposals(args: argparse.Namespace, target: Path, transport: hubc.Transport | None) -> int:
    from . import memory_hub_sync as sync

    if args.memory_hub_proposals_command == "list":
        rows = sync.proposal_rows(target)
        lines = [f"proposals: {len(rows)} waiting for review"]
        lines += [f"- {row['proposal_id']}  {row['scope']}/{row['card_id']}  ({row['reason']})" for row in rows]
        return _emit({"ok": True, "proposals": rows}, json_output=args.json, lines=lines)
    settings = hubc.load_settings(target)
    if not settings.enabled:
        raise hubc.HubConfigError('hub memory is off; set [hub] mode = "read" or "sync" in .brigade/memory-care.toml')
    client = hubc.client_for(settings, transport=transport)
    dropped = sync.drop_proposal(target, client, settings, args.proposal_id, keep=args.keep)
    if dropped is None:
        payload = {"ok": False, "error": "no such proposal", "proposal_id": args.proposal_id}
        return _emit(payload, json_output=args.json, lines=[f"error: no proposal {args.proposal_id}"], code=1)
    line = f"dropped {dropped['proposal_id']} ({dropped['scope']}/{dropped['card_id']})"
    if dropped["keep"] == sync.DROP_KEEP_HUB:
        line += f"; the local card now holds hub revision {dropped['rebased_on']} and the next push sends nothing"
    elif dropped["rebased_on"] is not None:
        line += f"; the next push writes the local card over hub revision {dropped['rebased_on']}"
    else:
        line += "; the hub holds no head for it, so the next push sends the local card with no base"
    lines = [line, f"the closed proposal is kept at {dropped['dropped_to']}"]
    return _emit({"ok": True, **dropped}, json_output=args.json, lines=lines)


class _ChunkEpochMoved(Exception):
    """A later chunk of `card --all` came from another log_epoch."""


def _read_chunks(
    client: hubc.MemoryHubClient, args: argparse.Namespace, scope: str, body: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """Follow `--all` from the first chunk's answer; every chunk must share its epoch and revision."""
    epoch = str(body.get("log_epoch") or "")
    fields: dict[str, Any] = {"card_id": args.card_id, "scope": scope}
    card = hubc.obj(body.get("card"))
    parts = [str(card.get("text", ""))]
    offset: int | None = args.offset
    while args.all:
        following = card.get("next_offset")
        if not (isinstance(following, int) and offset is not None and following > offset):
            break
        offset = following
        # Pin every later chunk to the revision the first one came from. A
        # restore reissues revision numbers, so the epoch must match too.
        fields["revision"] = card.get("revision")
        later = client.call("card", offset=offset, **fields)
        if str(later.get("log_epoch") or "") != epoch:
            raise _ChunkEpochMoved
        card = hubc.obj(later.get("card"))
        parts.append(str(card.get("text", "")))
    if args.all:
        card = {**card, "text": "".join(parts), "next_offset": None}
        expected = card.get("content_hash")
        if args.offset == 0 and isinstance(expected, str) and expected and hubc.content_hash(card["text"]) != expected:
            raise hubc.HubRefusal(
                502,
                {
                    "code": "memory_card_hash_mismatch",
                    "error": "the joined chunks do not hash to the card's content_hash; read it again",
                },
            )
    return card, epoch


def _hub_card(client: hubc.MemoryHubClient, settings: hubc.HubSettings, args: argparse.Namespace) -> dict[str, Any]:
    """The card from the hub. A restore between chunks restarts the read once, then refuses.

    The hub's `status` is read first so an admin credential in the node slot
    is refused before any card is read. A credential that cannot read status
    cannot read a card either; the card call then carries the hub's refusal.
    """
    try:
        hubc.refuse_admin(client.call("status"))
    except hubc.HubRefusal as exc:
        if not (exc.status == 403 and exc.code == "memory_grant_missing"):
            raise
    for attempt in (1, 2):
        scope, body = _card_scope(client, settings, args)
        try:
            card, epoch = _read_chunks(client, args, scope, body)
        except _ChunkEpochMoved:
            if attempt == 2:
                raise hubc.HubRefusal(
                    409,
                    {
                        "code": "memory_epoch_changed",
                        "error": "the hub's log_epoch changed while the card was read in chunks; read it again",
                    },
                ) from None
            continue
        return {"ok": True, "card": card, "log_epoch": epoch, "source": "hub"}
    raise AssertionError("unreachable")


def _replica_card(
    target: Path,
    client: hubc.MemoryHubClient,
    settings: hubc.HubSettings,
    args: argparse.Namespace,
    *,
    refused: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """The card from this host's replica, used only when the hub is unavailable.

    The replica must have been pulled from this hub, and only heads in scopes
    both configured now and readable at that pull count. A refusal never
    reaches here, and a scope the hub refused during this read (`refused`)
    is never served. None when the replica does not hold the card.
    """
    from . import memory_hub_store as store

    replica = store.replica(target)
    if replica.watermark(client.identity) is None or not replica.meta.get("principal"):
        return None
    scopes = [scope for scope in ((args.scope,) if args.scope else settings.read_scopes) if scope not in refused]
    found = [
        head
        for scope in scopes
        if (head := replica.readable_heads(settings.read_scopes).get(store.Replica.key(scope, args.card_id)))
    ]
    if len(found) > 1:
        names = [str(head["scope"]) for head in found]
        raise hubc.HubRefusal(
            409,
            {
                "code": "memory_card_ambiguous",
                "error": f"the card is in more than one read scope; pass --scope ({', '.join(names)})",
                "scopes": names,
            },
        )
    if not found:
        return None
    head = found[0]
    text = str(head.get("text", ""))
    if args.all:
        chunk, following = text[args.offset :], None
    else:
        chunk = text[args.offset : args.offset + REPLICA_CHUNK]
        end = args.offset + len(chunk)
        following = end if end < len(text) else None
    card = {
        "scope": head.get("scope"),
        "card_id": args.card_id,
        "revision": head.get("revision"),
        "content_hash": head.get("content_hash"),
        "trust": head.get("trust"),
        "text": chunk,
        "next_offset": following,
    }
    return {"ok": True, "card": card, "log_epoch": replica.meta.get("log_epoch"), "source": "replica"}


# The hub's chunk size for `card`.
REPLICA_CHUNK = 30_000


def dispatch(args: argparse.Namespace, *, transport: hubc.Transport | None = None) -> int:
    from . import memory_hub_sync as sync

    target = args.target.expanduser().resolve()
    name = args.memory_hub_command
    try:
        if name == "proposals":
            return _proposals(args, target, transport)
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
            lines += [
                f"- resolved {proposal_id}; its text is kept at {path}"
                for proposal_id, path in (report.get("resolved_kept") or {}).items()
            ]
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
        try:
            payload = _hub_card(client, settings, args)
        except hubc.HubUnavailable as exc:
            # Not now, rather than no: the replica may stand in, as for search.
            refused = exc.refused if isinstance(exc, _CardUnavailable) else ()
            fallback = _replica_card(target, client, settings, args, refused=refused)
            if fallback is None:
                raise
            warning = f"hub unavailable ({exc.reason}); answered from this host's replica"
            payload = {**fallback, "retrieval": {"hub": "unavailable", "degraded": True, "reason": exc.reason}}
            if args.json:
                payload["warnings"] = [warning]
            else:
                # stderr, so `card --all > file` holds only the card text.
                print(f"warning: {warning}", file=sys.stderr)
        return _emit(payload, json_output=args.json, lines=[str(payload["card"].get("text", ""))])
    except InboxLockTimeout:
        print(f"error: {BUSY}", file=sys.stderr)
        return EXIT_BUSY
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
