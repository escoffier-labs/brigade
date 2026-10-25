"""Local watch, hub notification and call-log entry points for Dot."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time
from dataclasses import replace
import hashlib

from .contracts import MAX_MESSAGE_BYTES, DotError
from .hub_inbox import HubInbox, default_subject
from .lifecycle import _load_config
from .profile import PROFILES, profile_for
from .state import BridgeState
from .watch import gaps, load, parse_time, run_watch_once


def _notify(args, runtime):
    if args.wait is not None and (args.wait < 1 or not 0 <= args.timeout <= 86400 or args.message is not None):
        return {"ok": False, "error": "invalid_request"}, 2
    with BridgeState(runtime.paths.state_dir) as state:
        hub = HubInbox(state, runtime.hub)
        if args.wait is not None:
            deadline, cursor, succeeded = time.monotonic() + args.timeout, args.wait, False
            authority = hub.capture(agent=False)
            while True:
                hub.require_authority(authority)
                try:
                    # Each poll is bounded by the CLI deadline, with at least 2 s so --timeout 0 checks once.
                    poll_deadline = max(deadline, time.monotonic() + 2.0)
                    replies, cursor = hub.wait_replies(args.wait, cursor, deadline=poll_deadline, authority=authority)
                    succeeded = True
                except DotError as error:
                    if error.code not in ("hub_unavailable", "hub_quota"):
                        raise
                    replies = []
                if replies:
                    return {"ok": True, "msg_id": args.wait, "replies": replies}, 0
                if time.monotonic() >= deadline:
                    return {
                        "ok": False,
                        "error": "reply_timeout" if succeeded else "inbox_unavailable",
                    }, 3 if succeeded else 2
                time.sleep(min(3, max(0, deadline - time.monotonic())))
        if args.sender is None:
            return {"ok": False, "error": "sender_required"}, 2
        message = args.message
        if message is None:
            if sys.stdin is None or sys.stdin.isatty():
                return {"ok": False, "error": "message_required"}, 2
            raw = sys.stdin.buffer.read(MAX_MESSAGE_BYTES + 1)
            if len(raw) > MAX_MESSAGE_BYTES:
                raise DotError("invalid_request")
            message = raw.decode("utf-8").removesuffix("\n")
        node_authority = hub.capture(agent=False)
        try:
            agent_authority = hub.capture()
        except DotError:
            agent_authority = None
        # Identical retries retain identity even after an ambiguous committed send or rotation.
        cid = hashlib.sha256(
            json.dumps(
                [
                    node_authority.hub.rstrip("/"),
                    node_authority.node_id,
                    runtime.hub.principal,
                    args.sender,
                    message,
                    args.thread,
                    args.priority,
                    args.subject or default_subject(message, args.kind, args.sender),
                    args.ref,
                    args.kind,
                ],
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:32]
        if agent_authority is not None:
            node_authority = replace(node_authority, companions=(agent_authority,))
        hub.require_authority(node_authority)
        row = hub.notify(
            args.sender,
            message,
            args.thread,
            args.priority,
            args.subject,
            args.ref,
            args.kind,
            cid,
            node_authority=node_authority,
            agent_authority=agent_authority,
        )
        hub.require_authority(node_authority)
        pending = 0
        if agent_authority is not None:
            hub.require_authority(agent_authority)
            pending = state.hub_messages(
                unacked_only=True, limit=1, node_id=runtime.hub.node_id, scope=agent_authority.scope
            )["unacked_total"]
            hub.require_authority(agent_authority)
        hub.require_authority(node_authority)
        result = {"ok": True, "id": row["id"] if row else None, "unacked": pending}
        if row is None:
            result.update(queued=True, client_id=cid)
        return result, 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m brigade.dot", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    watch = commands.add_parser("watch", allow_abbrev=False)
    watch.add_argument("--dry-run", action="store_true")
    notify = commands.add_parser("notify", allow_abbrev=False)
    notify.add_argument("--from", dest="sender")
    notify.add_argument("--thread")
    notify.add_argument("--wait", type=int)
    notify.add_argument("--timeout", type=int, default=600)
    notify.add_argument("--priority", choices=("urgent", "normal"), default="normal")
    notify.add_argument("--subject")
    notify.add_argument("--ref")
    notify.add_argument("--kind", choices=("message", "question", "completion"), default="message")
    notify.add_argument("message", nargs="?")
    calls = commands.add_parser("calls", allow_abbrev=False)
    calls.add_argument("--gaps", action="store_true")
    calls.add_argument("--gap-minutes", type=float, default=10)
    calls.add_argument("--since")
    calls.add_argument("--tool")
    calls.add_argument("--limit", type=int, default=200)
    calls.add_argument("--json", action="store_true")
    for command in (watch, notify, calls):
        command.add_argument("--target", type=Path, required=True)
        command.add_argument("--pack", choices=tuple(PROFILES), default="dot")
    args = parser.parse_args(argv)
    try:
        if args.command == "watch":
            extra = {} if args.pack == "dot" else {"pack_id": args.pack}
            value = run_watch_once(args.target, dry_run=args.dry_run, **extra)
            code = 0 if value["ok"] else 2 if "error" in value else 1
        else:
            if args.pack == "dot":
                _, runtime = _load_config(args.target)
            else:
                _, runtime = _load_config(args.target, profile=profile_for(args.pack))
            if args.command == "notify":
                value, code = _notify(args, runtime)
            else:
                if args.limit < 0 or args.gap_minutes < 0:
                    parser.error("--limit and --gap-minutes must be non-negative")
                since = parse_time(args.since) if args.since else None
                rows = load(
                    runtime.paths.state_dir, since, args.tool, None if args.gaps or not args.limit else args.limit
                )
                value, code = gaps(rows, args.gap_minutes) if args.gaps else rows, 0
                if not args.json:
                    if args.gaps:
                        print(f"{value['calls']} calls; {value['gap_count']} gaps > {args.gap_minutes:g} min")
                        for gap in value["gaps"]:
                            print(f"{gap['from']} -> {gap['to']} {gap['minutes']} min")
                    else:
                        for row in rows:
                            print(
                                "  ".join(
                                    [
                                        row["ts"],
                                        row["tool"],
                                        "ok" if row["ok"] else "ERR",
                                        f"{row['duration_ms']}ms",
                                        row["request_id"] or "-",
                                        row["arg_summary"] or "-",
                                        row["error_code"] or "-",
                                        *([row["client_name"]] if row.get("client_name") else []),
                                    ]
                                )
                            )
                    return 0
    except (DotError, OSError, ValueError, sqlite3.Error) as error:
        name = "call_log_unavailable" if args.command == "calls" else "inbox_unavailable"
        if isinstance(error, DotError) and error.code == "unsupported_on_windows":
            name = error.code
        elif args.command == "notify" and isinstance(error, DotError):
            name = {"invalid_request": "invalid_request", "hub_quota": "inbox_full"}.get(error.code, name)
        value, code = {"ok": False, "error": name}, 2
    print(json.dumps(value, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
