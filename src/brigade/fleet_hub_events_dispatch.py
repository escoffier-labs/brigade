"""Signed webhook delivery for Fleet Hub MCP Events.

One background thread owns every database write. Each tick it records expired
claims, fans new events out to matching subscriptions (one delivery row per
event and subscription), re-checks that the subscriber is still allowed to see
the event, and posts the due deliveries on a small thread pool that does
network I/O only. Failures retry with exponential backoff and a fresh
timestamp and signature; the event id never changes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import fleet_hub_events as events
from .mcp_events import CallbackPolicy, post_webhook, signed_headers

Post = Callable[[str, bytes, Mapping[str, str], CallbackPolicy], "tuple[int, bytes]"]

TICK_SECONDS = 0.5
BATCH = 20
WORKERS = 4
MAX_ATTEMPTS = 8
BACKOFF_BASE_SECONDS = 5
BACKOFF_CAP_SECONDS = 3600
MAX_FAILED_STREAK = 10
HOURLY_DELIVERY_CEILING = 120
EVENT_RETENTION_SECONDS = 86_400
CLAIM_SWEEP_LIMIT = 100
_LOG = logging.getLogger("brigade.fleet_hub_events")


def sweep_expired_claims(conn: sqlite3.Connection, now: float) -> None:
    """Emit claim.expired for leases that ran out; the prune path in claims covers the rest."""
    if not events.current().enabled or not events.has_subscribers(conn, events.CLAIM_EXPIRED):
        return
    rows = conn.execute(
        "SELECT target, owner_node, holder_token, expires_at FROM claims WHERE expires_at<=? "
        "ORDER BY expires_at DESC LIMIT ?",
        (now, CLAIM_SWEEP_LIMIT),
    ).fetchall()
    for target, owner, holder, expires_at in rows:
        events.record_claim_expired(conn, target, owner, holder, expires_at)


def _fan_out(conn: sqlite3.Connection, now: float) -> list[dict[str, Any]]:
    """Create delivery rows for new events, drop dead state, and return the due deliveries."""
    conn.execute("DELETE FROM fleet_event_subscriptions WHERE expires_at IS NOT NULL AND expires_at<=?", (now,))
    # A revoked principal keeps nothing, even before another event comes due for it.
    conn.execute(
        "DELETE FROM fleet_event_subscriptions WHERE principal_id NOT IN "
        "(SELECT node_id FROM nodes WHERE revoked_at IS NULL) AND principal_id NOT IN "
        "(SELECT principal_id FROM fleet_message_principals WHERE revoked_at IS NULL)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO fleet_event_deliveries(seq,subscription_id,state,next_attempt) "
        "SELECT e.seq, s.id, 'pending', 0 FROM fleet_events e JOIN fleet_event_subscriptions s "
        "ON e.name=s.name AND e.seq>s.cursor_seq AND (s.filter_value IS NULL OR s.filter_value=e.subject) "
        "WHERE e.created_at>=?",
        (now - EVENT_RETENTION_SECONDS,),
    )
    conn.execute("UPDATE fleet_event_subscriptions SET cursor_seq=(SELECT COALESCE(MAX(seq),0) FROM fleet_events)")
    conn.execute(
        "DELETE FROM fleet_event_deliveries WHERE subscription_id NOT IN (SELECT id FROM fleet_event_subscriptions)"
    )
    conn.execute(
        "DELETE FROM fleet_event_deliveries WHERE state='pending' AND seq IN "
        "(SELECT seq FROM fleet_events WHERE created_at<?)",
        (now - EVENT_RETENTION_SECONDS,),
    )
    conn.execute(
        "DELETE FROM fleet_events WHERE created_at<? AND seq NOT IN "
        "(SELECT seq FROM fleet_event_deliveries WHERE state='pending')",
        (now - EVENT_RETENTION_SECONDS,),
    )
    conn.execute(
        "DELETE FROM fleet_event_deliveries WHERE state!='pending' AND seq NOT IN (SELECT seq FROM fleet_events)"
    )
    rows = conn.execute(
        "SELECT d.seq, d.subscription_id, d.attempts, e.event_id, e.name, e.subject, e.data, e.created_at, "
        "s.principal_id, s.url, s.secret, s.previous_secret, s.rotate_until "
        "FROM fleet_event_deliveries d JOIN fleet_events e ON e.seq=d.seq "
        "JOIN fleet_event_subscriptions s ON s.id=d.subscription_id "
        "WHERE d.state='pending' AND d.next_attempt<=? ORDER BY d.seq LIMIT ?",
        (now, BATCH),
    ).fetchall()
    columns = (
        "seq subscription_id attempts event_id name subject data created_at principal url secret previous rotate_until"
    ).split()
    return [dict(zip(columns, row, strict=True)) for row in rows]


def _still_allowed(conn: sqlite3.Connection, row: dict[str, Any]) -> bool | None:
    """True to deliver, False to skip this event, None when the subscriber is revoked (drop the subscription)."""
    principal = row["principal"]
    active = conn.execute(
        "SELECT 1 FROM nodes WHERE node_id=? AND revoked_at IS NULL UNION ALL "
        "SELECT 1 FROM fleet_message_principals WHERE principal_id=? AND revoked_at IS NULL LIMIT 1",
        (principal, principal),
    ).fetchone()
    if active is None:
        return None
    if row["name"] == events.MESSAGE_CREATED:
        return row["subject"] == principal
    return events.permits(conn, principal, row["subject"])


def _body(row: dict[str, Any]) -> bytes:
    return json.dumps(
        {
            "eventId": row["event_id"],
            "name": row["name"],
            "timestamp": events.iso(row["created_at"]),
            "data": json.loads(row["data"]),
            "cursor": None,
        },
        separators=(",", ":"),
    ).encode()


class Dispatcher:
    def __init__(self, db_path: Path, *, post: Post | None = None, clock: Callable[[], float] = time.time) -> None:
        self.db_path, self.clock = Path(db_path), clock
        self._post: Post = post or post_webhook
        self._pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="fleet-events")

    def _attempt(self, job: tuple[dict[str, Any], bytes, dict[str, str]]) -> tuple[int | None, str]:
        row, body, headers = job
        try:
            status, _ = self._post(row["url"], body, headers, events.current().policy())
        except ValueError:
            return None, "policy"
        except OSError:
            return None, "transport"
        return status, ""

    def tick(self) -> int:
        """Run one pass; returns the number of deliveries acknowledged with 2xx."""
        from . import fleet_hub

        config = events.current()
        if not config.enabled:
            return 0
        now = self.clock()
        conn = fleet_hub.open_db(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                sweep_expired_claims(conn, now)
                due = _fan_out(conn, now)
                jobs: list[tuple[dict[str, Any], bytes, dict[str, str]]] = []
                planned: dict[str, int] = {}
                for row in due:
                    allowed = _still_allowed(conn, row)
                    if allowed is None:
                        conn.execute("DELETE FROM fleet_event_subscriptions WHERE id=?", (row["subscription_id"],))
                        continue
                    if not allowed:
                        conn.execute(
                            "UPDATE fleet_event_deliveries SET state='skipped' WHERE seq=? AND subscription_id=?",
                            (row["seq"], row["subscription_id"]),
                        )
                        continue
                    recent = conn.execute(
                        "SELECT COUNT(*) FROM fleet_event_deliveries WHERE subscription_id=? AND attempted_at>?",
                        (row["subscription_id"], now - 3600),
                    ).fetchone()[0]
                    if recent + planned.get(row["subscription_id"], 0) >= HOURLY_DELIVERY_CEILING:
                        conn.execute(
                            "UPDATE fleet_event_deliveries SET next_attempt=? WHERE seq=? AND subscription_id=?",
                            (now + 60, row["seq"], row["subscription_id"]),
                        )
                        continue
                    body = _body(row)
                    previous = row["previous"] if row["rotate_until"] and row["rotate_until"] > now else None
                    headers = signed_headers(
                        row["secret"], row["event_id"], body, row["subscription_id"], now=now, previous=previous
                    )
                    planned[row["subscription_id"]] = planned.get(row["subscription_id"], 0) + 1
                    jobs.append((row, body, headers))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            delivered = 0
            results = list(self._pool.map(self._attempt, jobs))
            conn.execute("BEGIN IMMEDIATE")
            try:
                for (row, _, _), (status, problem) in zip(jobs, results, strict=True):
                    delivered += self._record(conn, row, status, problem, now)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            return delivered
        finally:
            conn.close()

    @staticmethod
    def _record(conn: sqlite3.Connection, row: dict[str, Any], status: int | None, problem: str, now: float) -> int:
        attempts = row["attempts"] + 1
        key = (row["seq"], row["subscription_id"])
        if status is not None and 200 <= status < 300:
            conn.execute(
                "UPDATE fleet_event_deliveries SET state='delivered', attempts=?, attempted_at=? "
                "WHERE seq=? AND subscription_id=?",
                (attempts, now, *key),
            )
            conn.execute("UPDATE fleet_event_subscriptions SET failures=0 WHERE id=?", (row["subscription_id"],))
            return 1
        if status == 410:
            # The receiver says the subscription is gone; stop sending.
            conn.execute("DELETE FROM fleet_event_subscriptions WHERE id=?", (row["subscription_id"],))
            return 0
        permanent = problem == "policy" or (
            status is not None and (300 <= status < 500 and status not in (408, 429) or status == 413)
        )
        if permanent or attempts >= MAX_ATTEMPTS:
            conn.execute(
                "UPDATE fleet_event_deliveries SET state='failed', attempts=?, attempted_at=? "
                "WHERE seq=? AND subscription_id=?",
                (attempts, now, *key),
            )
            conn.execute(
                "UPDATE fleet_event_subscriptions SET failures=failures+1 WHERE id=?", (row["subscription_id"],)
            )
            conn.execute(
                "DELETE FROM fleet_event_subscriptions WHERE id=? AND failures>=?",
                (row["subscription_id"], MAX_FAILED_STREAK),
            )
            return 0
        conn.execute(
            "UPDATE fleet_event_deliveries SET attempts=?, attempted_at=?, next_attempt=? "
            "WHERE seq=? AND subscription_id=?",
            (attempts, now, now + min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2**attempts), *key),
        )
        return 0

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception as error:  # noqa: BLE001 - a bad tick must not end delivery
                _LOG.warning("fleet event tick failed: %s", type(error).__name__)
            stop.wait(TICK_SECONDS)
        self._pool.shutdown(wait=False, cancel_futures=True)


def start_dispatcher(db_path: Path, stop: threading.Event) -> threading.Thread | None:
    """Start delivery when events are enabled; returns None (and starts nothing) when they are not."""
    if not events.current().enabled:
        return None
    thread = threading.Thread(target=Dispatcher(db_path).run, args=(stop,), name="fleet-events", daemon=True)
    thread.start()
    return thread
