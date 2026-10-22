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
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import fleet_hub_events as events
from .mcp_events import CallbackPolicy, post_webhook, signed_headers

Post = Callable[[str, bytes, Mapping[str, str], CallbackPolicy], "tuple[int, bytes]"]

TICK_SECONDS = 0.5
# With events off nothing is delivered; the tick only prunes what an earlier run left behind.
IDLE_TICK_SECONDS = 60.0
BATCH = 20
WORKERS = 4
MAX_ATTEMPTS = 8
BACKOFF_BASE_SECONDS = 5
BACKOFF_CAP_SECONDS = 3600
MAX_FAILED_STREAK = 10
HOURLY_DELIVERY_CEILING = 120
HOURLY_WINDOW_SECONDS = 3600
EVENT_RETENTION_SECONDS = 86_400
CLAIM_SWEEP_LIMIT = 100
# Fan-out bounds: a tick materializes a bounded window, and the outbox holds a bounded number of pending rows.
# One principal can hold at most MAX_PENDING_PER_PRINCIPAL of the shared total, and every principal may always
# reach RESERVED_PENDING_PER_PRINCIPAL rows even when the total is spent. The outbox therefore stays within
# MAX_PENDING_DELIVERIES plus the reserve times the number of principals, and no subscriber can starve another.
FAN_OUT_PER_SUBSCRIPTION = 50
MAX_PENDING_DELIVERIES = 2000
MAX_PENDING_PER_PRINCIPAL = 400
RESERVED_PENDING_PER_PRINCIPAL = 10
_LOG = logging.getLogger("brigade.fleet_hub_events")


Watermark = tuple[float, str]


def sweep_expired_claims(conn: sqlite3.Connection, now: float, after: Watermark | None = None) -> Watermark:
    """Emit claim.expired for leases that ran out; the prune path in claims covers the rest.

    Claims stay in their table after expiry, so progress is a (expires_at, target) watermark the caller keeps
    between ticks: each pass takes the next ``CLAIM_SWEEP_LIMIT`` rows past it. Anything that expired before
    the retention window has no retained event to dedupe against and is not revisited.
    """
    if not events.current().enabled or not events.has_subscribers(conn, events.CLAIM_EXPIRED):
        # Nobody was subscribed for these expiries, so nobody is owed them later.
        return (now, "")
    floor = max(after or (0.0, ""), (now - EVENT_RETENTION_SECONDS, ""))
    rows = conn.execute(
        "SELECT target, owner_node, holder_token, expires_at FROM claims WHERE expires_at<=? "
        "AND (expires_at>? OR (expires_at=? AND target>?)) ORDER BY expires_at, target LIMIT ?",
        (now, floor[0], floor[0], floor[1], CLAIM_SWEEP_LIMIT),
    ).fetchall()
    for target, owner, holder, expires_at in rows:
        events.record_claim_expired(conn, target, owner, holder, expires_at)
    return (rows[-1][3], rows[-1][0]) if rows else floor


