"""Closed-schema, local-only diagnostic readers for bug reports."""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Any

from . import __version__, bug_io, bug_last_error, component_paths, extras, telemetry_export
from .isolated_exec import module_argv
from .localio import canonical_json_digest

_DOCTOR_CHECK_TIMEOUT = 5.0
_DOCTOR_ENTRY_LIMIT = 8
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


def collect_diagnostics(target: Path) -> dict[str, Any]:
    # General Doctor callbacks can enumerate whole run trees, follow configured
    # roots, read arbitrary files, or contact services. Run only report-specific
    # fixed-path checks, each in a killable process with a hard deadline.
    checks: list[dict[str, str]] = []
    target = target.resolve()
    # Pin the package and ignore inherited Python paths and user-site startup.
    with tempfile.TemporaryDirectory(prefix="brigade-bug-diagnostics-") as cwd:
        for name in ("bootstrap", "agents-quality", "security-config"):
            try:
                result = subprocess.run(
                    module_argv("brigade.bug_doctor", str(target), name, str(_DOCTOR_ENTRY_LIMIT)),
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=_DOCTOR_CHECK_TIMEOUT,
                    check=True,
                )
                checks.extend(json.loads(result.stdout))
            except (OSError, ValueError, subprocess.SubprocessError):
                checks.append({"status": "WARN", "name": f"bug diagnostics: {name} budget or check unavailable"})
    return {
        "target": "<target>",
        "checks": checks,
        "summary": {
            "failed": sum(c["status"] == "FAIL" for c in checks),
            "warn": sum(c["status"] == "WARN" for c in checks),
        },
    }


def recent_failures(target: Path) -> dict[str, Any]:
    return {"runs": _failed_runs(target), "verify": _failed_verify(target)}


def _install_channel() -> str:
    # The updater persists the installed channel separately from the version.
    # Read only bounded local metadata. Missing or stale state proves neither
    # stable nor beta, including source checkouts without an update record.
    state = _object(Path(component_paths.update_state_path(component_paths.data_root())))
    if (
        state is not None
        and state.get("schema_version") == 1
        and state.get("cli_coordinate") == __version__
        and state.get("channel") in ("stable", "beta")
    ):
        return str(state["channel"])
    return "unknown"


def collect(
    target: Path, *, summary: str, title: str, kind: str, diagnostics: dict[str, Any], output: str
) -> dict[str, Any]:
    # Only these fields may cross the reporting boundary. Diagnostic details,
    # task text, stdout, roster config, and environment mappings are never copied.
    return {
        "schema": "brigade.bug-report.v1",
        "brigade": {
            "version": __version__,
            "channel": _install_channel(),
            "distribution": "brigade-cli",
            "extras": extras.enabled(),
        },
        "runtime": {
            "python": platform.python_version(),
            "os": platform.system(),
            "release": platform.release(),
            "arch": platform.machine(),
        },
        "bug_diagnostics": {
            "target": "<target>",
            "checks": [
                {"name": str(c.get("name", "")), "status": str(c.get("status", ""))}
                for c in islice(diagnostics.get("checks", []), _PROJECTION_ROW_LIMIT)
                if isinstance(c, dict) and c.get("status") in {"FAIL", "WARN"}
            ],
            "summary": {key: diagnostics.get("summary", {}).get(key, 0) for key in ("failed", "warn")},
        },
        "recent_failures": recent_failures(target),
        "last_error": bug_last_error.read(),
        "user": {"summary": summary, "title": title, "kind": kind, "output_tail": output},
    }
