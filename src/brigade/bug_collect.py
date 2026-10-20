"""Closed-schema, local-only diagnostic readers for bug reports."""

from __future__ import annotations

import json
import os
import platform
import re
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Any

from . import __version__, bug_io, bug_last_error, extras, telemetry_export
from .localio import canonical_json_digest

_RECEIPT_BYTE_LIMIT = 256 * 1024
_DIRECTORY_ENTRY_LIMIT = 20_000
_RECEIPT_DIR_LIMIT = 64
_PROJECTION_ROW_LIMIT = 512
_RECEIPT_TIMESTAMP = re.compile(r"^(?:[0-9a-f]{8}\.)?(\d{8}-\d{6})-", re.I)


def _object(path: Path) -> dict[str, Any] | None:
    try:
        raw = bug_io.read_bytes(path, limit=_RECEIPT_BYTE_LIMIT)
        value = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _newest_receipt_dirs(root: Path) -> list[Path]:
    """Sort the bounded listing by creation stamps, using mtime for legacy names."""
    candidates: list[tuple[int, str]] = []
    try:
        with os.scandir(root) as entries:
            for entry in islice(entries, _DIRECTORY_ENTRY_LIMIT):
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stamp = _RECEIPT_TIMESTAMP.match(entry.name)
                        modified = None
                        if stamp is not None:
                            try:
                                created = datetime.strptime(stamp[1], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
                                modified = int(created.timestamp()) * 1_000_000_000
                            except ValueError:
                                pass
                        if modified is None:
                            modified = entry.stat(follow_symlinks=False).st_mtime_ns
                        candidates.append((modified, entry.name))
                except OSError:
                    continue
    except OSError:
        return []
    candidates.sort(reverse=True)
    return [root / name for _modified, name in candidates[:_RECEIPT_DIR_LIMIT]]


def _failed_runs(target: Path) -> list[dict[str, Any]]:
    failed: list[tuple[str, dict[str, Any]]] = []
    remaining = _PROJECTION_ROW_LIMIT
    for directory in _newest_receipt_dirs(target / ".brigade" / "runs"):
        if remaining <= 0:
            break
        run = _object(directory / "run.json")
        roster = _object(directory / "roster.json")
        workers = _object(directory / "worker-results.json")
        if run is None or roster is None or workers is None:
            continue
        agents, results = roster.get("agents"), workers.get("results")
        if not isinstance(agents, dict) or not isinstance(results, list):
            continue
        for result in results[:remaining]:
            remaining -= 1
            if not isinstance(result, dict) or not isinstance(result.get("worker"), str):
                continue
            seat = result["worker"]
            if not isinstance(agents.get(seat), dict) or result.get("ok") is True:
                continue
            failed.append(
                (
                    str(run.get("started_at") or ""),
                    {
                        "id": directory.name,
                        "status": "timed_out" if telemetry_export._error_type(result) == "timeout" else "failed",
                    },
                )
            )
    failed.sort(key=lambda item: item[0], reverse=True)
    runs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _started, row in failed:
        if row["id"] not in seen:
            seen.add(row["id"])
            runs.append(row)
        if len(runs) == 5:
            break
    return runs


def _failed_verify(target: Path) -> dict[str, Any] | None:
    failed: list[tuple[str, dict[str, Any]]] = []
    remaining = _PROJECTION_ROW_LIMIT
    for directory in _newest_receipt_dirs(target / ".brigade" / "work" / "verify-runs"):
        if remaining <= 0:
            break
        receipt = _object(directory / "receipt.json")
        if receipt is None or receipt.get("run_id") != directory.name:
            continue
        commands = receipt.get("commands")
        if not isinstance(commands, list):
            continue
        latest_start: str | None = None
        for command in commands[:remaining]:
            remaining -= 1
            if not isinstance(command, dict):
                continue
            raw_command = command.get("command")
            if not isinstance(raw_command, str) or telemetry_export._verify_executable(raw_command) is None:
                continue
            status = telemetry_export._as_str(command.get("status", receipt.get("status")))
            start = telemetry_export._as_str(command.get("started_at"), iso=True) or telemetry_export._as_str(
                receipt.get("started_at"), iso=True
            )
            end = telemetry_export._as_str(command.get("completed_at"), iso=True) or telemetry_export._as_str(
                receipt.get("completed_at"), iso=True
            )
            if status is None or status == "completed" or start is None or end is None:
                continue
            if latest_start is None or start > latest_start:
                latest_start = start
        if latest_start is not None:
            failed.append((latest_start, {"id": directory.name, "digest": canonical_json_digest(receipt)}))
    return max(failed, key=lambda item: item[0])[1] if failed else None


def collect_doctor(target: Path) -> dict[str, Any]:
    from . import doctor, security_cmd

    ctx = doctor.build_context(target)
    # An explicit allowlist is intentional. Registry callbacks mix local target
    # checks with host probes, managed commands, and authenticated HTTP requests.
    # Filtering their results after invocation cannot make collection offline.
    local = [
        *doctor._check_workspace_files(ctx.target),
        *doctor._check_agents_quality(ctx.target),
        *doctor._check_default_wired_skills(ctx.target, ctx.harnesses),
        *doctor._check_orphan_inboxes(ctx.target, ctx.harnesses),
        doctor._check_recovery_checkpoints(ctx.target),
        doctor._check_unreconciled_run_lock(ctx.target),
        *doctor._check_journal_event_headroom(ctx.target),
        doctor._check_run_lineage(ctx.target),
        *doctor._check_outcome_loop(ctx),
        *doctor.mcp_station_checks(ctx),
    ]
    # General memory checks follow configured source roots and ingestor logs.
    # Exclude that callback entirely, rather than reading private logs and then
    # discarding their details after an unbounded read.
    if "claude" in ctx.harnesses:
        check = doctor._check_claude_work_loop(ctx.target)
        if check is not None:
            local.append(check)
    # The security station also probes host isolation and can run scanners.
    # Read its target configuration only, without executing that callback.
    try:
        config = security_cmd.load_config(ctx.target)
    except ValueError as exc:
        local.append((doctor.FAIL, "security: config", f"invalid config: {exc}"))
    else:
        if config is None:
            local.append((doctor.WARN, "security: config", "missing .brigade/security.toml"))
    checks = [doctor._normalize_scoped_check(check) for check in local[:_PROJECTION_ROW_LIMIT]]
    counts = doctor._status_counts(list(checks))
    return {
        "target": "<target>",
        "checks": [
            {"status": status, "name": name}
            for status, name, _detail, _scope in checks
            if status in {doctor.FAIL, doctor.WARN}
        ],
        "summary": {"failed": counts[doctor.FAIL], "warn": counts[doctor.WARN]},
    }


def recent_failures(target: Path) -> dict[str, Any]:
    return {"runs": _failed_runs(target), "verify": _failed_verify(target)}


def collect(
    target: Path, *, summary: str, title: str, kind: str, doctor: dict[str, Any], output: str
) -> dict[str, Any]:
    # Only these fields may cross the reporting boundary. Doctor's other fields,
    # task text, stdout, roster config, and environment mappings are never copied.
    return {
        "schema": "brigade.bug-report.v1",
        "brigade": {
            "version": __version__,
            "channel": "beta" if "dev" in __version__ else "stable",
            "distribution": "brigade-cli",
            "extras": extras.enabled(),
        },
        "runtime": {
            "python": platform.python_version(),
            "os": platform.system(),
            "release": platform.release(),
            "arch": platform.machine(),
        },
        "doctor": {
            "target": "<target>",
            "checks": [
                {"name": str(c.get("name", "")), "status": str(c.get("status", ""))}
                for c in islice(doctor.get("checks", []), _PROJECTION_ROW_LIMIT)
                if isinstance(c, dict) and c.get("status") in {"FAIL", "WARN"}
            ],
            "summary": {key: doctor.get("summary", {}).get(key, 0) for key in ("failed", "warn")},
        },
        "recent_failures": recent_failures(target),
        "last_error": bug_last_error.read(),
        "user": {"summary": summary, "title": title, "kind": kind, "output_tail": output},
    }
