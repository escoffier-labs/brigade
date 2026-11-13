"""Cumulative read days, independent of the bounded search log."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import sys
import time
from pathlib import Path
from typing import Callable, Iterable

from . import memory_hub_client as hubc
from . import memory_hub_store as store

READS_REL = ".brigade/memory/card-reads.json"


@dataclass(frozen=True)
class ReadLedger:
    tracking_since: date
    last_read: dict[str, date]


def load(target: Path) -> ReadLedger:
    try:
        p = store.read_json(target / READS_REL)
        return ReadLedger(
            date.fromisoformat(p["tracking_since"]), {k: date.fromisoformat(v) for k, v in p["last_read"].items()}
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return ReadLedger(date.today(), {})


def record(target: Path, keys: Iterable[str], *, today: date | None = None, _retry: bool = True) -> None:
    from .inbox_lock import held_file_lock

    today = today or date.today()
    keys = tuple(keys)
    try:
        path = target / READS_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        with held_file_lock(path.with_suffix(".lock"), deadline_seconds=5):
            try:
                p = store.read_json(path)
                ledger = ReadLedger(
                    date.fromisoformat(p["tracking_since"]),
                    {k: date.fromisoformat(v) for k, v in p["last_read"].items()},
                )
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                ledger = ReadLedger(today, {})
            days = dict(ledger.last_read)
            for key in keys:
                days[key] = max(today, days.get(key, today))
            store.write_json(
                path,
                {
                    "tracking_since": ledger.tracking_since.isoformat(),
                    "last_read": {k: v.isoformat() for k, v in days.items()},
                },
            )
    except (OSError, ValueError):
        if _retry:
            record(target, keys, today=today, _retry=False)
        return


def record_content(target: Path, text: str, rel: str) -> None:
    """Record both identities for successfully returned card content, failing open."""
    try:
        from .memory_cmd import card_identity_from_text

        record(target, [rel, card_identity_from_text(text, rel)])
    except Exception:  # noqa: BLE001 - ledger trouble must never fail retrieval
        return


def last_read(ledger: ReadLedger, keys: Iterable[str]) -> date | None:
    return max((ledger.last_read[k] for k in keys if k in ledger.last_read), default=None)


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
                or (date.today() - (date(1970, 1, 1) + timedelta(days=since))).days < unused_after_days
            ):
                raise ValueError("hub read tracking too young")
            for row in body["rows"]:
                try:
                    day = row["last_read_day"]
                    if type(day) is not int or day < 0 or not isinstance(row["card_id"], str):
                        raise ValueError("invalid read day")
                    if day:
                        result[row["card_id"]] = date(1970, 1, 1) + timedelta(days=day)
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
