"""One-shot wake watcher: poll open requests and POST a wake to the calling bot's routine."""

from __future__ import annotations

import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..grokbot_feed import _NoRedirect
from ..fleet_client_messages import MESSAGE_DRAIN_DEADLINE_SECONDS
from ..hub_messages import HubMessageError, call_message
from . import contracts as c
from .fleet import CALL_TIMEOUT, FleetCallError, FleetTimeout
from .ledger import HUB_PENDING_LIMIT, Ledger, wake_target_id
from .inbox import HubInbox
from .runtime_config import Runtime
from . import runtime_config
from .runtime_config import read_owner_file

WAKE_SCHEMA = "brigade.grokbot.t3-dispatch-wake.v1"
WAKE_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?/[A-Za-z0-9._~/%-]*$")
MAX_WAKE_BYTES = 16_384
MAX_KEY_BYTES = 1_024
MAX_TARGETS = 4
POST_TIMEOUT = 8
# The watcher unit's TimeoutStartSec. The budget is checked only before each row starts, so reserve the
# slowest row (a status call plus a post to every target) and startup and shutdown time.
UNIT_TIMEOUT_SECONDS = 300
SHUTDOWN_MARGIN_SECONDS = 30
FLUSH_LOCK_TIMEOUT_SECONDS = 1.0
PASS_BUDGET_SECONDS = float(UNIT_TIMEOUT_SECONDS - CALL_TIMEOUT - MAX_TARGETS * POST_TIMEOUT - SHUTDOWN_MARGIN_SECONDS)
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
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        return 0


def _append_log(path: Path, record: dict[str, Any]) -> None:
    runtime_config.require_posix_storage()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _wake_hub_inbox(
    *,
    runtime: Runtime,
    ledger: Ledger,
    wake_targets: dict[str, list[dict[str, str]]],
    log_path: Path,
    clock: Callable[[], datetime],
    post: Post,
    summary: dict[str, int],
) -> None:
    if runtime.hub_token_file is None:
        return
    summary["checked"] += 1
    hub = HubInbox(token_file=str(runtime.hub_token_file))
    authority = hub.capture()
    scope = authority.scope
    targets = wake_targets.get(runtime.hub_wake_target, [])
    if not targets:
        raise c.T3DispatchError("invalid_request")
    for target in targets:
        target_id = wake_target_id(target["url"])
        pending = ledger.pending_hub_wake(scope, target_id)
        after = max(ledger.hub_wake_cursor(scope, target_id), pending[-1]["id"] if pending else 0)
        if len(pending) < HUB_PENDING_LIMIT:
            try:
                # Each endpoint discovers independently, so a failed endpoint can retain its
                # bounded batch without blocking healthy endpoints at the same old ids.
                for page, _ in hub.pages(after=after, authority=authority):
                    if any(len(row["sender"].encode("utf-8")) > 256 for row in page):
                        raise c.T3DispatchError("hub-error")
                    hub.require_authority(authority)
                    ledger.queue_hub_wake(scope, target_id, page)
                    pending = ledger.pending_hub_wake(scope, target_id)
                    if len(pending) >= HUB_PENDING_LIMIT:
                        break
            except Exception as error:
                if isinstance(error, HubMessageError) and error.code in {
                    "hub_credential_changed",
                    "hub_credential_invalid",
                }:
                    raise
                summary["failed"] += 1
        # A failed POST remains retryable even if the Hub now omits acknowledged messages
        # or a later page fetch failed. No message content is retained in the ledger.
        pending = ledger.pending_hub_wake(scope, target_id)
        if not pending:
            continue
        body = {
            "kind": "hub-inbox",
            "count": len(pending),
            "latest_id": pending[-1]["id"],
            "sender_ids": sorted({row["sender"] for row in pending}),
        }
        try:
            key = read_key(target["key_file"])
            hub.require_authority(authority)
            code = post(target["url"], key, body)
        except Exception as error:
            if isinstance(error, HubMessageError) and error.code in {
                "hub_credential_changed",
                "hub_credential_invalid",
            }:
                raise
            code = 0
        hub.require_authority(authority)
        if code == 200:
            ledger.mark_hub_notified(scope, target_id, body["latest_id"])
            summary["woken"] += 1
        else:
            summary["failed"] += 1
        try:
            _append_log(log_path, {"at": clock().isoformat(timespec="seconds"), **body, "statuses": [code]})
        except OSError:
            pass


