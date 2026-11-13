"""Cumulative read days, independent of the bounded search log."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

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


def record(target: Path, keys: Iterable[str], *, today: date | None = None) -> None:
    from .inbox_lock import held_file_lock

    today = today or date.today()
    try:
        path = target / READS_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        with held_file_lock(path.with_suffix(".lock"), deadline_seconds=1):
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
        return


def last_read(ledger: ReadLedger, keys: Iterable[str]) -> date | None:
    return max((ledger.last_read[k] for k in keys if k in ledger.last_read), default=None)


def hub_last_reads(target: Path) -> dict[str, date]:
    try:
        settings = hubc.load_settings(target)
        if not settings.enabled or not settings.scope:
            return {}
        client = hubc.client_for(settings)
        after = None
        result = {}
        while True:
            body = hubc.read_stats(client, settings.scope, limit=500, after=after)
            for row in body["rows"]:
                if row.get("last_read_day"):
                    result[row["card_id"]] = date.fromisoformat(row["last_read_day"])
            following = body.get("next_after")
            if not following or following == after:
                return result
            after = following
    except (hubc.HubConfigError, hubc.HubUnavailable, hubc.HubRefusal, OSError, ValueError, KeyError):
        return {}
