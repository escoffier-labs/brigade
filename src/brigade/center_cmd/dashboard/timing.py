"""Request-scoped phase timings for Center dashboard handlers.

Enabled when ``BRIGADE_CENTER_PROFILE`` is set to a non-empty value other
than ``0``/``false``/``no``. Phases are also always recorded on the current
request so a response can carry ``X-Brigade-Center-Timing`` without requiring
the env flag. Stderr JSON lines are env-gated to keep tests quiet.
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_current: ContextVar["_Timer | None"] = ContextVar("center_timing", default=None)


def profiling_enabled() -> bool:
    raw = os.environ.get("BRIGADE_CENTER_PROFILE", "").strip().lower()
    return raw not in {"", "0", "false", "no", "off"}


class _Timer:
    def __init__(self, label: str) -> None:
        self.label = label
        self.phases: list[tuple[str, float]] = []
        self.started = time.perf_counter()

    def add(self, name: str, elapsed_ms: float) -> None:
        self.phases.append((name, elapsed_ms))

    def header_value(self) -> str:
        total_ms = (time.perf_counter() - self.started) * 1000.0
        parts = [f"{name}={elapsed:.1f}" for name, elapsed in self.phases]
        parts.append(f"total={total_ms:.1f}")
        return ";".join(parts)

    def dominant(self) -> str:
        if not self.phases:
            return "total"
        name, _ = max(self.phases, key=lambda item: item[1])
        return name


@contextmanager
def request_timer(label: str) -> Iterator[_Timer]:
    timer = _Timer(label)
    token = _current.set(timer)
    try:
        yield timer
    finally:
        _current.reset(token)
        if profiling_enabled():
            payload = {
                "view": label,
                "phases": {name: round(ms, 1) for name, ms in timer.phases},
                "total_ms": round((time.perf_counter() - timer.started) * 1000.0, 1),
                "dominant": timer.dominant(),
            }
            print(json.dumps(payload, sort_keys=True), file=sys.stderr, flush=True)


@contextmanager
def phase(name: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        timer = _current.get()
        if timer is not None:
            timer.add(name, elapsed_ms)
        elif profiling_enabled():
            print(
                json.dumps({"phase": name, "ms": round(elapsed_ms, 1)}, sort_keys=True),
                file=sys.stderr,
                flush=True,
            )


def note(name: str, elapsed_ms: float) -> None:
    timer = _current.get()
    if timer is not None:
        timer.add(name, elapsed_ms)
