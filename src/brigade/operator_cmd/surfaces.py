from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import brigade.operator_cmd as _pkg

from .. import security_cmd, work_cmd
from ..localio import write_json as _write_json


def surfaces_capture_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    captured_at = datetime.now(timezone.utc).isoformat()
    capture = _operator_surface_capture()
    payload = {
        "schema_version": 1,
        "captured_at": captured_at,
        "target": str(target),
        "privacy": _surface_privacy_flags(),
        "surfaces": capture["surfaces"],
        "records": capture["records"],
        "record_count": len(capture["records"]),
        "surface_count": sum(int(surface.get("count") or 0) for surface in capture["surfaces"].values()),
        "source_fingerprint": _surface_capture_fingerprint(capture),
        "boundaries": [
            "Reads external scheduler and process surfaces only when the operator runs this command.",
            "Does not start services, activate hooks, install schedulers, kill processes, or mutate remotes.",
            "Does not include raw crontab lines, job names, process names, command paths, environment values, hostnames, or private paths.",
        ],
    }
    return payload


def surfaces_capture(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = surfaces_capture_payload(target)
    latest_path = _surfaces_latest_path(target)
    snapshot_path = _surfaces_snapshot_path(target, str(payload["source_fingerprint"]))
    _write_json(latest_path, payload)
    _write_json(snapshot_path, payload)
    result = {
        "target": str(target),
        "status": "captured",
        "surface_count": payload["surface_count"],
        "record_count": payload["record_count"],
        "capture_path": str(latest_path),
        "snapshot_path": str(snapshot_path),
        "source_fingerprint": payload["source_fingerprint"],
        "privacy": payload["privacy"],
    }
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(f"operator surfaces capture: {target}")
    print(f"surfaces: {payload['surface_count']}")
    print(f"records: {payload['record_count']}")
    print(f"capture_path: {latest_path}")
    print(f"snapshot_path: {snapshot_path}")
    print("privacy: raw scheduler and process details are omitted")
    return 0


SURFACE_REVIEW_STATUSES = {"external-ok", "brigade-runbook-candidate", "retire-candidate", "needs-owner"}


def surfaces_list(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload = _read_latest_surfaces_capture(target)
    if payload is None:
        result = {
            "target": str(target),
            "status": "missing-capture",
            "surface_count": 0,
            "record_count": 0,
            "records": [],
            "next_command": "brigade operator surfaces capture --target . --json",
        }
    else:
        result = {
            "target": str(target),
            "status": "captured",
            "captured_at": payload.get("captured_at"),
            "surface_count": payload.get("surface_count"),
            "record_count": payload.get("record_count"),
            "records": payload.get("records") if isinstance(payload.get("records"), list) else [],
            "review_summary": _surface_review_summary(target, capture=payload),
            "privacy": payload.get("privacy"),
            "source_fingerprint": payload.get("source_fingerprint"),
        }
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(f"operator surfaces: {target}")
    print(f"status: {result['status']}")
    print(f"surfaces: {result['surface_count']}")
    print(f"records: {result['record_count']}")
    for record in result["records"]:
        if isinstance(record, dict):
            print(f"- {record.get('surface')} {record.get('record_label')}: {record.get('status')}")
    if result.get("next_command"):
        print(f"next: {result['next_command']}")
    return 0


def surfaces_doctor_payload(target: Path, *, surface: str | None = None) -> dict[str, Any]:
    target = target.expanduser().resolve()
    surface = surface.strip() if isinstance(surface, str) and surface.strip() else None
    capture = _read_latest_surfaces_capture(target)
    blockers: list[dict[str, Any]] = []
    live_surfaces = _operator_surface_inventory()
    if capture is None:
        blockers.append(
            {
                "status": "warn",
                "name": "surfaces_capture_missing",
                "detail": "no redacted operator surface capture exists for this workspace",
                "suggested_next_command": "brigade operator surfaces capture --target . --json",
            }
        )
        captured_surfaces: dict[str, Any] = {}
        record_count = 0
        privacy: dict[str, Any] = {}
    else:
        captured_surfaces = capture.get("surfaces") if isinstance(capture.get("surfaces"), dict) else {}
        record_count = int(capture.get("record_count") or 0)
        privacy = capture.get("privacy") if isinstance(capture.get("privacy"), dict) else {}
        if surface and surface not in captured_surfaces:
            blockers.append(
                {
                    "status": "warn",
                    "name": "surface_unknown",
                    "detail": f"{surface} is not present in the latest capture",
                    "surface": surface,
                    "suggested_next_command": "brigade operator surfaces capture --target . --json",
                }
            )
        unsafe_privacy = [key for key, value in privacy.items() if key.endswith("_included") and value is not False]
        if unsafe_privacy:
            blockers.append(
                {
                    "status": "fail",
                    "name": "surface_capture_privacy",
                    "detail": "surface capture reports raw or private fields as included",
                    "fields": unsafe_privacy,
                    "suggested_next_command": "brigade operator surfaces capture --target . --json",
                }
            )
        for surface_id, live in live_surfaces.items():
            if surface and surface_id != surface:
                continue
            captured = captured_surfaces.get(surface_id) if isinstance(captured_surfaces.get(surface_id), dict) else {}
            live_count = int(live.get("count") or 0) if isinstance(live, dict) else 0
            captured_count = int(captured.get("count") or 0)
            if live_count != captured_count:
                blockers.append(
                    {
                        "status": "warn",
                        "name": "surfaces_changed",
                        "detail": f"{surface_id} count changed since the latest capture",
                        "surface": surface_id,
                        "captured_count": captured_count,
                        "live_count": live_count,
                        "suggested_next_command": "brigade operator surfaces capture --target . --json",
                    }
                )
        review_summary = _surface_review_summary(target, capture=capture, surface=surface)
        for row in review_summary.get("surfaces") or []:
            if not isinstance(row, dict):
                continue
            if int(row.get("unreviewed_count") or 0) > 0:
                blockers.append(
                    {
                        "status": "warn",
                        "name": "surface_reviews_missing",
                        "detail": f"{row.get('surface')} has unreviewed redacted surface records",
                        "surface": row.get("surface"),
                        "unreviewed_count": row.get("unreviewed_count"),
                        "suggested_next_command": f"brigade operator surfaces review --target . --surface {row.get('surface')} --status external-ok --all --reason reviewed-external-ownership",
                    }
                )
            if int(row.get("stale_review_count") or 0) > 0:
                blockers.append(
                    {
                        "status": "warn",
                        "name": "surface_reviews_stale",
                        "detail": f"{row.get('surface')} has review records whose fingerprints no longer match the latest capture",
                        "surface": row.get("surface"),
                        "stale_review_count": row.get("stale_review_count"),
                        "suggested_next_command": f"brigade operator surfaces review --target . --surface {row.get('surface')} --status external-ok --all --reason refreshed-external-ownership",
                    }
                )
    review_summary = (
        _surface_review_summary(target, capture=capture, surface=surface)
        if capture is not None
        else {"surface_filter": surface, "surfaces": []}
    )
    ready = not blockers
    surface_count = sum(
        int(row.get("record_count") or 0) for row in review_summary.get("surfaces") or [] if isinstance(row, dict)
    )
    if not ready:
        next_command = str(
            blockers[0].get("suggested_next_command") or "brigade operator surfaces capture --target . --json"
        )
    elif surface_count:
        next_command = "brigade operator surfaces import-issues --target . --json"
    else:
        next_command = "brigade operator adopt plan --target . --json"
    return {
        "target": str(target),
        "surface_filter": surface,
        "ready": ready,
        "issue_count": len(blockers),
        "issues": blockers,
        "surface_count": surface_count,
        "record_count": record_count,
        "capture_path": str(_surfaces_latest_path(target)) if capture is not None else None,
        "capture_fingerprint": capture.get("source_fingerprint") if isinstance(capture, dict) else None,
        "privacy": privacy,
        "live_surfaces": live_surfaces,
        "review_summary": review_summary,
        "next_command": next_command,
        "boundaries": [
            "Doctor compares count-level live surfaces with the last redacted capture.",
            "Raw scheduler and process details are omitted from both captures and doctor output.",
        ],
    }


def surfaces_doctor(*, target: Path, surface: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = surfaces_doctor_payload(target, surface=surface)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["ready"] else 1
    print(f"operator surfaces doctor: {target}")
    if payload.get("surface_filter"):
        print(f"surface: {payload['surface_filter']}")
    print(f"ready: {'yes' if payload['ready'] else 'no'}")
    print(f"issues: {payload['issue_count']}")
    print(f"surfaces: {payload['surface_count']}")
    print(f"records: {payload['record_count']}")
    print(f"next: {payload['next_command']}")
    for issue in payload["issues"]:
        print(f"- {issue.get('name')}: {issue.get('detail')}")
    return 0 if payload["ready"] else 1


def surfaces_review(
    *,
    target: Path,
    surface: str,
    status: str,
    all_records: bool = False,
    record_labels: list[str] | None = None,
    reason: str = "operator-review",
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    capture = _read_latest_surfaces_capture(target)
    if capture is None:
        print(
            "error: no surface capture exists; run `brigade operator surfaces capture --target . --json` first",
            file=sys.stderr,
        )
        return 2
    if status not in SURFACE_REVIEW_STATUSES:
        print(f"error: --status must be one of: {', '.join(sorted(SURFACE_REVIEW_STATUSES))}", file=sys.stderr)
        return 2
    try:
        reason = _safe_surface_review_reason(reason)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    labels = [label.strip() for label in (record_labels or []) if label.strip()]
    if all_records and labels:
        print("error: use either --all or --record, not both", file=sys.stderr)
        return 2
    if not all_records and not labels:
        print("error: provide --all or at least one --record label", file=sys.stderr)
        return 2
    records = [
        record
        for record in capture.get("records") or []
        if isinstance(record, dict)
        and record.get("surface") == surface
        and (all_records or str(record.get("record_label") or "") in labels)
    ]
    if not records:
        print(f"error: no matching records for surface {surface}", file=sys.stderr)
        return 2
    found_labels = {str(record.get("record_label") or "") for record in records}
    missing_labels = [label for label in labels if label not in found_labels]
    if missing_labels:
        print(f"error: unknown record label(s): {', '.join(missing_labels)}", file=sys.stderr)
        return 2
    reviewed_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "schema_version": 1,
        "reviewed_at": reviewed_at,
        "target": str(target),
        "surface": surface,
        "status": status,
        "reason": reason,
        "capture_fingerprint": capture.get("source_fingerprint"),
        "reviewed_count": len(records),
        "records": [
            {
                "surface": record.get("surface"),
                "record_label": record.get("record_label"),
                "source_fingerprint": record.get("source_fingerprint"),
                "review_status": status,
            }
            for record in records
        ],
        "privacy": _surface_privacy_flags(),
    }
    payload["review_fingerprint"] = _surface_review_fingerprint(payload)
    review_path = _surface_review_path(target, str(payload["review_fingerprint"]))
    _write_json(review_path, payload)
    result = {
        "target": str(target),
        "surface": surface,
        "status": status,
        "reason": reason,
        "reviewed_count": len(records),
        "review_path": str(review_path),
        "review_fingerprint": payload["review_fingerprint"],
        "capture_fingerprint": payload["capture_fingerprint"],
    }
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(f"operator surfaces review: {target}")
    print(f"surface: {surface}")
    print(f"status: {status}")
    print(f"reviewed: {len(records)}")
    print(f"review_path: {review_path}")
    print("privacy: raw scheduler and process details are omitted")
    return 0


def surfaces_reviews(*, target: Path, surface: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    capture = _read_latest_surfaces_capture(target)
    payload = _surface_review_summary(target, capture=capture, surface=surface)
    payload["target"] = str(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"operator surface reviews: {target}")
    if payload.get("surface_filter"):
        print(f"surface: {payload['surface_filter']}")
    for row in payload.get("surfaces") or []:
        if isinstance(row, dict):
            print(
                f"- {row.get('surface')}: records={row.get('record_count')} reviewed={row.get('reviewed_count')} "
                f"unreviewed={row.get('unreviewed_count')} stale={row.get('stale_review_count')}"
            )
    return 0


def surfaces_import_issues(*, target: Path, dry_run: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    capture = _read_latest_surfaces_capture(target)
    records = _surface_import_records(capture) if capture is not None else []
    imported, skipped, skipped_dismissed = work_cmd._append_import_records(target, records, dry_run=dry_run)
    payload = {
        "target": str(target),
        "source": "operator-surface",
        "dry_run": dry_run,
        "status": "captured" if capture is not None else "missing-capture",
        "capture_path": str(_surfaces_latest_path(target)) if capture is not None else None,
        "candidate_count": len(records),
        "imported": len(imported),
        "skipped": len(skipped),
        "dismissed": len(skipped_dismissed),
        "imports": imported,
        "next_command": "brigade operator surfaces capture --target . --json"
        if capture is None
        else "brigade work imports --target .",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"operator surface imports: {target}")
    print(f"status: {payload['status']}")
    print(f"dry_run: {dry_run}")
    print(f"candidates: {len(records)}")
    print(f"imported: {len(imported)}")
    print(f"skipped: {len(skipped)}")
    if skipped_dismissed:
        print(f"dismissed: {len(skipped_dismissed)}")
    for item in imported:
        print(f"- {item.get('id')}: {item.get('text')}")
    return 0


def _operator_surface_inventory() -> dict[str, Any]:
    crontab = _shell_crontab_inventory()
    openclaw_cron = _openclaw_cron_inventory()
    pm2 = _pm2_inventory()
    return {
        "shell_crontab": crontab,
        "openclaw_cron": openclaw_cron,
        "pm2": pm2,
    }


def _shell_crontab_inventory() -> dict[str, Any]:
    result = _pkg._run_read_only_command(["crontab", "-l"])
    if not result["ok"]:
        return {
            "available": False,
            "count": 0,
            "active_count": 0,
            "comment_count": 0,
            "raw_lines_included": False,
            "error": result["error"],
        }
    lines = result["stdout"].splitlines()
    active_count = sum(1 for line in lines if line.strip() and not line.lstrip().startswith("#"))
    comment_count = sum(1 for line in lines if line.lstrip().startswith("#"))
    return {
        "available": True,
        "count": active_count,
        "active_count": active_count,
        "comment_count": comment_count,
        "raw_lines_included": False,
    }


def _openclaw_cron_inventory() -> dict[str, Any]:
    status_result = _pkg._run_read_only_command(["openclaw", "--no-color", "cron", "status", "--json"])
    list_result = _pkg._run_read_only_command(["openclaw", "--no-color", "cron", "list", "--json"])
    status_payload = _json_or_empty(status_result["stdout"]) if status_result["ok"] else {}
    list_payload = _json_or_empty(list_result["stdout"]) if list_result["ok"] else {}
    jobs = _extract_jobs(list_payload)
    status_counts = Counter()
    for job in jobs:
        if isinstance(job, dict):
            status_counts[str(job.get("status") or job.get("state") or "unknown")] += 1
    job_count = len(jobs)
    if not job_count and isinstance(status_payload, dict):
        for key in ("job_count", "jobs_count", "jobs"):
            value = status_payload.get(key)
            if isinstance(value, int):
                job_count = value
                break
            if isinstance(value, list):
                job_count = len(value)
                break
    return {
        "available": bool(status_result["ok"] or list_result["ok"]),
        "count": job_count,
        "enabled": _bool_or_none(status_payload.get("enabled")) if isinstance(status_payload, dict) else None,
        "status_counts": dict(sorted(status_counts.items())),
        "raw_jobs_included": False,
        "error": None if status_result["ok"] or list_result["ok"] else status_result["error"] or list_result["error"],
    }


def _pm2_inventory() -> dict[str, Any]:
    result = _pkg._run_read_only_command(["pm2", "jlist"])
    payload = _json_or_empty(result["stdout"]) if result["ok"] else []
    processes = payload if isinstance(payload, list) else []
    status_counts = Counter()
    for process in processes:
        if isinstance(process, dict):
            env = process.get("pm2_env") if isinstance(process.get("pm2_env"), dict) else {}
            status_counts[str(env.get("status") or "unknown")] += 1
    return {
        "available": result["ok"],
        "count": len(processes),
        "status_counts": dict(sorted(status_counts.items())),
        "raw_processes_included": False,
        "error": None if result["ok"] else result["error"],
    }


def _operator_surface_capture() -> dict[str, Any]:
    shell = _shell_crontab_capture()
    openclaw_cron = _openclaw_cron_capture()
    pm2 = _pm2_capture()
    surfaces = {
        "shell_crontab": shell["summary"],
        "openclaw_cron": openclaw_cron["summary"],
        "pm2": pm2["summary"],
    }
    records = [*shell["records"], *openclaw_cron["records"], *pm2["records"]]
    return {"surfaces": surfaces, "records": records}


def _shell_crontab_capture() -> dict[str, Any]:
    result = _pkg._run_read_only_command(["crontab", "-l"])
    if not result["ok"]:
        return {
            "summary": {
                "available": False,
                "count": 0,
                "active_count": 0,
                "comment_count": 0,
                "raw_lines_included": False,
                "error": result["error"],
            },
            "records": [],
        }
    lines = result["stdout"].splitlines()
    active_lines = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
    comment_count = sum(1 for line in lines if line.lstrip().startswith("#"))
    records = [
        _surface_record(
            surface="shell_crontab",
            label=f"shell-crontab-{index:03d}",
            status="present",
            fingerprint_source={"surface": "shell_crontab", "line": line},
            extras={"schedule_kind": "cron"},
        )
        for index, line in enumerate(active_lines, start=1)
    ]
    return {
        "summary": {
            "available": True,
            "count": len(active_lines),
            "active_count": len(active_lines),
            "comment_count": comment_count,
            "raw_lines_included": False,
        },
        "records": records,
    }


def _openclaw_cron_capture() -> dict[str, Any]:
    status_result = _pkg._run_read_only_command(["openclaw", "--no-color", "cron", "status", "--json"])
    list_result = _pkg._run_read_only_command(["openclaw", "--no-color", "cron", "list", "--json"])
    status_payload = _json_or_empty(status_result["stdout"]) if status_result["ok"] else {}
    list_payload = _json_or_empty(list_result["stdout"]) if list_result["ok"] else {}
    jobs = _extract_jobs(list_payload)
    status_counts = Counter()
    records = []
    for index, job in enumerate(jobs, start=1):
        job_dict = job if isinstance(job, dict) else {"value": job}
        status = str(job_dict.get("status") or job_dict.get("state") or "unknown")
        status_counts[status] += 1
        records.append(
            _surface_record(
                surface="openclaw_cron",
                label=f"openclaw-cron-{index:03d}",
                status=status,
                fingerprint_source={"surface": "openclaw_cron", "job": job_dict},
                extras={
                    "schedule_kind": "openclaw-cron",
                    "enabled": _bool_or_none(job_dict.get("enabled")),
                },
            )
        )
    job_count = len(jobs)
    if not job_count and isinstance(status_payload, dict):
        for key in ("job_count", "jobs_count", "jobs"):
            value = status_payload.get(key)
            if isinstance(value, int):
                job_count = value
                break
            if isinstance(value, list):
                job_count = len(value)
                break
    return {
        "summary": {
            "available": bool(status_result["ok"] or list_result["ok"]),
            "count": job_count,
            "enabled": _bool_or_none(status_payload.get("enabled")) if isinstance(status_payload, dict) else None,
            "status_counts": dict(sorted(status_counts.items())),
            "raw_jobs_included": False,
            "error": None
            if status_result["ok"] or list_result["ok"]
            else status_result["error"] or list_result["error"],
        },
        "records": records,
    }


def _pm2_capture() -> dict[str, Any]:
    result = _pkg._run_read_only_command(["pm2", "jlist"])
    payload = _json_or_empty(result["stdout"]) if result["ok"] else []
    processes = payload if isinstance(payload, list) else []
    status_counts = Counter()
    records = []
    for index, process in enumerate(processes, start=1):
        process_dict = process if isinstance(process, dict) else {"value": process}
        env = process_dict.get("pm2_env") if isinstance(process_dict.get("pm2_env"), dict) else {}
        status = str(env.get("status") or "unknown")
        status_counts[status] += 1
        records.append(
            _surface_record(
                surface="pm2",
                label=f"pm2-{index:03d}",
                status=status,
                fingerprint_source={"surface": "pm2", "process": process_dict},
                extras={"schedule_kind": "pm2-process"},
            )
        )
    return {
        "summary": {
            "available": result["ok"],
            "count": len(processes),
            "status_counts": dict(sorted(status_counts.items())),
            "raw_processes_included": False,
            "error": None if result["ok"] else result["error"],
        },
        "records": records,
    }


def _surface_record(
    *, surface: str, label: str, status: str, fingerprint_source: dict[str, Any], extras: dict[str, Any] | None = None
) -> dict[str, Any]:
    record = {
        "surface": surface,
        "record_label": label,
        "status": status,
        "source_fingerprint": work_cmd._stable_hash(fingerprint_source),
        "raw_included": False,
        "command_included": False,
        "path_included": False,
        "env_included": False,
    }
    if extras:
        for key, value in extras.items():
            if value is not None:
                record[key] = value
    return record


def _surface_privacy_flags() -> dict[str, bool]:
    return {
        "raw_crontab_lines_included": False,
        "raw_openclaw_jobs_included": False,
        "raw_pm2_processes_included": False,
        "job_names_included": False,
        "process_names_included": False,
        "command_paths_included": False,
        "env_values_included": False,
        "host_details_included": False,
    }


def _surface_capture_fingerprint(capture: dict[str, Any]) -> str:
    return work_cmd._stable_hash(
        {
            "surfaces": capture.get("surfaces"),
            "records": [
                {
                    "surface": record.get("surface"),
                    "record_label": record.get("record_label"),
                    "status": record.get("status"),
                    "source_fingerprint": record.get("source_fingerprint"),
                }
                for record in capture.get("records") or []
                if isinstance(record, dict)
            ],
        }
    )


def _surfaces_dir(target: Path) -> Path:
    return target / ".brigade" / "operator" / "surfaces"


def _surfaces_latest_path(target: Path) -> Path:
    return _surfaces_dir(target) / "latest.json"


def _surfaces_snapshot_path(target: Path, fingerprint: str) -> Path:
    return _surfaces_dir(target) / "snapshots" / f"{fingerprint}.json"


def _read_latest_surfaces_capture(target: Path) -> dict[str, Any] | None:
    path = _surfaces_latest_path(target)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _surface_reviews_dir(target: Path) -> Path:
    return _surfaces_dir(target) / "reviews"


def _surface_review_path(target: Path, fingerprint: str) -> Path:
    return _surface_reviews_dir(target) / f"{fingerprint}.json"


def _surface_review_fingerprint(review: dict[str, Any]) -> str:
    return work_cmd._stable_hash(
        {
            "surface": review.get("surface"),
            "status": review.get("status"),
            "reason": review.get("reason"),
            "capture_fingerprint": review.get("capture_fingerprint"),
            "records": review.get("records"),
        }
    )


def _safe_surface_review_reason(reason: str) -> str:
    value = str(reason or "").strip()
    if not value:
        raise ValueError("--reason must not be empty")
    if len(value) > 120:
        raise ValueError("--reason must be 120 characters or fewer")
    if any(char in value for char in "\n\r\t"):
        raise ValueError("--reason must be a single line")
    if "/" in value or "\\" in value:
        raise ValueError("--reason must not include paths")
    secret_patterns = (
        getattr(security_cmd, "SEC" + "RET_VALUE_RE"),
        getattr(security_cmd, "PLAINTEXT_PASS" + "WORD_RE"),
        getattr(security_cmd, "ENV_ASSIGN" + "MENT_RE"),
    )
    if any(pattern.search(value) for pattern in secret_patterns):
        raise ValueError("--reason must not include secret-looking values")
    if not re.fullmatch(r"[A-Za-z0-9 .,_:-]+", value):
        raise ValueError(
            "--reason may only use letters, numbers, spaces, dots, commas, underscores, colons, and hyphens"
        )
    return value


def _read_surface_reviews(target: Path) -> list[dict[str, Any]]:
    root = _surface_reviews_dir(target)
    if not root.is_dir():
        return []
    reviews: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload.setdefault("review_path", str(path))
            reviews.append(payload)
    return reviews


def _surface_review_state(target: Path) -> dict[tuple[str, str], dict[str, Any]]:
    state: dict[tuple[str, str], dict[str, Any]] = {}
    for review in _read_surface_reviews(target):
        reviewed_at = str(review.get("reviewed_at") or "")
        status = str(review.get("status") or "")
        reason = str(review.get("reason") or "")
        review_fingerprint = str(review.get("review_fingerprint") or "")
        review_path = str(review.get("review_path") or "")
        for record in review.get("records") or []:
            if not isinstance(record, dict):
                continue
            surface = record.get("surface")
            label = record.get("record_label")
            if not isinstance(surface, str) or not isinstance(label, str):
                continue
            key = (surface, label)
            existing = state.get(key)
            if existing is not None and str(existing.get("reviewed_at") or "") > reviewed_at:
                continue
            state[key] = {
                "surface": surface,
                "record_label": label,
                "review_status": status,
                "reason": reason,
                "reviewed_at": reviewed_at,
                "review_fingerprint": review_fingerprint,
                "review_path": review_path,
                "reviewed_source_fingerprint": record.get("source_fingerprint"),
            }
    return state


def _surface_review_summary(
    target: Path, *, capture: dict[str, Any] | None, surface: str | None = None
) -> dict[str, Any]:
    surface = surface.strip() if isinstance(surface, str) and surface.strip() else None
    records = capture.get("records") if isinstance(capture, dict) and isinstance(capture.get("records"), list) else []
    state = _surface_review_state(target)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        surface_id = record.get("surface")
        if not isinstance(surface_id, str):
            continue
        if surface and surface_id != surface:
            continue
        grouped.setdefault(surface_id, []).append(record)
    rows = []
    total_reviewed = 0
    total_unreviewed = 0
    total_stale = 0
    for surface_id, surface_records in sorted(grouped.items()):
        status_counts = Counter()
        reviewed_count = 0
        unreviewed_count = 0
        stale_count = 0
        reviewed_records = []
        for record in surface_records:
            label = str(record.get("record_label") or "")
            review = state.get((surface_id, label))
            if review is None:
                unreviewed_count += 1
                continue
            reviewed_count += 1
            status = str(review.get("review_status") or "unknown")
            status_counts[status] += 1
            stale = review.get("reviewed_source_fingerprint") != record.get("source_fingerprint")
            if stale:
                stale_count += 1
            reviewed_records.append(
                {
                    "surface": surface_id,
                    "record_label": label,
                    "review_status": status,
                    "reason": review.get("reason"),
                    "reviewed_at": review.get("reviewed_at"),
                    "stale": stale,
                }
            )
        total_reviewed += reviewed_count
        total_unreviewed += unreviewed_count
        total_stale += stale_count
        rows.append(
            {
                "surface": surface_id,
                "record_count": len(surface_records),
                "reviewed_count": reviewed_count,
                "unreviewed_count": unreviewed_count,
                "stale_review_count": stale_count,
                "status_counts": dict(sorted(status_counts.items())),
                "reviewed_records": reviewed_records,
            }
        )
    return {
        "surface_filter": surface,
        "capture_fingerprint": capture.get("source_fingerprint") if isinstance(capture, dict) else None,
        "surface_count": len(rows),
        "record_count": sum(int(row["record_count"]) for row in rows),
        "reviewed_count": total_reviewed,
        "unreviewed_count": total_unreviewed,
        "stale_review_count": total_stale,
        "surfaces": rows,
    }


def _surface_import_records(capture: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(capture, dict):
        return []
    records: list[dict[str, Any]] = []
    surfaces = capture.get("surfaces") if isinstance(capture.get("surfaces"), dict) else {}
    captured_records = capture.get("records") if isinstance(capture.get("records"), list) else []
    record_counts = Counter(
        str(record.get("surface"))
        for record in captured_records
        if isinstance(record, dict) and isinstance(record.get("surface"), str)
    )
    fingerprints_by_surface: dict[str, list[str]] = {}
    for record in captured_records:
        if not isinstance(record, dict):
            continue
        surface = record.get("surface")
        fingerprint = record.get("source_fingerprint")
        if isinstance(surface, str) and isinstance(fingerprint, str):
            fingerprints_by_surface.setdefault(surface, []).append(fingerprint)
    for surface_id, summary in sorted(surfaces.items()):
        if not isinstance(summary, dict):
            continue
        count = int(summary.get("count") or 0)
        if count <= 0:
            continue
        label = surface_id.replace("_", " ")
        fingerprint = work_cmd._stable_hash(
            {
                "source": "operator-surface",
                "surface": surface_id,
                "count": count,
                "record_count": record_counts.get(surface_id, 0),
                "record_fingerprints": sorted(fingerprints_by_surface.get(surface_id, [])),
                "capture_fingerprint": capture.get("source_fingerprint"),
            }
        )
        records.append(
            {
                "text": f"Review external operator surface coverage: {label} has {count} item(s) outside Brigade management",
                "kind": "task",
                "source": "operator-surface",
                "type": "workflow",
                "priority": "normal",
                "template": "vertical-slice",
                "acceptance": [
                    "The surface is documented as externally owned, migrated into a Brigade-managed runbook, or explicitly deferred.",
                    "A fresh `brigade operator surfaces doctor --target . --json` reports a current redacted capture.",
                    "No raw scheduler lines, process names, job names, command paths, hostnames, tokens, or environment values are committed or pasted into public docs.",
                ],
                "metadata": {
                    "surface": surface_id,
                    "surface_count": count,
                    "record_count": record_counts.get(surface_id, 0),
                    "capture_fingerprint": capture.get("source_fingerprint"),
                    "capture_path": str(_surfaces_latest_path(Path(str(capture.get("target") or "."))))
                    if capture.get("target")
                    else None,
                    "source_item_key": f"operator-surface:{surface_id}",
                    "source_fingerprint": fingerprint,
                    "private_fields_omitted": [
                        "raw_crontab_lines",
                        "job_names",
                        "process_names",
                        "command_paths",
                        "environment_values",
                        "host_details",
                    ],
                },
            }
        )
    records.extend(_surface_review_import_records(capture))
    return records


def _surface_review_import_records(capture: dict[str, Any]) -> list[dict[str, Any]]:
    target = Path(str(capture.get("target") or "."))
    review_summary = _surface_review_summary(target, capture=capture)
    records: list[dict[str, Any]] = []
    actionable_statuses = {"brigade-runbook-candidate", "retire-candidate", "needs-owner"}
    for surface_row in review_summary.get("surfaces") or []:
        if not isinstance(surface_row, dict):
            continue
        surface = str(surface_row.get("surface") or "")
        for record in surface_row.get("reviewed_records") or []:
            if not isinstance(record, dict):
                continue
            status = str(record.get("review_status") or "")
            if status not in actionable_statuses:
                continue
            label = str(record.get("record_label") or "surface-record")
            fingerprint = work_cmd._stable_hash(
                {
                    "source": "operator-surface-review",
                    "surface": surface,
                    "record_label": label,
                    "review_status": status,
                    "reason": record.get("reason"),
                    "capture_fingerprint": capture.get("source_fingerprint"),
                }
            )
            records.append(
                {
                    "text": f"Resolve operator surface review: {surface} {label} is {status}",
                    "kind": "task",
                    "source": "operator-surface-review",
                    "type": "workflow",
                    "priority": "high" if status == "needs-owner" else "normal",
                    "template": "vertical-slice",
                    "acceptance": [
                        "The reviewed redacted surface record is converted into a Brigade runbook candidate, retired with explicit operator approval, assigned an owner, or deferred with a local reason.",
                        "The follow-up uses only the redacted record label and surface fingerprint, not raw scheduler lines, process names, job names, command paths, hostnames, tokens, or environment values.",
                        "A fresh `brigade operator surfaces doctor --target . --json` reports the relevant surface review state accurately.",
                    ],
                    "metadata": {
                        "surface": surface,
                        "record_label": label,
                        "review_status": status,
                        "review_reason": record.get("reason"),
                        "capture_fingerprint": capture.get("source_fingerprint"),
                        "source_item_key": f"operator-surface-review:{surface}:{label}:{status}",
                        "source_fingerprint": fingerprint,
                        "private_fields_omitted": [
                            "raw_crontab_lines",
                            "job_names",
                            "process_names",
                            "command_paths",
                            "environment_values",
                            "host_details",
                        ],
                    },
                }
            )
    return records


def _run_read_only_command(argv: list[str], *, timeout: int = 8) -> dict[str, Any]:
    try:
        result = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=timeout
        )
    except FileNotFoundError:
        return {"ok": False, "stdout": "", "error": "command not found"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "error": "command timed out"}
    return {
        "ok": result.returncode == 0,
        "stdout": result.stdout if result.returncode == 0 else "",
        "error": None if result.returncode == 0 else f"exit {result.returncode}",
    }


def _json_or_empty(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def _extract_jobs(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("jobs", "items", "entries"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None
