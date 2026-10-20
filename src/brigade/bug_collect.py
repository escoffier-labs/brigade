"""Closed-schema, local-only diagnostic readers for bug reports."""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Any

from . import __version__, bug_last_error, extras, telemetry_export
from .localio import canonical_json_digest


def collect_doctor(target: Path) -> dict[str, Any]:
    from . import doctor

    ctx = doctor.build_context(target)
    checks = doctor._filter_target_scoped_checks(doctor._gather_checks(ctx))
    counts = doctor._status_counts(list(checks))
    return {
        "target": "<target>",
        "checks": [
            {"status": status, "name": name, "detail": detail}
            for status, name, detail, _scope in checks
            if status in {doctor.FAIL, doctor.WARN}
        ],
        "summary": {"failed": counts[doctor.FAIL], "warn": counts[doctor.WARN]},
    }


def recent_failures(target: Path) -> dict[str, Any]:
    rows, anchors, parents = telemetry_export._project_runs(target, "otel-genai")
    ids = {trace: run_id for run_id, (trace, _span) in anchors.items()}
    failed = sorted(
        (row for row in rows if row.get("status") == "ERROR"),
        key=lambda row: str(row.get("start_time") or ""),
        reverse=True,
    )
    runs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in failed:
        run_id = ids.get(row["trace_id"])
        if run_id is not None and run_id not in seen:
            seen.add(run_id)
            runs.append(
                {
                    "id": run_id,
                    "status": "timed_out" if row.get("error_type") == "timeout" else "failed",
                    "seat": str(row.get("seat", "unknown")),
                }
            )
        if len(runs) == 5:
            break
    verify = telemetry_export._project_verify(target, "otel-genai", parents, anchors)
    verify.sort(key=lambda row: str(row.get("start_time") or ""), reverse=True)
    latest = None
    for row in verify:
        if row.get("status") != "ERROR":
            continue
        receipt_id = row["attributes"].get("brigade.receipt.id")
        if not isinstance(receipt_id, str) or Path(receipt_id).name != receipt_id:
            continue
        receipt = telemetry_export._object(target / ".brigade" / "work" / "verify-runs" / receipt_id / "receipt.json")
        if receipt is not None:
            latest = {"id": receipt_id, "digest": canonical_json_digest(receipt)}
            break
    return {"runs": runs, "verify": latest}


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
                {"name": str(c.get("name", "")), "status": str(c.get("status", "")), "detail": str(c.get("detail", ""))}
                for c in doctor.get("checks", [])
                if isinstance(c, dict) and c.get("status") in {"FAIL", "WARN"}
            ],
            "summary": {key: doctor.get("summary", {}).get(key, 0) for key in ("failed", "warn")},
        },
        "recent_failures": recent_failures(target),
        "last_error": bug_last_error.read(),
        "user": {"summary": summary, "title": title, "kind": kind, "output_tail": output},
    }
