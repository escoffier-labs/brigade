"""`brigade status` - show which stations are present and healthy."""

from __future__ import annotations

import json
from pathlib import Path

from . import doctor as _doctor
from .registry import all_stations


def run(target: Path, *, json_output: bool = False) -> int:
    ctx = _doctor.build_context(target)
    rows = []
    for station in all_stations():
        checks = station.doctor(ctx) if station.doctor else []
        ok = sum(1 for s, _, _ in checks if s == _doctor.OK)
        warn = sum(1 for s, _, _ in checks if s == _doctor.WARN)
        fail = sum(1 for s, _, _ in checks if s == _doctor.FAIL)
        health = "issues" if fail else ("ok" if ok else "empty")
        rows.append(
            {
                "station": station.name,
                "health": health,
                "ok": ok,
                "warn": warn,
                "fail": fail,
                "summary": station.summary,
            }
        )

    if json_output:
        print(json.dumps({"target": str(ctx.target), "stations": rows}, indent=2, sort_keys=True))
        return 0

    print(f"brigade status: {ctx.target}")
    width = max((len(s.name) for s in all_stations()), default=8)
    for row in rows:
        print(
            f"  {row['station'].ljust(width)}  [{row['health']}]  "
            f"{row['ok']} ok, {row['warn']} warn, {row['fail']} fail  - {row['summary']}"
        )
    return 0
