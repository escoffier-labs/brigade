"""In-process snapshot cache for Center dashboard views.

Keeps Center read-only: loaders still go through existing fetch/CLI seams.
The cache is per server process, TTL-bounded, and single-flight so a slow
view cannot pile overlapping subprocesses.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

Loader = Callable[[], dict[str, Any]]

DEFAULT_TTL_SECONDS = 15.0
DEFAULT_WAIT_MS = 2000.0
LOADING_PAYLOAD: dict[str, Any] = {"_center_loading": True}


@dataclass(frozen=True)
class Snapshot:
    payload: dict[str, Any]
    fetched_at: datetime | None
    status: str
    age_seconds: float | None


@dataclass
class _Entry:
    payload: dict[str, Any]
    fetched_at: datetime
    fetched_mono: float


@dataclass
class _Inflight:
    event: threading.Event
    thread: threading.Thread


class SnapshotCache:
    """TTL cache with stale-while-revalidate and a loading miss path."""

    def __init__(self, *, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self._inflight: dict[str, _Inflight] = {}

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def get(
        self,
        key: str,
        loader: Loader,
        *,
        wait_ms: float = DEFAULT_WAIT_MS,
        now: float | None = None,
    ) -> Snapshot:
        now_mono = time.monotonic() if now is None else now
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and (now_mono - entry.fetched_mono) < self.ttl_seconds:
                return self._as_snapshot(entry, "fresh", now_mono)
            stale_entry = entry
            inflight = self._inflight.get(key)
            if inflight is None:
                inflight = self._start_locked(key, loader)

        if stale_entry is not None:
            return self._as_snapshot(stale_entry, "stale", now_mono)

        if wait_ms > 0:
            inflight.event.wait(wait_ms / 1000.0)
            with self._lock:
                entry = self._entries.get(key)
            if entry is not None:
                status = "fresh" if (time.monotonic() - entry.fetched_mono) < self.ttl_seconds else "stale"
                return self._as_snapshot(entry, status, time.monotonic())
        return Snapshot(payload=dict(LOADING_PAYLOAD), fetched_at=None, status="loading", age_seconds=None)

    def _start_locked(self, key: str, loader: Loader) -> _Inflight:
        event = threading.Event()
        inflight = _Inflight(
            event=event, thread=threading.Thread(target=self._run, args=(key, loader, event), daemon=True)
        )
        self._inflight[key] = inflight
        inflight.thread.start()
        return inflight

    def _run(self, key: str, loader: Loader, event: threading.Event) -> None:
        try:
            payload = loader()
            if not isinstance(payload, dict):
                payload = {"error": "view fetch returned a non-object"}
        except Exception:  # noqa: BLE001 - a failed refresh must not take the server down
            payload = {"_center_fetch_failed": True}
        fetched_at = datetime.now(timezone.utc)
        fetched_mono = time.monotonic()
        with self._lock:
            self._entries[key] = _Entry(payload=payload, fetched_at=fetched_at, fetched_mono=fetched_mono)
            self._inflight.pop(key, None)
            event.set()

    def _as_snapshot(self, entry: _Entry, status: str, now_mono: float) -> Snapshot:
        return Snapshot(
            payload=entry.payload,
            fetched_at=entry.fetched_at,
            status=status,
            age_seconds=max(0.0, now_mono - entry.fetched_mono),
        )
