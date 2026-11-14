"""`brigade memory hub status|pull|push|import|card|proposals` (S14)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import memory_hub_client as hubc
from .inbox_lock import InboxLockTimeout

COMMANDS = ("status", "pull", "push", "import", "card", "proposals", "mirror")
# Another push, pull or proposal change holds the hub state lock (EX_TEMPFAIL).
EXIT_BUSY = 75
BUSY = "another memory hub push or pull is running in this workspace; try again when it finishes"


def register(memory_sub: argparse._SubParsersAction) -> None:
    hub = memory_sub.add_parser(
        "hub", help="Read and publish hub memory: status, pull the replica, push local cards, import, read a card."
    )
    commands = hub.add_subparsers(dest="memory_hub_command", metavar="<memory-hub-command>", required=True)
    helps = {
        "mirror": "Export readable hub cards as a disposable one-way Markdown mirror.",
        "status": "Show the hub memory mode, the hub's memory status, the replica, the spool and open proposals.",
        "pull": "Follow the hub log into this host's replica; a changed log_epoch rebuilds it.",
        "push": 'Publish changed local cards to the configured scope (needs [hub] mode = "sync").',
        "import": (
            "Import Markdown notes (files or folders) into hub memory: --dry-run lists what it would append, "
            "a second run appends nothing (the real run needs the import grant)."
        ),
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
                    "the other writer's text on the hub. A gone or private local card sends nothing."
                ),
            )
            choice.add_argument(
                "--keep-hub",
                dest="keep",
                action="store_const",
                const="keep-hub",
                help=(
                    "Keep the hub's text: the local card file is rewritten with the hub head and the "
                    "next push sends nothing for it. The replaced local text is kept with the record. "
                    "Refuses a private local card."
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
        if name == "mirror":
            command.add_argument("--out", type=Path, required=True, help="Destination folder for generated notes.")
            command.add_argument("--init", action="store_true", help="Initialize ownership in a non-empty folder.")
            command.add_argument("--dry-run", action="store_true", help="Report changes without writing the mirror.")
            command.add_argument(
                "--exclude-scope",
                action="append",
                default=[],
                metavar="SCOPE",
                help="Exclude an exact scope from the mirror (repeatable).",
            )
        if name in ("push", "import"):
            command.add_argument(
                "--yes",
                action="store_true",
                help=f"Confirm the first {'publish' if name == 'push' else 'import'} into this scope after reading its count.",
            )
        if name == "import":
            command.add_argument("paths", nargs="+", type=Path, help="Markdown files, or folders searched recursively.")
            command.add_argument("--scope", help="Scope to import into (default: [hub] scope).")
            command.add_argument(
                "--dry-run", action="store_true", help="List what would be appended and write nothing."
            )
            command.add_argument(
                "--update",
                action="store_true",
                help="Overwrite a card whose id exists with other text, and bring back a retired one (default: report changed or revive and keep the hub's).",
            )
            command.add_argument(
                "--exclude",
                action="append",
                default=[],
                metavar="GLOB",
                help="Skip notes matching GLOB against the root-relative path (repeatable; `*` crosses `/`).",
            )
            command.add_argument(
                "--root",
                type=Path,
                help="Folder that derived card ids are relative to (default: the nearest ancestor holding .obsidian, else the common ancestor of the arguments).",
            )
        if name == "card":
            command.add_argument("card_id", help="Card id, [a-z0-9][a-z0-9._-]{0,127}.")
            command.add_argument("--scope", help="Scope to read from; one of the [hub] read scopes.")
            command.add_argument("--offset", type=int, default=0, help="Character offset of the chunk.")
            command.add_argument("--all", action="store_true", help="Read every chunk and print the whole text.")


def _coverage_text(coverage: Any) -> str:
    """Cards with a vector over the cards that can have one. Readiness compares
    these two, so a hub at `ok` with unembeddable cards still reads full."""
    if not isinstance(coverage, dict):
        return ""
    eligible = coverage.get("eligible", coverage.get("total"))
    text = f", coverage {coverage.get('cards')}/{eligible}"
    unembeddable = coverage.get("unembeddable")
    return text + (f" ({unembeddable} unembeddable)" if unembeddable else "")


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
    "no node token is configured (BRIGADE_FLEET_NODE_TOKEN or [fleet] node_token_file, or a memory hub "
    "override: [memory_hub] in ~/.brigade/fleet.toml, or BRIGADE_MEMORY_HUB_URL with "
    "BRIGADE_MEMORY_HUB_TOKEN_FILE); hub memory never reads the shared admin token (BRIGADE_FLEET_TOKEN)"
)


def _unavailable(exc: hubc.HubUnavailable, *, json_output: bool) -> int:
    payload = {"ok": False, "hub": "unavailable", "reason": exc.reason}
    detail = f": {NO_NODE_CREDENTIAL}" if exc.reason == "no-node-credential" else ""
    if exc.reason == "no-hub-configured":
        detail = ": configure [fleet] hub_url and a node token, then [hub] mode and read_scopes"
    return _emit(
        payload, json_output=json_output, lines=[f"error: hub memory unavailable ({exc.reason}){detail}"], code=1
    )


class _CardUnavailable(hubc.HubUnavailable):
    """The hub became unavailable during a card read.

    `refused` names every scope the hub refused the card in during that read,
    which the replica never serves. `fallback` is False when the read showed
    that nothing the replica holds may stand in: `status` refused this
    credential every read grant, or the log_epoch moved under the read.
    `principal` is the principal `status` named (None when status did not
    answer), and `epochs` every log_epoch an answer in this read carried; the
    replica stands in only when it was pulled as that principal under each of
    those epochs.
    """

    def __init__(
        self,
        exc: hubc.HubUnavailable,
        refused: tuple[str, ...],
        *,
        fallback: bool,
        principal: object = None,
        epochs: tuple[str, ...] = (),
    ) -> None:
        super().__init__(exc.reason, status=exc.status)
        self.refused = refused
        self.fallback = fallback
        self.principal = principal
        self.epochs = epochs


def _card_scope(
    client: hubc.MemoryHubClient,
    settings: hubc.HubSettings,
    args: argparse.Namespace,
    refused: list[str],
    epochs: list[str],
) -> tuple[str, dict]:
    """The read scope that holds the card, and the first chunk's answer. Never a scope outside the config.

    Each scope that refuses the card is added to `refused`, and each first
    chunk's log_epoch to `epochs`; the caller keeps both for the whole read.
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
    for scope in candidates:
        try:
            body = client.call("card", card_id=args.card_id, scope=scope, offset=args.offset)
        except hubc.HubRefusal as exc:
            if len(candidates) == 1 or exc.code not in {"memory_card_not_found", "memory_grant_missing"}:
                raise
            refusal = refusal or exc
            if scope not in refused:
                refused.append(scope)
            continue
        epochs.append(str(body.get("log_epoch") or ""))
        found.append((scope, body))
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
    nothing = {
        "gone": "no readable local card holds it",
        "private": "the local card is private",
        "matches-hub": "the local card already holds the hub's text",
    }
    if dropped["keep"] == sync.DROP_KEEP_HUB:
        line += f"; the local card now holds hub revision {dropped['rebased_on']} and the next push sends nothing"
    elif dropped.get("local_card") in nothing:
        line += f"; {nothing[dropped['local_card']]}, so the next push sends nothing for it"
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

    Any HubUnavailable during the read leaves as _CardUnavailable, carrying
    every scope refused across both attempts and every chunk, the principal
    `status` named and every log_epoch an answer carried, and allowing no
    replica answer after a `status` 403 or an epoch move seen in this read.
    """
    refused: list[str] = []
    epochs: list[str] = []
    principal: object = None
    fallback = True
    try:
        try:
            status = hubc.refuse_admin(client.call("status"))
        except hubc.HubRefusal as exc:
            if not (exc.status == 403 and exc.code == "memory_grant_missing"):
                raise
            # This credential can read no scope now, so the replica speaks for none.
            fallback = False
        else:
            # What the hub says now: the replica must have been pulled as this
            # principal under this epoch to stand in for it.
            principal = status.get("principal")
            epochs.append(str(status.get("log_epoch") or ""))
            if not isinstance(principal, str) or not principal:
                fallback = False
        for attempt in (1, 2):
            scope, body = _card_scope(client, settings, args, refused, epochs)
            try:
                card, epoch = _read_chunks(client, args, scope, body)
            except _ChunkEpochMoved:
                # The replica's epoch may be the one this read just saw replaced.
                fallback = False
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
    except hubc.HubUnavailable as exc:
        raise _CardUnavailable(
            exc, tuple(refused), fallback=fallback, principal=principal, epochs=tuple(epochs)
        ) from exc
    raise AssertionError("unreachable")


def _replica_card(
    target: Path,
    client: hubc.MemoryHubClient,
    settings: hubc.HubSettings,
    args: argparse.Namespace,
    *,
    unavailable: _CardUnavailable,
) -> dict[str, Any] | None:
    """The card from this host's replica, used only when the hub is unavailable.

    The replica must have been pulled from this hub, and only heads in scopes
    both configured now and readable at that pull count. A refusal never
    reaches here, and a scope the hub refused during this read is never
    served. When the hub answered anything in this read, the replica must
    have been pulled as the principal its `status` named and under every
    log_epoch its answers carried. None when the replica does not hold the
    card or may not stand in.
    """
    from . import memory_hub_store as store

    replica = store.replica(target)
    if replica.watermark(client.identity) is None or not replica.meta.get("principal"):
        return None
    if not hubc.replica_principal_allowed(replica.meta.get("principal")):
        return None
    if unavailable.principal is not None and not replica.belongs_to(client.identity, unavailable.principal):
        return None
    if any(epoch != replica.meta.get("log_epoch") for epoch in unavailable.epochs):
        return None
    refused = unavailable.refused
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
    card: dict[str, Any] = {
        "scope": head.get("scope"),
        "card_id": args.card_id,
        "revision": head.get("revision"),
        "content_hash": head.get("content_hash"),
        "trust": head.get("trust"),
        "text": chunk,
        "next_offset": following,
        # What the hub's own read carries. A replica pulled before these were kept lacks them.
        **{name: head[name] for name in ("scan_state", "writer", "updated_at") if name in head},
    }
    from .memory_card_reads import record_content

    record_content(target, text, args.card_id)
    return {"ok": True, "card": card, "log_epoch": replica.meta.get("log_epoch"), "source": "replica"}


# The hub's chunk size for `card`.
REPLICA_CHUNK = 30_000


def dispatch(args: argparse.Namespace, *, transport: hubc.Transport | None = None) -> int:
    from . import memory_hub_sync as sync

    target = args.target.expanduser().resolve()
    name = args.memory_hub_command
    try:
        if name == "mirror":
            for scope in args.exclude_scope:
                if not hubc.valid_scope(scope):
                    raise hubc.HubConfigError(f"invalid excluded scope: {scope!r}")
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
            if "hub_source" in report:
                lines.append(
                    f"hub url: {hubc.display_url(report.get('hub_url') or '') or 'none'} (source: {report['hub_source']})"
                )
            if hub.get("available"):
                lines.append(
                    f"hub: {hub.get('cards')} card(s), seq {hub.get('seq')}, indexed {hub.get('indexed_seq')}, "
                    f"retrieval {hub.get('default_mode')}, as {hub.get('principal')}"
                )
                retrieval = hubc.obj(hub.get("retrieval"))
                if retrieval:
                    lines.append(
                        f"retrieval: {retrieval.get('mode')}, effective {retrieval.get('effective')}, "
                        f"state {retrieval.get('state')}"
                        + (f" ({retrieval['reason']})" if retrieval.get("reason") else "")
                        + _coverage_text(retrieval.get("coverage"))
                        + (f", endpoint {retrieval['endpoint_host']}" if retrieval.get("endpoint_host") else "")
                        + (
                            f", pull_private {'on' if retrieval.get('pull_private') else 'off'}"
                            if retrieval.get("pull")
                            else ""
                        )
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
        if name == "mirror":
            from . import memory_hub_mirror

            report = memory_hub_mirror.mirror(
                target,
                client,
                settings,
                args.out.expanduser(),
                init=args.init,
                dry_run=args.dry_run,
                exclude_scopes=tuple(args.exclude_scope),
            )
            lines = [f"memory hub mirror: {len(report['written'])} write(s), {len(report['deleted'])} deletion(s)"]
            lines.append("excluded scopes: " + (", ".join(report["excluded_scopes"]) or "none"))
            lines += [f"write: {name}" for name in report["written"]]
            lines += [f"delete: {name}" for name in report["deleted"]]
            lines += [f"skipped {reason}: {count}" for reason, count in report["skipped"].items()]
            return _emit(report, json_output=args.json, lines=lines)
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
                    f"{report['derived_ids']} derived id(s). Run again with --yes to publish them.",
                ]
                return _emit(report, json_output=args.json, lines=lines, code=1)
            if report.get("hub_memory") is False:
                return _emit(report, json_output=args.json, lines=[f"error: {report['error']}"], code=1)
            lines = [f"- refused {item['card_id']}: {item['code']}" for item in report["refused"]]
            lines += [
                f"- resolved {proposal_id}; its text is kept at {path}"
                for proposal_id, path in (report.get("resolved_kept") or {}).items()
            ]
            # Keep the count summary last so scheduled runs expose it in journals.
            summary = (
                f"memory hub push: {len(report['published'])} published, {len(report['spooled'])} spooled, "
                f"{len(report['proposals'])} proposal(s), {len(report['refused'])} refused, "
                f"{len(report.get('kept_local', []))} kept local, {len(report['skipped'])} skipped, "
                f"{report['derived_ids']} derived id(s)"
            )
            # _emit prints warnings after lines. Fold them in before the final summary.
            lines = lines + [f"warning: {warning}" for warning in report.get("warnings", [])] + [summary]
            return _emit(
                {**report, "warnings": []} if not args.json else report,
                json_output=args.json,
                lines=lines,
                code=0 if report["ok"] else 1,
            )
        if name == "import":
            from . import memory_hub_import as importer

            scope = args.scope or settings.scope
            if not hubc.valid_scope(scope):
                raise hubc.HubConfigError("import needs --scope or [hub] scope")
            report = importer.run(
                client,
                scope=str(scope),
                paths=list(args.paths),
                root=args.root,
                excludes=list(args.exclude),
                dry_run=args.dry_run,
                update=args.update,
                yes=args.yes,
            )
            if args.json:
                for row in importer.json_lines(report):
                    print(json.dumps(row, sort_keys=True, ensure_ascii=False))
            else:
                for line in importer.lines(report):
                    print(line)
            return 0 if report["ok"] else 1
        try:
            payload = _hub_card(client, settings, args)
        except hubc.HubUnavailable as exc:
            # Not now, rather than no: the replica may stand in, as for search,
            # unless this read showed it may not.
            if not isinstance(exc, _CardUnavailable) or not exc.fallback:
                raise
            fallback = _replica_card(target, client, settings, args, unavailable=exc)
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
