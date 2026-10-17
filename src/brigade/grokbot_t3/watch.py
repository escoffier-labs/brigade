"""One-shot wake watcher: poll open requests and POST a wake to the calling bot's routine."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..grokbot_feed import _NoRedirect
from . import contracts as c
from .fleet import FleetCallError, FleetTimeout
from .ledger import Ledger
from .runtime_config import read_owner_file

WAKE_SCHEMA = "brigade.grokbot.t3-dispatch-wake.v1"
WAKE_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?/[A-Za-z0-9._~/%-]*$")
MAX_WAKE_BYTES = 16_384
MAX_KEY_BYTES = 1_024
MAX_TARGETS = 4
POST_TIMEOUT = 8
MAX_ATTEMPTS = 30
Post = Callable[[str, str, dict[str, Any]], int]


def _bad() -> c.T3DispatchError:
    return c.T3DispatchError("invalid_request")


def load_wake_config(path: Path) -> dict[str, list[dict[str, str]]]:
    try:
        payload = json.loads(read_owner_file(path, max_bytes=MAX_WAKE_BYTES))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _bad() from exc
    if not isinstance(payload, dict) or set(payload) != {"schema", "targets"} or payload["schema"] != WAKE_SCHEMA:
        raise _bad()
    targets = payload["targets"]
    if not isinstance(targets, dict) or "default" not in targets:
        raise _bad()
    parsed: dict[str, list[dict[str, str]]] = {}
    for name, entries in targets.items():
        if not isinstance(name, str) or (name != "default" and not c.CALLER_RE.fullmatch(name)):
            raise _bad()
        if not isinstance(entries, list) or not entries or len(entries) > MAX_TARGETS:
            raise _bad()
        parsed_entries = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"url", "key_file"}:
                raise _bad()
            url, key_file = entry["url"], entry["key_file"]
            if not isinstance(url, str) or not WAKE_URL_RE.fullmatch(url):
                raise _bad()
            if not isinstance(key_file, str) or not Path(key_file).is_absolute() or "\x00" in key_file:
                raise _bad()
            parsed_entries.append({"url": url, "key_file": key_file})
        parsed[name] = parsed_entries
    return parsed


def read_key(key_file: str) -> str:
    try:
        value = read_owner_file(Path(key_file), max_bytes=MAX_KEY_BYTES).decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise _bad() from exc
    if not value or "\n" in value or "\r" in value:
        raise _bad()
    return value


def post_wake(url: str, key: str, body: dict[str, Any]) -> int:
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}", "X-Automation-Key": key},
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=POST_TIMEOUT) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError, ValueError):
        return 0


def _append_log(path: Path, record: dict[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def run_watch_once(
    *,
    runner: Any,
    ledger: Ledger,
    wake_targets: dict[str, list[dict[str, str]]],
    log_path: Path,
    now: Callable[[], datetime] | None = None,
    post: Post = post_wake,
) -> dict[str, int]:
    clock = now or (lambda: datetime.now(timezone.utc))
    summary = {"checked": 0, "woken": 0, "failed": 0}
    for row in ledger.pending_notifications():
        state = row["last_state"]
        notified = row["notified_state"]
        status_exhausted = state == "unknown" and row["status_failures"] >= MAX_ATTEMPTS
        if state not in c.TERMINAL_STATES and not status_exhausted:
            summary["checked"] += 1
            try:
                result = runner.status(row["request_id"])
            except (FleetCallError, FleetTimeout):
                summary["failed"] += 1
                failures = ledger.record_status_poll(row["request_id"], failed=True)
                if failures < MAX_ATTEMPTS:
                    continue
                new_state = "unknown"
            else:
                ledger.record_status_poll(row["request_id"], failed=False)
                new_state = result.get("state") if isinstance(result.get("state"), str) else "unknown"
            if new_state != state:
                ledger.update_state(row["request_id"], new_state, clock())
                state = new_state
                notified = None
                row["delivered_targets"] = "[]"
        if state not in c.NOTIFY_STATES or notified == state:
            continue
        targets = wake_targets.get(row["caller"]) or wake_targets.get("default") or []
        body = {"request_id": row["request_id"], "state": state, "repository": row["repository"], "title": row["title"]}
        statuses: list[int] = []
        delivered = set(json.loads(row["delivered_targets"]))
        target_ids = {json.dumps([target["url"], target["key_file"]]) for target in targets}
        for target in targets:
            target_id = json.dumps([target["url"], target["key_file"]])
            if target_id in delivered:
                continue
            try:
                key = read_key(target["key_file"])
                code = post(target["url"], key, body)
            except c.T3DispatchError:
                code = 0
            statuses.append(code)
            if code == 200:
                delivered.add(target_id)
        attempts = ledger.record_wake_attempt(row["request_id"], state, delivered)
        complete = bool(targets) and target_ids <= delivered
        gave_up = not complete and attempts >= MAX_ATTEMPTS
        record: dict[str, Any] = {
            "at": clock().isoformat(timespec="seconds"),
            "request_id": row["request_id"],
            "state": state,
            "statuses": statuses,
        }
        if gave_up:
            record["note"] = "gave-up"
        _append_log(log_path, record)
        if complete:
            ledger.mark_notified(row["request_id"], state, clock())
            summary["woken"] += 1
        else:
            if gave_up:
                ledger.mark_notified(row["request_id"], state, clock())
            summary["failed"] += 1
    return summary