def _round_robin(subscriptions: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    """Interleave subscriptions across principals (principal id is the last column), keeping each one's order."""
    queues: dict[str, deque[tuple[Any, ...]]] = {}
    for subscription in subscriptions:
        queues.setdefault(subscription[-1], deque()).append(subscription)
    ordered: list[tuple[Any, ...]] = []
    while queues:
        for principal in list(queues):
            ordered.append(queues[principal].popleft())
            if not queues[principal]:
                del queues[principal]
    return ordered


def _materialize(conn: sqlite3.Connection, now: float) -> None:
    """Create delivery rows for new events, a bounded window per subscription and a bounded pending total.

    A subscription's cursor moves only past events it was given a delivery row for (or past the end of the
    outbox when nothing more matches), so whatever does not fit this tick waits for the next one. Capacity is
    shared by principal, not by subscription: subscriptions are served round-robin across principals, each
    principal is capped, and each keeps a small reserve, so one subscriber's failing callbacks cannot fill the
    outbox and hold other principals' events back.
    """
    pending: dict[str, int] = dict(
        conn.execute(
            "SELECT s.principal_id, COUNT(*) FROM fleet_event_deliveries d "
            "JOIN fleet_event_subscriptions s ON s.id=d.subscription_id WHERE d.state='pending' "
            "GROUP BY s.principal_id"
        ).fetchall()
    )
    shared = MAX_PENDING_DELIVERIES - sum(pending.values())
    newest = conn.execute("SELECT COALESCE(MAX(seq),0) FROM fleet_events").fetchone()[0]
    subscriptions = conn.execute(
        "SELECT id, name, filter_value, cursor_seq, principal_id FROM fleet_event_subscriptions WHERE cursor_seq<? "
        "ORDER BY cursor_seq, id",
        (newest,),
    ).fetchall()
    for sid, name, filter_value, cursor, principal in _round_robin(subscriptions):
        held = pending.get(principal, 0)
        room = max(
            RESERVED_PENDING_PER_PRINCIPAL - held,
            min(MAX_PENDING_PER_PRINCIPAL - held, shared),
        )
        limit = min(FAN_OUT_PER_SUBSCRIPTION, room)
        if limit <= 0:
            continue
        seqs = [
            row[0]
            for row in conn.execute(
                "SELECT seq FROM fleet_events WHERE name=? AND seq>? AND created_at>=? "
                "AND (? IS NULL OR subject=?) ORDER BY seq LIMIT ?",
                (name, cursor, now - EVENT_RETENTION_SECONDS, filter_value, filter_value, limit),
            )
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO fleet_event_deliveries(seq,subscription_id,state,next_attempt) "
            "VALUES(?,?,'pending',0)",
            [(seq, sid) for seq in seqs],
        )
        pending[principal] = held + len(seqs)
        shared -= len(seqs)
        # A full window may have more behind it; a short one has reached the end of the outbox.
        conn.execute(
            "UPDATE fleet_event_subscriptions SET cursor_seq=? WHERE id=?",
            (seqs[-1] if len(seqs) == limit else newest, sid),
        )


def _prune_subscriptions(conn: sqlite3.Connection, now: float) -> None:
    """Drop lapsed and revoked subscriptions and erase retired signing keys."""
    conn.execute("DELETE FROM fleet_event_subscriptions WHERE expires_at IS NOT NULL AND expires_at<=?", (now,))
    # A retired signing key is erased once its rotation window closes, not kept as dead weight.
    conn.execute(
        "UPDATE fleet_event_subscriptions SET previous_secret=NULL, rotate_until=NULL "
        "WHERE rotate_until IS NOT NULL AND rotate_until<=?",
        (now,),
    )
    # A revoked principal keeps nothing, even before another event comes due for it.
    conn.execute(
        "DELETE FROM fleet_event_subscriptions WHERE principal_id NOT IN "
        "(SELECT node_id FROM nodes WHERE revoked_at IS NULL) AND principal_id NOT IN "
        "(SELECT principal_id FROM fleet_message_principals WHERE revoked_at IS NULL)"
    )


def _prune_outbox(conn: sqlite3.Connection, now: float) -> None:
    """Apply the retention window to events, deliveries and the request ledgers."""
    conn.execute("DELETE FROM fleet_event_attempts WHERE attempted_at<=?", (now - HOURLY_WINDOW_SECONDS,))
    conn.execute(
        "DELETE FROM fleet_event_verifications WHERE attempted_at<=?", (now - events.VERIFICATION_WINDOW_SECONDS,)
    )
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


def _fan_out(conn: sqlite3.Connection, now: float) -> list[dict[str, Any]]:
    """Create delivery rows for new events, drop dead state, and return the due deliveries."""
    _prune_subscriptions(conn, now)
    _materialize(conn, now)
    _prune_outbox(conn, now)
    # Due deliveries are ranked within each principal and taken rank by rank, the same interleave the outbox
    # uses on the way in, so one principal's older backlog cannot fill every batch.
    rows = conn.execute(
        "SELECT seq, subscription_id, attempts, event_id, name, subject, data, created_at, principal_id, url, "
        "secret, previous_secret, rotate_until, generation FROM ("
        "SELECT d.seq AS seq, d.subscription_id AS subscription_id, d.attempts AS attempts, e.event_id AS event_id, "
        "e.name AS name, e.subject AS subject, e.data AS data, e.created_at AS created_at, "
        "s.principal_id AS principal_id, s.url AS url, s.secret AS secret, s.previous_secret AS previous_secret, "
        "s.rotate_until AS rotate_until, s.generation AS generation, "
        "ROW_NUMBER() OVER (PARTITION BY s.principal_id ORDER BY d.seq, d.subscription_id) AS turn "
        "FROM fleet_event_deliveries d JOIN fleet_events e ON e.seq=d.seq "
        "JOIN fleet_event_subscriptions s ON s.id=d.subscription_id "
        "WHERE d.state='pending' AND d.next_attempt<=?) ORDER BY turn, seq, subscription_id LIMIT ?",
        (now, BATCH),
    ).fetchall()
    columns = (
        "seq subscription_id attempts event_id name subject data created_at principal url secret previous rotate_until "
        "generation"
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
            "data": events.public_data(row["name"], json.loads(row["data"]), row["secret"], row["subscription_id"]),
            "cursor": None,
        },
        separators=(",", ":"),
    ).encode()


