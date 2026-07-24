#!/usr/bin/env python3
"""Attribute Codex provider token receipts to experiment route runs."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def parse_time(value: str) -> datetime:
    rendered = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(rendered)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object: {path}")
    return data


def read_session(path: Path) -> dict[str, Any] | None:
    meta: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    for line in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "session_meta" and isinstance(event.get("payload"), dict):
            meta = event["payload"]
        payload = event.get("payload")
        if event.get("type") == "event_msg" and isinstance(payload, dict) and payload.get("type") == "token_count":
            info = payload.get("info")
            if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
                usage = info["total_token_usage"]
    if meta is None or usage is None:
        return None
    timestamp = meta.get("timestamp")
    cwd = meta.get("cwd")
    session_id = meta.get("id") or meta.get("session_id")
    if not isinstance(timestamp, str) or not isinstance(cwd, str) or not isinstance(session_id, str):
        return None
    return {
        "session_id": session_id,
        "started_at": timestamp,
        "started": parse_time(timestamp),
        "cwd": cwd,
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "reasoning_output_tokens": int(usage.get("reasoning_output_tokens", 0) or 0),
    }


def run_interval(run_json: Path) -> dict[str, Any]:
    data = read_json(run_json)
    started_at = data.get("started_at")
    if not isinstance(started_at, str):
        raise ValueError(f"missing started_at: {run_json}")
    started = parse_time(started_at)
    duration = float(data.get("duration_seconds", 0.0) or 0.0)
    cwd = data.get("cwd")
    if not isinstance(cwd, str):
        raise ValueError(f"missing cwd: {run_json}")
    return {
        "path": run_json,
        "started": started,
        "ended": started + timedelta(seconds=duration),
        "cwd": cwd,
        "status": str(data.get("status", "")),
        "duration_seconds": duration,
    }


def matching_sessions(interval: dict[str, Any], sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lower = interval["started"]
    upper = interval["ended"]
    return [
        session for session in sessions if session["cwd"] == interval["cwd"] and lower <= session["started"] <= upper
    ]


def successful_b_runs(case_dir: Path) -> list[Path]:
    selected: dict[str, Path] = {}
    candidates = sorted((case_dir / "route-b").glob("sample-*/run.json"))
    candidates += sorted((case_dir / "route-b").glob("sample-*-retry-*/run.json"))
    for path in candidates:
        data = read_json(path)
        if data.get("status") != "ok":
            continue
        name = path.parent.name
        parts = name.split("-")
        if len(parts) < 2:
            continue
        sample_id = parts[1]
        selected.setdefault(sample_id, path)
    if set(selected) != {"1", "2", "3"}:
        raise ValueError(f"case {case_dir.name} does not have three successful B samples: {selected}")
    return [selected[sample_id] for sample_id in ("1", "2", "3")]


def read_wall(log_dir: Path, route: str) -> float:
    if route == "B":
        total = float((log_dir / "route-b.wall-seconds").read_text().strip())
        for retry in log_dir.glob("route-b-sample-*-retry-*.wall-seconds"):
            total += float(retry.read_text().strip())
        return total
    return float((log_dir / f"route-{route.lower()}.wall-seconds").read_text().strip())


def route_payload(
    intervals: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    *,
    wall_seconds: float,
    status: str,
) -> dict[str, Any]:
    matched: list[dict[str, Any]] = []
    seen: set[str] = set()
    for interval in intervals:
        for session in matching_sessions(interval, sessions):
            if session["session_id"] in seen:
                continue
            seen.add(session["session_id"])
            matched.append(session)
    matched.sort(key=lambda item: item["started"])
    output_tokens = sum(item["output_tokens"] for item in matched)
    reasoning_tokens = sum(item["reasoning_output_tokens"] for item in matched)
    return {
        "status": status,
        "model_calls": len(matched),
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_tokens,
        "within_4000_token_ceiling": output_tokens <= 4000,
        "wall_seconds": round(wall_seconds, 3),
        "sessions": [
            {
                "session_id": item["session_id"],
                "started_at": item["started_at"],
                "output_tokens": item["output_tokens"],
                "reasoning_output_tokens": item["reasoning_output_tokens"],
            }
            for item in matched
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment-root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--sessions-root", type=Path, default=Path.home() / ".codex" / "sessions")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    sessions = [session for path in args.sessions_root.rglob("*.jsonl") if (session := read_session(path)) is not None]

    cases: dict[str, Any] = {}
    raw_root = args.experiment_root / "raw"
    logs_root = args.experiment_root / "command-logs"
    for case_dir in sorted(raw_root.glob("case-*")):
        case_id = case_dir.name.removeprefix("case-")
        log_dir = logs_root / case_dir.name
        a_interval = run_interval(case_dir / "route-a" / "run.json")
        b_intervals = [run_interval(path) for path in successful_b_runs(case_dir)]
        c_interval = run_interval(case_dir / "route-c" / "run.json")
        cases[case_id] = {
            "A": route_payload(
                [a_interval],
                sessions,
                wall_seconds=read_wall(log_dir, "A"),
                status=a_interval["status"],
            ),
            "B": route_payload(
                b_intervals,
                sessions,
                wall_seconds=read_wall(log_dir, "B"),
                status="ok",
            ),
            "C": route_payload(
                [c_interval],
                sessions,
                wall_seconds=read_wall(log_dir, "C"),
                status=c_interval["status"],
            ),
        }

    payload = {
        "schema": "brigade.deliberation-experiment.tokens.v1",
        "token_definition": (
            "Codex session total_token_usage.output_tokens summed across every model call "
            "attributed to the route interval. This includes reasoning output tokens."
        ),
        "token_ceiling_per_case_route": 4000,
        "cases": cases,
    }
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
