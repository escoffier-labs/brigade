"""Read-only Dot inbox health checks with persistent notification deduplication."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from typing import Any

from .access_auth import METADATA_PATH
from .hub_inbox import sender_name
from .lifecycle import DotListenerConfig, _base, _load_config, _probe
from .runtime_config import Runtime

STALE_AFTER = timedelta(minutes=30)
REALERT_AFTER = timedelta(hours=2)
IDLE_AFTER = timedelta(hours=3)
KEEP_STATE = timedelta(days=7)
UNANSWERED_WAKE_AFTER = timedelta(minutes=30)
STATE_FILE = "inbox-watch.json"


def parse_time(value):
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def age_text(delta):
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes // 60}h {minutes % 60:02d}m"


def table_exists(connection, name):
    return (
        connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None
    )


def route_alerts(now, failed, last_wake, last_call, state):
    route = state.setdefault("route", {})
    alerts, notes = [], []
    if failed:
        key = ",".join(failed)
        previous = route.get("last_alert")
        if route.get("last_key") != key or previous is None or now - parse_time(previous) >= REALERT_AFTER:
            alerts.append(
                {
                    "kind": "route_down",
                    "key": key,
                    "title": "Dot bridge: route down",
                    "text": "Failing probe(s): " + key + ". Dot cannot reach the bridge while this lasts.",
                }
            )
        else:
            notes.append("route_down deduped")
    if (
        last_wake is not None
        and now - last_wake > UNANSWERED_WAKE_AFTER
        and (last_call is None or parse_time(last_call) < last_wake)
    ):
        key = last_wake.isoformat()
        if route.get("wake") == key:
            notes.append("wake_unanswered already alerted")
        else:
            alerts.append(
                {
                    "kind": "wake_unanswered",
                    "key": key,
                    "title": "Dot bridge: wake unanswered",
                    "text": f"A wake went out at {key} and Dot has made no bridge call since. "
                    "Check Dot's ChatGPT connector (MCP errors such as -32603) and the tunnel.",
                }
            )
    return alerts, notes


def load_state(path):
    try:
        value = json.loads(path.read_text())
        if isinstance(value, dict):
            result = {"inbox": dict(value.get("inbox") or {}), "idle": dict(value.get("idle") or {})}
            for key in ("sync", "route"):
                if key in value:
                    result[key] = dict(value[key] or {})
            return result
    except (OSError, ValueError):
        pass
    return {"inbox": {}, "idle": {}}


def save_state(path, state):
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(state, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def decide(now, unacked, last_call, open_requests, state):
    alerts, notes = [], []
    stale = [row for row in unacked if now - parse_time(row[1]) >= STALE_AFTER]
    if stale:
        message_id, created_at, sender, subject = stale[0]
        key = str(message_id)
        previous = state["inbox"].get(key)
        text = (
            f"Dot inbox: {len(stale)} unacked message(s) older than 30 min "
            f"({len(unacked)} unacked total). Oldest #{message_id} "
            f'"{(subject or "(no subject, from " + sender + ")")[:120]}", '
            f"age {age_text(now - parse_time(created_at))}."
        )
        if previous is not None and now - parse_time(previous) < REALERT_AFTER:
            notes.append(f"stale_inbox deduped for #{message_id} (last alert {previous})")
        else:
            alerts.append({"kind": "stale_inbox", "key": key, "title": "Dot bridge: stale inbox", "text": text})
    pending = open_requests > 0 or bool(unacked)
    if last_call is None:
        notes.append("dot_idle skipped: no call history")
    elif now - parse_time(last_call) > IDLE_AFTER and pending:
        if state["idle"].get("last_call") == last_call:
            notes.append(f"dot_idle already alerted for last call {last_call}")
        else:
            alerts.append(
                {
                    "kind": "dot_idle",
                    "key": last_call,
                    "title": "Dot bridge: Dot idle",
                    "text": (
                        f"No Dot bridge call for {age_text(now - parse_time(last_call))} "
                        f"(last {last_call}) while {open_requests} fleet request(s) are "
                        f"unterminated and {len(unacked)} inbox message(s) are unacked."
                    ),
                }
            )
    return alerts, notes, len(stale)


def read_bridge(runtime: Runtime) -> tuple[list[Any], str | None, int, str | None, bool, datetime | None]:
    """Use SQLite's read-only mode. The watcher never migrates or initializes state."""
    url = (runtime.paths.state_dir / "bridge.sqlite3").as_uri() + "?mode=ro"
    with sqlite3.connect(url, uri=True, timeout=5) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM hub_inbox WHERE acked_at IS NULL ORDER BY created_at,id")
        unacked = [(r["id"], r["created_at"], sender_name(dict(r), runtime.hub.node_id), r["subject"]) for r in rows]
        last_call = connection.execute("SELECT MAX(ts) FROM call_log").fetchone()[0]
        pending = connection.execute("SELECT COUNT(*) FROM tracked_requests WHERE completed=0").fetchone()[0]
        hub = dict(connection.execute("SELECT key,value FROM hub_state"))
        value = connection.execute("SELECT MAX(timestamp) FROM wake_attempts").fetchone()[0]
    credential = any(hub.get(key) == "hub_credential_invalid" for key in ("last_sync_error", "last_reconcile_error"))
    wake = None if value is None else datetime.fromtimestamp(value, timezone.utc)
    return unacked, last_call, pending, hub.get("last_sync_ok_at"), credential, wake