def run_watch_once(
    *,
    runner: Any,
    runtime: Runtime | None = None,
    ledger: Ledger,
    wake_targets: dict[str, list[dict[str, str]]],
    log_path: Path,
    now: Callable[[], datetime] | None = None,
    post: Post = post_wake,
    budget_seconds: float = PASS_BUDGET_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, int]:
    clock = now or (lambda: datetime.now(timezone.utc))
    summary = {"checked": 0, "woken": 0, "failed": 0}
    started = monotonic()
    if runtime is not None:
        try:
            _wake_hub_inbox(
                runtime=runtime,
                ledger=ledger,
                wake_targets=wake_targets,
                log_path=log_path,
                clock=clock,
                post=post,
                summary=summary,
            )
        except Exception:
            # Never let Hub credentials, outages or malformed pages starve request notifications.
            summary["failed"] += 1
            try:
                _append_log(
                    log_path, {"at": clock().isoformat(timespec="seconds"), "kind": "hub-inbox", "error": "hub-error"}
                )
            except OSError:
                pass
    rows = ledger.pending_notifications()
    for index, row in enumerate(rows):
        if monotonic() - started >= budget_seconds:
            # Leave the rest for the next tick; the ordering puts them first next time.
            summary["deferred"] = len(rows) - index
            break
        state = row["last_state"]
        delivered: set[str] = set()
        wake_attempt_recorded = False
        try:
            notified = row["notified_state"]
            revision = row["state_revision"]
            status_exhausted = state == "unknown" and row["status_failures"] >= MAX_ATTEMPTS
            if state not in c.TERMINAL_STATES and (not status_exhausted or ledger.status_poll_due(row["request_id"])):
                summary["checked"] += 1
                status_failed = False
                ledger.mark_polled(row["request_id"])
                try:
                    result = runner.status(row["request_id"])
                except (FleetCallError, FleetTimeout):
                    summary["failed"] += 1
                    failures = ledger.record_status_poll(row["request_id"], failed=True)
                    if failures < MAX_ATTEMPTS:
                        continue
                    status_failed = True
                    ledger.defer_status_poll(row["request_id"], 14)
                    new_state = "unknown"
                else:
                    new_state = result.get("state") if isinstance(result.get("state"), str) else "unknown"
                if new_state != state or not status_failed:
                    applied = ledger.update_state(
                        row["request_id"],
                        new_state,
                        clock(),
                        reset_status_failures=not status_failed,
                        expected_revision=row["state_revision"],
                    )
                    if not applied:
                        # The listener recorded a newer state while status was in flight; handle it next tick.
                        continue
                    revision += 1
                if new_state != state:
                    state = new_state
                    notified = None
                    row["delivered_targets"] = "[]"
            if state not in c.NOTIFY_STATES or notified == state:
                continue
            targets = wake_targets.get(row["caller"]) or wake_targets.get("default") or []
            body = {
                "request_id": row["request_id"],
                "state": state,
                "repository": row["repository"],
                "title": row["title"],
            }
            statuses: list[int] = []
            delivered = set(json.loads(row["delivered_targets"]))
            target_ids = {wake_target_id(target["url"]) for target in targets}
            superseded = False
            for target in targets:
                target_id = wake_target_id(target["url"])
                if target_id in delivered:
                    continue
                current = ledger.get(row["request_id"])
                if current is None or current["state_revision"] != revision:
                    # A follow-up or status call moved the row on since it was read; never wake for the old state.
                    superseded = True
                    break
                try:
                    key = read_key(target["key_file"])
                    code = post(target["url"], key, body)
                except c.T3DispatchError:
                    code = 0
                statuses.append(code)
                if code == 200:
                    delivered.add(target_id)
            if superseded:
                continue
            attempts = ledger.record_wake_attempt(row["request_id"], state, delivered)
            wake_attempt_recorded = True
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
        except Exception:
            # A broken row must not starve later requests or bypass retry limits.
            summary["failed"] += 1
            if state in c.NOTIFY_STATES:
                if not wake_attempt_recorded:
                    attempts = ledger.record_wake_attempt(row["request_id"], state, delivered)
                if attempts >= MAX_ATTEMPTS:
                    ledger.mark_notified(row["request_id"], state, clock())
            else:
                ledger.record_status_poll(row["request_id"], failed=True)
    if runtime is not None and runtime.enabled and runtime.hub_token_file is not None:
        # Request wakes run first: a competing sender's spool lock must not hold them up.
        # Reserve the existing network-drain deadline and shutdown margin before waiting.
        remaining = (
            UNIT_TIMEOUT_SECONDS - (monotonic() - started) - MESSAGE_DRAIN_DEADLINE_SECONDS - SHUTDOWN_MARGIN_SECONDS
        )
        if remaining <= 0:
            summary["failed"] += 1
        else:
            try:
                call_message(
                    "flush_credential",
                    token_file=str(runtime.hub_token_file),
                    lock_timeout=min(FLUSH_LOCK_TIMEOUT_SECONDS, remaining),
                )
            except Exception:
                summary["failed"] += 1
    return summary