class Dispatcher:
    def __init__(self, db_path: Path, *, post: Post | None = None, clock: Callable[[], float] = time.time) -> None:
        self.db_path, self.clock = Path(db_path), clock
        self._post: Post = post or post_webhook
        self._pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="fleet-events")
        self._swept: Watermark | None = None

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

        enabled = events.current().enabled
        now = self.clock()
        conn = fleet_hub.open_db(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            if not enabled:
                # Rows and keys from an earlier enabled run still age out; nothing is swept, fanned out or sent.
                try:
                    _prune_subscriptions(conn, now)
                    _prune_outbox(conn, now)
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
                return 0
            try:
                swept = sweep_expired_claims(conn, now, self._swept)
                due = _fan_out(conn, now)
                jobs: list[tuple[dict[str, Any], bytes, dict[str, str]]] = []
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
                    # Requests, not rows: a retry is another attempt, and attempts planned this tick are in the table.
                    recent = conn.execute(
                        "SELECT COUNT(*) FROM fleet_event_attempts WHERE subscription_id=? AND attempted_at>?",
                        (row["subscription_id"], now - HOURLY_WINDOW_SECONDS),
                    ).fetchone()[0]
                    if recent >= HOURLY_DELIVERY_CEILING:
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
                    conn.execute(
                        "INSERT INTO fleet_event_attempts(subscription_id, attempted_at) VALUES(?,?)",
                        (row["subscription_id"], now),
                    )
                    jobs.append((row, body, headers))
                conn.commit()
                self._swept = swept  # only once the events it recorded are durable
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
        # The outcome belongs to the subscription it was sent for. The owner may have unsubscribed and created the
        # same id again while the request was in flight; a result for the earlier one must not touch the new one.
        if (
            conn.execute(
                "SELECT 1 FROM fleet_event_subscriptions WHERE id=? AND generation=?",
                (row["subscription_id"], row["generation"]),
            ).fetchone()
            is None
        ):
            return 0
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
            stop.wait(TICK_SECONDS if events.current().enabled else IDLE_TICK_SECONDS)
        self._pool.shutdown(wait=False, cancel_futures=True)


def start_dispatcher(db_path: Path, stop: threading.Event) -> threading.Thread:
    """Start the dispatcher thread. It delivers only while events are enabled, but always prunes retained state."""
    thread = threading.Thread(target=Dispatcher(db_path).run, args=(stop,), name="fleet-events", daemon=True)
    thread.start()
    return thread
