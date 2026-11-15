"""Cumulative read days, independent of the bounded search log."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import sys
import json
import time
import secrets
import os
import stat
from pathlib import Path
from typing import Callable, Iterable

from . import memory_hub_client as hubc
from . import dirfd
from .run_dirfd import bound_run_dir, MAX_READ_BYTES

READS_REL = ".brigade/memory/card-reads.json"


@dataclass(frozen=True)
class ReadLedger:
    tracking_since: date
    last_read: dict[str, date]
    healthy_since: date | None = None
    heartbeat: date | None = None
    recorder_error: bool = False


COVERAGE_REL = ".brigade/memory/read-coverage"


def _day() -> date:
    return datetime.now(timezone.utc).date()


def _read_json(target: Path, rel: str):
    parts = Path(rel).parts
    with bound_run_dir(target) as bound:
        try:
            fd = bound.open_file(parts[:-1], parts[-1], os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise OSError("read coverage must be a regular single-link file")
            raw = handle.read(MAX_READ_BYTES + 1)
            if len(raw) > MAX_READ_BYTES:
                raise ValueError("read coverage byte limit exceeded")
            return json.loads(raw)


def _write_json(target: Path, rel: str, value: dict) -> None:
    parts = Path(rel).parts
    with bound_run_dir(target) as bound:
        bound.write_text_atomic(parts[:-1], parts[-1], json.dumps(value, sort_keys=True) + "\n")


def _unlink(target: Path, rel: str) -> None:
    parts = Path(rel).parts
    with bound_run_dir(target) as bound:
        parent = bound.dir_fd(*parts[:-1])
        dirfd.unlink_child(parent, parts[-1])
        dirfd.fsync_directory(parent)


def _pending(target: Path) -> list[str]:
    with bound_run_dir(target) as bound:
        try:
            parent = bound.dir_fd(*Path(COVERAGE_REL).parts)
        except FileNotFoundError:
            return []
        names = dirfd.child_names(parent, 10001)
        if len(names) > 10000:
            raise ValueError("read coverage intent limit exceeded")
        return [f"{COVERAGE_REL}/{name}" for name in names if name.endswith(".json")]


def load(target: Path) -> ReadLedger:
    try:
        p = _read_json(target, READS_REL)
        return ReadLedger(
            date.fromisoformat(p["tracking_since"]),
            {k: date.fromisoformat(v) for k, v in p["last_read"].items()},
            date.fromisoformat(p["healthy_since"]) if p.get("healthy_since") else None,
            date.fromisoformat(p["heartbeat"]) if p.get("heartbeat") else None,
            bool(_pending(target)),
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return ReadLedger(_day(), {}, recorder_error=True)


def covered(ledger: ReadLedger, *, today: date, days: int) -> bool:
    return bool(
        not ledger.recorder_error
        and ledger.heartbeat == today
        and ledger.healthy_since is not None
        and 0 <= (today - ledger.healthy_since).days
        and (today - ledger.healthy_since).days >= days
    )


def record(target: Path, keys: Iterable[str], *, today: date | None = None) -> bool:
    """Persist intent before recording. Failed or interrupted writes block coverage.

    A subsequent successful heartbeat starts a new window after recorded errors.
    Pending intents are never cleared by another writer.
    """
    from .inbox_lock import held_file_lock
    from .memory_care_journal import safe_path

    today = today or _day()
    keys = tuple(keys)
    intent = f"{COVERAGE_REL}/{secrets.token_hex(16)}.json"
    _write_json(target, intent, {"state": "pending", "day": today.isoformat()})
    try:
        path = safe_path(target, READS_REL)
        with held_file_lock(path.with_suffix(".lock"), deadline_seconds=5):
            ledger = load(target)
            errors = [p for p in _pending(target) if _read_json(target, p).get("state") == "error"]
            since = ledger.healthy_since
            if errors or since is None or ledger.heartbeat is None or not 0 <= (today - ledger.heartbeat).days <= 1:
                since = today
            days = {k: v for k, v in ledger.last_read.items() if v <= today}
            for key in keys:
                days[key] = max(today, days.get(key, today))
            _write_json(
                target,
                READS_REL,
                {
                    "tracking_since": min(ledger.tracking_since, today).isoformat(),
                    "healthy_since": since.isoformat(),
                    "heartbeat": today.isoformat(),
                    "last_read": {k: v.isoformat() for k, v in days.items()},
                },
            )
            for error in errors:
                _unlink(target, error)
            _unlink(target, intent)
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        # This is independent of the ledger lock and remains visible to lifecycle.
        _write_json(target, intent, {"state": "error", "day": today.isoformat()})
        return False


def record_content(target: Path, text: str, rel: str) -> None:
    """Record an explicit content open before returning its content to the caller."""
    from .memory_cmd import card_identity_from_text

    record(target, [rel, card_identity_from_text(text.lstrip("\ufeff").replace("\r\n", "\n"), rel)])


def last_read(ledger: ReadLedger, keys: Iterable[str], *, today: date | None = None) -> date | None:
    today = today or _day()
    return max(
        (ledger.last_read[k] for k in keys if k in ledger.last_read and ledger.last_read[k] <= today), default=None
    )


HUB_PAGE_GAP_SECONDS = 1.1


def hub_last_reads(
    target: Path, *, unused_after_days: int = 120, sleep: Callable[[float], None] = time.sleep
) -> dict[str, date] | None:
    """Last read day per hub card, or ``None`` when a configured hub could not be read in full.

    ``None`` is not "nobody read anything": callers must not retire on use grounds without it.
    The hub allows one read-stat request per second per principal, so pages are paced.
    """
    try:
        settings = hubc.load_settings(target)
        if not settings.enabled or not settings.scope:
            return {}
        client = hubc.client_for(settings)
        after = None
        result = {}
        cursors: set[str] = set()
        while True:
            if after is not None:
                sleep(HUB_PAGE_GAP_SECONDS)
            for attempt in range(3):
                try:
                    body = hubc.read_stats(client, settings.scope, limit=500, after=after)
                    break
                except hubc.HubRefusal as exc:
                    if getattr(exc, "status", None) != 429 or attempt == 2:
                        raise
                    sleep(HUB_PAGE_GAP_SECONDS)
            since = body["read_counts_since_day"]
            if (
                type(since) is not int
                or since < 0
                or (_day() - (date(1970, 1, 1) + timedelta(days=since))).days < unused_after_days
            ):
                raise ValueError("hub read tracking too young")
            for row in body["rows"]:
                try:
                    # Legacy totals combine searches with opens and cannot establish use.
                    if "last_card_read_day" not in row:
                        raise ValueError("hub read stats do not distinguish explicit card opens")
                    day = row["last_card_read_day"]
                    if type(day) is not int or day < 0 or not isinstance(row["card_id"], str):
                        raise ValueError("invalid read day")
                    if day:
                        observed = date(1970, 1, 1) + timedelta(days=day)
                        if observed <= _day():
                            result[row["card_id"]] = observed
                except (TypeError, ValueError, OverflowError, KeyError):
                    raise ValueError("malformed hub read-stat row") from None
            following = body.get("next_after")
            if following is None:
                return result
            if not isinstance(following, str) or not following or following in cursors:
                raise ValueError("repeated or malformed hub read-stat cursor")
            cursors.add(following)
            after = following
    except (
        hubc.HubConfigError,
        hubc.HubUnavailable,
        hubc.HubRefusal,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        OverflowError,
    ) as exc:
        print(f"memory care: hub read stats unavailable, retiring nothing on use grounds: {exc}", file=sys.stderr)
        return None
