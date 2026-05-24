"""`brigade status` - show which stations are present and healthy."""
from __future__ import annotations

from pathlib import Path

from . import doctor as _doctor
from .registry import all_stations


def run(target: Path) -> int:
    ctx = _doctor.build_context(target)
    print(f"brigade status: {ctx.target}")
    width = max((len(s.name) for s in all_stations()), default=8)
    for station in all_stations():
        checks = station.doctor(ctx) if station.doctor else []
        ok = sum(1 for s, _, _ in checks if s == _doctor.OK)
        warn = sum(1 for s, _, _ in checks if s == _doctor.WARN)
        fail = sum(1 for s, _, _ in checks if s == _doctor.FAIL)
        health = "issues" if fail else ("ok" if ok else "empty")
        print(
            f"  {station.name.ljust(width)}  [{health}]  "
            f"{ok} ok, {warn} warn, {fail} fail  - {station.summary}"
        )
    return 0
