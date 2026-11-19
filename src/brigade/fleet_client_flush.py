"""Spool flush with poisoned-batch isolation (#1735, #1710).

The hub rejects a ``/events`` batch atomically, so a non-retryable 4xx says
nothing about the other events in it. A rejected batch is bisected until
each rejected event stands alone; valid sub-batches are delivered in order
and each isolated event moves to a bounded dead-letter file beside the
spool (``<node>.rejected.jsonl``). A transport failure at any point stops the
flush and keeps every unprocessed event queued.

When the hub rejects every event, for example a wrong hub URL or an older
hub refusing a field, bisection would isolate every single event: 199 POSTs
per 100-event batch. Bisection stays left-first and in spool order. A single
event the hub rejects is held as a pending rejection instead of being written
to the dead letter at once. The first accepted event in a flush proves the
hub accepts events, so the pending rejections, which precede it in the spool,
are written in order and later rejections go straight to the dead letter.
Four consecutive single rejections with no acceptance declare a hub fault:
nothing is dead-lettered and the pending events stay queued. A transport
failure or budget stop leaves pending rejections queued as well.

Residual behavior: four or more consecutive poison events at the head of the
spool, with no deliverable event before them, look like a hub fault and stay
queued until a later flush delivers something ahead of them or an operator
intervenes. This is the price of bounding an all-reject hub.

Processing is strictly in order, so delivered and dead-lettered events always
form a prefix of the spool. The dead letter is written before the spool
rewrite: a crash between the two replays the prefix, which the hub dedupes by
digest and the dead letter dedupes by fingerprint. Names resolve on
``fleet_client`` at call time so its seams (``_post_events``,
``_write_spool_atomic``, ``FLUSH_BATCH_SIZE``) stay patchable.
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
# Consecutive single-event rejections with no acceptance that declare a hub
# fault. Bounds an all-reject batch to its split path plus this many singles.
WHOLE_BATCH_REJECT_BUDGET = 4
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
    delivered: int = 0
    dead: list[tuple[dict[str, Any], str]] = field(default_factory=list)
    pending: list[tuple[dict[str, Any], str]] = field(default_factory=list)
    proven: bool = False
    consecutive_singles: int = 0
    stopped: bool = False
    aborted: bool = False


def _commit_pending(result: _Bisection) -> None:
    """Dead-letter held rejections once an acceptance proves the hub works."""
    if result.pending:
        result.dead.extend(result.pending)
        result.pending.clear()


def _bisect(
    chunk: list[dict[str, Any]],
    error: Exception,
    post: Callable[[list[dict[str, Any]]], None],
    poison: type[Exception],
    on_delivered: Callable[[list[dict[str, Any]]], None] | None,
    result: _Bisection,
    budget: int = WHOLE_BATCH_REJECT_BUDGET,
) -> None:
    """Resolve a rejected ``chunk`` in left-first spool order.

    Sets ``stopped`` on transport failure and ``aborted`` when the hub rejects
    ``budget`` single events in a row without accepting anything.
    """
    if len(chunk) == 1:
        reason = _sanitize_reason(str(error))
        if result.proven:
            result.dead.append((chunk[0], reason))
        else:
            # Hold the rejection until an accepted event proves the hub works.
            # These events precede that acceptance in the spool, so committing
            # them later keeps prefix truncation correct.
            result.pending.append((chunk[0], reason))
            result.consecutive_singles += 1
            if result.consecutive_singles >= budget:
                result.aborted = True
        return
    middle = len(chunk) // 2
    for half in (chunk[:middle], chunk[middle:]):
        # Both halves are posted: a batch-level rejection (HTTP 413) can
        # clear once split, so a delivered left half proves nothing about
        # the right one.
        try:
            post(half)
        except poison as exc:
            _bisect(half, exc, post, poison, on_delivered, result, budget)
        except Exception:
            result.stopped = True
        else:
            result.delivered += len(half)
            if not result.proven:
                result.proven = True
                _commit_pending(result)
            if on_delivered is not None:
                on_delivered(half)
        if result.stopped or result.aborted:
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
    whole_batch_budget: int = WHOLE_BATCH_REJECT_BUDGET,
) -> int:
    """Flush a locked spool in order; return the number of delivered events.

    ``bisect_budget`` caps the wall-clock seconds all bisection posts in this
    flush may share, for callers with a latency contract (``report_event``).
    Isolation that runs out of budget stops like a transport failure, so the
    unprocessed events stay queued for the next flush. ``None`` is unbounded.

    ``whole_batch_budget`` is the run of rejected single events, with no
    accepted event in this flush, that declares a hub fault. Those events and
    the rest of the spool stay queued.
    """
    if whole_batch_budget < 2:
        raise ValueError("whole_batch_budget must be at least 2")
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

    outcome = _Bisection()
    index = 0
    batches = 0
    while index < len(events) and (max_batches is None or batches < max_batches):
        batch = events[index : index + client.FLUSH_BATCH_SIZE]
        batches += 1
        try:
            post(batch)
        except client._PoisonResponse as exc:
            if preserve_poison:
                outcome.stopped = True
                break
            if bisect_budget is not None and deadline is None:
                deadline = _monotonic() + bisect_budget
            _bisect(batch, exc, bisect_post, client._PoisonResponse, on_delivered, outcome, whole_batch_budget)
            if outcome.aborted:
                client._LOG.warning(
                    "fleet spool %s: hub rejects every event (%d single rejections, no delivery); "
                    "keeping %d event(s) queued for the next flush",
                    path,
                    outcome.consecutive_singles,
                    len(events) - (outcome.delivered + len(outcome.dead)),
                )
                break
            if outcome.stopped:
                break
            index += len(batch)
            continue
        except Exception:
            # A transport failure proves nothing about held rejections, so
            # they must stay queued like any other stop.
            outcome.stopped = True
            break
        outcome.delivered += len(batch)
        if not outcome.proven:
            outcome.proven = True
            _commit_pending(outcome)
        if on_delivered is not None:
            on_delivered(batch)
        index += len(batch)
    # A clean finish with no fault proves the hub accepted this spool's events,
    # so any held rejections belong in the dead letter. On a fault or a stop,
    # pending rejections stay queued and are never dead-lettered.
    if not outcome.aborted and not outcome.stopped:
        _commit_pending(outcome)
    if not outcome.delivered and not outcome.dead:
        return 0
    # Delivered and dead-lettered events form the handled prefix. Everything
    # else, including pending rejections, resumes from this point.
    remaining = events[outcome.delivered + len(outcome.dead) :]
    if outcome.dead and not _dead_letter(client, path, outcome.dead, dir_fd):
        # Never drop an event without a record: requeue it ahead of the
        # unprocessed suffix, which keeps the original relative order.
        remaining = [event for event, _reason in outcome.dead] + remaining
    client._write_spool_atomic(path, remaining, dir_fd=dir_fd)
    return outcome.delivered
