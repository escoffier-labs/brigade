"""Spool flush with poisoned-batch isolation (#1735, #1710).

The hub rejects a ``/events`` batch atomically, so a non-retryable 4xx says
nothing about the other events in it. A rejected batch is bisected until
each rejected event stands alone; valid sub-batches are delivered in order
and each isolated event moves to a bounded dead-letter file beside the
spool (``<node>.rejected.jsonl``). A transport failure at any point stops the
flush and keeps every unprocessed event queued.

Processing is strictly in order, so handled events always form a prefix of
the spool. The dead letter is written before the spool rewrite: a crash
between the two replays the prefix, which the hub dedupes by digest and the
dead letter dedupes by fingerprint. Names resolve on ``fleet_client`` at
call time so its seams (``_post_events``, ``_write_spool_atomic``,
``FLUSH_BATCH_SIZE``) stay patchable.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEAD_LETTER_SUFFIX = ".rejected.jsonl"
DEAD_LETTER_MAX_RECORDS = 200
# Events larger than this keep their identity fields but not their body.
DEAD_LETTER_MAX_EVENT_BYTES = 16 * 1024
REASON_MAX_CHARS = 200
_monotonic = time.monotonic
_IDENTITY_FIELDS = ("node_id", "run_id", "sequence", "digest", "ts", "state")


def dead_letter_path(spool: Path) -> Path:
    # Node ids are sanitized to [A-Za-z0-9_-], so this never names a spool.
    return spool.with_name(spool.stem + DEAD_LETTER_SUFFIX)


def _sanitize_reason(reason: str) -> str:
    cleaned = "".join(ch if ch.isprintable() else "?" for ch in reason)
    return cleaned[:REASON_MAX_CHARS]


@dataclass
class _Bisection:
    handled: int = 0
    delivered: int = 0
    rejected: list[tuple[dict[str, Any], str]] = field(default_factory=list)
    stopped: bool = False


def _bisect(
    chunk: list[dict[str, Any]],
    error: Exception,
    post: Callable[[list[dict[str, Any]]], None],
    poison: type[Exception],
    on_delivered: Callable[[list[dict[str, Any]]], None] | None,
    result: _Bisection,
) -> None:
    """Resolve a rejected ``chunk`` in order; set ``stopped`` on transport failure."""
    if len(chunk) == 1:
        result.rejected.append((chunk[0], _sanitize_reason(str(error))))
        result.handled += 1
        return
    middle = len(chunk) // 2
    for half in (chunk[:middle], chunk[middle:]):
        # Both halves are posted: a batch-level rejection (HTTP 413) can
        # clear once split, so a delivered left half proves nothing about
        # the right one.
        try:
            post(half)
        except poison as exc:
            _bisect(half, exc, post, poison, on_delivered, result)
        except Exception:
            result.stopped = True
        else:
            result.handled += len(half)
            result.delivered += len(half)
            if on_delivered is not None:
                on_delivered(half)
        if result.stopped:
            return


def _fingerprint(client: Any, event: dict[str, Any]) -> str:
    return hashlib.sha256(client._encode(event).encode("utf-8")).hexdigest()


def _dead_letter(client: Any, path: Path, rejected: list[tuple[dict[str, Any], str]], dir_fd: int | None) -> bool:
    """Record isolated events in the bounded dead letter; False if it was not persisted."""
    target = dead_letter_path(path)
    try:
        records = client._read_spool(target, dir_fd=dir_fd)
        known = {record.get("fingerprint") for record in records}
        rejected_at = datetime.now(timezone.utc).isoformat()
        for event, reason in rejected:
            fingerprint = _fingerprint(client, event)
            if fingerprint in known:
                continue
            known.add(fingerprint)
            record: dict[str, Any] = {key: event.get(key) for key in _IDENTITY_FIELDS}
            record.update(fingerprint=fingerprint, reason=reason, rejected_at=rejected_at)
            if len(client._encode(event).encode("utf-8")) <= DEAD_LETTER_MAX_EVENT_BYTES:
                record["event"] = event
            else:
                record["event_omitted"] = True
            records.append(record)
        client._write_spool_atomic(target, records[-DEAD_LETTER_MAX_RECORDS:], dir_fd=dir_fd)
    except (OSError, client.FleetClientError) as exc:
        client._LOG.warning("fleet spool %s: rejected events kept queued, dead letter failed: %s", path, exc)
        return False
    for event, reason in rejected:
        client._LOG.warning(
            "fleet spool %s: dropping 1 event(s) (sequence %s) to %s: %s",
            path,
            event.get("sequence"),
            target.name,
            reason,
        )
    return True


def flush_locked(
    path: Path,
    hub: str,
    token: str,
    *,
    max_batches: int | None,
    timeout: float,
    dir_fd: int | None = None,
    preserve_poison: bool = False,
    on_delivered: Callable[[list[dict[str, Any]]], None] | None = None,
    bisect_budget: float | None = None,
) -> int:
    """Flush a locked spool in order; return the number of delivered events.

    ``bisect_budget`` caps the wall-clock seconds all bisection posts in this
    flush may share, for callers with a latency contract (``report_event``).
    Isolation that runs out of budget stops like a transport failure, so the
    unprocessed events stay queued for the next flush. ``None`` is unbounded.
    """
    from . import fleet_client as client

    events = client._read_spool(path, dir_fd=dir_fd)
    if not events:
        if dir_fd is not None:
            try:
                os.unlink(path.name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
        else:
            path.unlink(missing_ok=True)
        return 0

    def post(batch: list[dict[str, Any]]) -> None:
        client._post_events(hub, token, batch, timeout=timeout)

    deadline: float | None = None

    def bisect_post(batch: list[dict[str, Any]]) -> None:
        if deadline is None:
            post(batch)
            return
        remaining = deadline - _monotonic()
        if remaining <= 0:
            raise TimeoutError("fleet spool poison isolation exceeded its budget")
        client._post_events(hub, token, batch, timeout=min(timeout, remaining))

    index = 0
    delivered = 0
    batches = 0
    rejected: list[tuple[dict[str, Any], str]] = []
    while index < len(events) and (max_batches is None or batches < max_batches):
        batch = events[index : index + client.FLUSH_BATCH_SIZE]
        batches += 1
        try:
            post(batch)
        except client._PoisonResponse as exc:
            if preserve_poison:
                break
            if bisect_budget is not None and deadline is None:
                deadline = _monotonic() + bisect_budget
            outcome = _Bisection()
            _bisect(batch, exc, bisect_post, client._PoisonResponse, on_delivered, outcome)
            index += outcome.handled
            delivered += outcome.delivered
            rejected.extend(outcome.rejected)
            if outcome.stopped:
                break
            continue
        except Exception:
            break
        index += len(batch)
        delivered += len(batch)
        if on_delivered is not None:
            on_delivered(batch)
    if index == 0:
        return 0
    remaining = events[index:]
    if rejected and not _dead_letter(client, path, rejected, dir_fd):
        # Never drop an event without a record: requeue it ahead of the
        # unprocessed suffix, which keeps the original relative order.
        remaining = [event for event, _reason in rejected] + remaining
    client._write_spool_atomic(path, remaining, dir_fd=dir_fd)
    return delivered