def probe_route(config: DotListenerConfig, runtime: Runtime) -> list[str]:
    verifier_base = runtime.auth.resource.rsplit("/", 1)[0]
    failed = []
    for name, url in (("origin", _base(config) + METADATA_PATH), ("edge", verifier_base + METADATA_PATH)):
        status, body = _probe(url)
        if status != 200 or not isinstance(body, dict) or body.get("resource") != runtime.auth.resource:
            failed.append(name)
    return failed


def send(command: tuple[str, ...], alert: dict[str, Any]) -> int | None:
    payload = json.dumps(
        {
            "title": alert["title"],
            "body": alert["text"],
            "level": "warn",
            "source": "dot-inbox-watch",
            "tags": ["dot", "dot-bridge"],
        }
    )
    try:
        return subprocess.run(
            list(command),
            input=payload.encode(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        ).returncode
    except (OSError, subprocess.TimeoutExpired):
        return None


def run_watch_once(target: Path, *, now: datetime | None = None, dry_run: bool = False) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    try:
        config, runtime = _load_config(target)
        unacked, last_call, pending, sync_at, credential, last_wake = read_bridge(runtime)
    except Exception:
        return {"ok": False, "error": "bridge_state_unavailable"}
    state_path = runtime.paths.state_dir / STATE_FILE
    state = load_state(state_path)
    alerts, notes, stale = decide(now, unacked, last_call, pending, state)
    sync = state.setdefault("sync", {})
    key = "last_credential_alert" if credential else "last_alert"
    if credential or sync_at is None or now - parse_time(sync_at) > timedelta(minutes=15):
        previous = sync.get(key)
        if previous is None or now - parse_time(previous) >= REALERT_AFTER:
            kind = "hub_credential_invalid" if credential else "hub_sync_stale"
            text = (
                "The hub message token file is unusable. Inbox sync is failing until it is repaired. "
                if credential
                else "No successful hub inbox sync in 15 minutes. "
            )
            alerts.append(
                {
                    "kind": kind,
                    "key": key,
                    "title": "Dot bridge: " + kind.replace("_", " "),
                    "text": text + "Last successful sync: " + (sync_at or "never"),
                }
            )
        else:
            notes.append(("hub_credential_invalid" if credential else "hub_sync_stale") + " deduped")
    more, more_notes = route_alerts(now, probe_route(config, runtime), last_wake, last_call, state)
    alerts += more
    notes += more_notes
    failed, sent = False, False
    for alert in alerts:
        if dry_run or runtime.notify_command is None:
            alert["result"] = "would_send" if dry_run else "skipped"
            continue
        code = send(runtime.notify_command, alert)
        alert["result"] = "sent" if code == 0 else f"send_failed:{code}"
        if code != 0:
            failed = True
            continue
        sent = True
        kind = alert["kind"]
        if kind == "stale_inbox":
            state["inbox"][alert["key"]] = now.isoformat()
        elif kind in ("hub_sync_stale", "hub_credential_invalid"):
            state["sync"]["last_credential_alert" if kind == "hub_credential_invalid" else "last_alert"] = (
                now.isoformat()
            )
        elif kind == "route_down":
            state["route"].update(last_alert=now.isoformat(), last_key=alert["key"])
        elif kind == "wake_unanswered":
            state["route"]["wake"] = alert["key"]
        else:
            state["idle"]["last_call"] = alert["key"]
    if sent:
        cutoff = now - KEEP_STATE
        state["inbox"] = {k: v for k, v in state["inbox"].items() if parse_time(v) >= cutoff}
        save_state(state_path, state)
    return {
        "ok": not failed,
        "dry_run": dry_run,
        "now": now.isoformat(),
        "unacked": len(unacked),
        "stale": stale,
        "last_call": last_call,
        "open_requests": pending,
        "alerts": alerts,
        "notes": notes,
    }


COLUMNS = ("id", "ts", "tool", "ok", "duration_ms", "request_id", "arg_summary", "error_code")


def load(state_dir, since, tool, limit):
    connection = sqlite3.connect(f"file:{state_dir / 'bridge.sqlite3'}?mode=ro", uri=True, timeout=5)
    try:
        clauses, params = [], []
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since.isoformat())
        if tool is not None:
            clauses.append("tool = ?")
            params.append(tool)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        query = f"SELECT {','.join(COLUMNS)} FROM call_log{where} ORDER BY id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        rows = [dict(zip(COLUMNS, row, strict=True)) for row in connection.execute(query, params)]
    finally:
        connection.close()
    rows.reverse()
    for row in rows:
        row["ok"] = bool(row["ok"])
    return rows


def gaps(rows, minutes):
    found = []
    for before, after in zip(rows, rows[1:], strict=False):
        idle = (parse_time(after["ts"]) - parse_time(before["ts"])).total_seconds() / 60
        if idle > minutes:
            found.append(
                {
                    "from": before["ts"],
                    "to": after["ts"],
                    "minutes": round(idle, 1),
                    "last_tool_before": before["tool"],
                    "first_tool_after": after["tool"],
                }
            )
    span = ((parse_time(rows[-1]["ts"]) - parse_time(rows[0]["ts"])).total_seconds() / 60) if rows else 0
    return {
        "calls": len(rows),
        "first": rows[0]["ts"] if rows else None,
        "last": rows[-1]["ts"] if rows else None,
        "span_minutes": round(span, 1),
        "gap_minutes": minutes,
        "gap_count": len(found),
        "idle_minutes": round(sum(gap["minutes"] for gap in found), 1),
        "gaps": found,
    }
