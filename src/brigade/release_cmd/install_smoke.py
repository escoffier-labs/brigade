"""Local release readiness receipts."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import (
    context_cmd,
    handoff_cmd,
    learn_cmd,
    memory_cmd,
    phases_cmd,
    projects_cmd,
    repos_cmd,
    reportstore,
    research_cmd,
    roadmap_cmd,
    scrub,
    security_cmd,
    tools_cmd,
    work_cmd,
)
from ..selection import KNOWN_HARNESSES
from ..localio import (
    read_json_dict as _read_json,
    read_jsonl_dicts as _read_jsonl,
    utc_now as _now,
    write_json as _write_json,
)

from . import paths as _family_base

globals().update({name: value for name, value in vars(_family_base).items() if not name.startswith("__")})


def _install_smoke_root(target: Path) -> Path:
    return _release_root(target) / "install-smoke"


def _install_smoke_receipts_path(target: Path) -> Path:
    return _install_smoke_root(target) / "receipts.jsonl"


def _normalize_harnesses(value: str | list[Any] | tuple[Any, ...] | None) -> list[str]:
    if value is None or value == "" or value == "none":
        return []
    raw = value if isinstance(value, list | tuple) else str(value).split(",")
    harnesses = [str(item).strip() for item in raw if str(item).strip()]
    return sorted(dict.fromkeys(harnesses))


def _install_smoke_matrix_id(depth: str, harnesses: list[str]) -> str:
    label = "-".join(harnesses) if harnesses else "none"
    return f"{depth}-{label}"


def _install_smoke_matrix() -> list[dict[str, Any]]:
    return [
        {
            **item,
            "command_label": f"brigade init --depth {item['depth']} --harnesses {','.join(item['harnesses']) or 'none'}",
        }
        for item in INSTALL_SMOKE_MATRIX
    ]


def _read_install_smoke_receipts(target: Path) -> list[dict[str, Any]]:
    receipts = _read_jsonl(_install_smoke_receipts_path(target))
    receipts.sort(
        key=lambda item: str(item.get("completed_at") or item.get("created_at") or item.get("receipt_id") or ""),
        reverse=True,
    )
    return receipts


def _write_install_smoke_receipts(target: Path, receipts: list[dict[str, Any]]) -> None:
    path = _install_smoke_receipts_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for receipt in receipts:
            handle.write(json.dumps(receipt, sort_keys=True) + "\n")


def _install_smoke_record_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    depth = str(payload.get("depth") or "repo")
    harnesses = _normalize_harnesses(payload.get("harnesses"))
    status = str(payload.get("status") or "passed")
    completed_at = str(payload.get("completed_at") or payload.get("created_at") or _now().isoformat())
    matrix_id = str(payload.get("matrix_id") or _install_smoke_matrix_id(depth, harnesses))
    record = {
        "receipt_id": str(
            payload.get("receipt_id")
            or f"install-smoke-{work_cmd._stable_hash({'matrix_id': matrix_id, 'completed_at': completed_at, 'status': status})[:16]}"
        ),
        "matrix_id": matrix_id,
        "depth": depth,
        "harnesses": harnesses,
        "status": status,
        "command_label": _release_safe_text(
            str(
                payload.get("command_label")
                or f"brigade init --depth {depth} --harnesses {','.join(harnesses) or 'none'}"
            )
        ),
        "safe_summary": _release_safe_text(
            str(payload.get("safe_summary") or payload.get("summary") or f"install smoke {status}")
        ),
        "stdout_summary": _release_safe_text(str(payload.get("stdout_summary") or "")),
        "stderr_summary": _release_safe_text(str(payload.get("stderr_summary") or "")),
        "duration_seconds": payload.get("duration_seconds"),
        "created_at": completed_at,
        "completed_at": completed_at,
    }
    record["source_fingerprint"] = work_cmd._stable_hash(
        {key: value for key, value in record.items() if key not in {"receipt_id", "created_at", "completed_at"}}
    )
    return record


def install_smoke_plan(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    latest_by_matrix = {
        str(receipt.get("matrix_id") or ""): receipt for receipt in _read_install_smoke_receipts(target)
    }
    matrix = []
    for item in _install_smoke_matrix():
        latest = latest_by_matrix.get(str(item.get("matrix_id") or ""))
        matrix.append(
            {
                **item,
                "latest_receipt_id": latest.get("receipt_id") if isinstance(latest, dict) else None,
                "latest_status": latest.get("status") if isinstance(latest, dict) else None,
            }
        )
    payload = {
        "target": str(target),
        "receipt_path_label": ".brigade/release/install-smoke/receipts.jsonl",
        "matrix": matrix,
        "matrix_count": len(matrix),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("release install smoke plan")
    for item in matrix:
        print(f"- {item['matrix_id']}: {item['command_label']}")
    return 0


def install_smoke_record(
    *,
    target: Path,
    depth: str = "repo",
    harnesses: str | None = None,
    status: str = "passed",
    command_label: str | None = None,
    summary: str | None = None,
    receipt_json: Path | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if receipt_json is not None:
        payload = _read_json(receipt_json.expanduser())
        if payload is None:
            print(f"error: invalid install smoke receipt JSON: {receipt_json}", file=sys.stderr)
            return 2
    else:
        payload = {
            "depth": depth,
            "harnesses": harnesses,
            "status": status,
            "command_label": command_label,
            "safe_summary": summary,
        }
    record = _install_smoke_record_from_payload(payload)
    if record["depth"] not in {"repo", "workspace"}:
        print("error: --depth must be repo or workspace", file=sys.stderr)
        return 2
    invalid_harnesses = [item for item in record["harnesses"] if item not in KNOWN_HARNESSES]
    if invalid_harnesses:
        print(f"error: unknown harness in smoke receipt: {', '.join(invalid_harnesses)}", file=sys.stderr)
        return 2
    if record["status"] not in INSTALL_SMOKE_STATUSES:
        print(f"error: --status must be one of {', '.join(sorted(INSTALL_SMOKE_STATUSES))}", file=sys.stderr)
        return 2
    receipts = _read_install_smoke_receipts(target)
    receipts = [receipt for receipt in receipts if receipt.get("receipt_id") != record["receipt_id"]]
    receipts.append(record)
    _write_install_smoke_receipts(target, receipts)
    payload_out = {
        "target": str(target),
        "record": record,
        "receipt_path_label": ".brigade/release/install-smoke/receipts.jsonl",
    }
    if json_output:
        print(json.dumps(payload_out, indent=2, sort_keys=True))
        return 0
    print(f"release install smoke: {record['receipt_id']}")
    print(f"status: {record['status']}")
    return 0


def install_smoke_list(*, target: Path, limit: int = 20, json_output: bool = False) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    receipts = _read_install_smoke_receipts(target)
    payload = {"target": str(target), "receipts": receipts[:limit], "receipt_count": len(receipts)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("release install smoke receipts")
    for receipt in receipts[:limit]:
        print(f"- {receipt.get('receipt_id')} {receipt.get('matrix_id')} [{receipt.get('status')}]")
    return 0


def _resolve_install_smoke_receipt(target: Path, receipt_id: str) -> tuple[dict[str, Any] | None, str | None]:
    receipts = _read_install_smoke_receipts(target)
    if receipt_id == "latest":
        return (receipts[0], None) if receipts else (None, "install smoke receipt not found: latest")
    matches = [receipt for receipt in receipts if str(receipt.get("receipt_id") or "").startswith(receipt_id)]
    if not matches:
        return None, f"install smoke receipt not found: {receipt_id}"
    if len(matches) > 1:
        return None, f"install smoke receipt id is ambiguous: {receipt_id}"
    return matches[0], None


def install_smoke_show(*, target: Path, receipt_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    receipt, error = _resolve_install_smoke_receipt(target, receipt_id)
    if receipt is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    if json_output:
        print(json.dumps({"target": str(target), "receipt": receipt}, indent=2, sort_keys=True))
        return 0
    print(f"release install smoke: {receipt.get('receipt_id')}")
    print(f"matrix: {receipt.get('matrix_id')}")
    print(f"status: {receipt.get('status')}")
    return 0


def install_smoke_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    receipts = _read_install_smoke_receipts(target)
    latest_by_matrix: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        matrix_id = str(receipt.get("matrix_id") or "")
        if matrix_id and matrix_id not in latest_by_matrix:
            latest_by_matrix[matrix_id] = receipt
    issues: list[dict[str, Any]] = []
    now = _now()
    for item in _install_smoke_matrix():
        matrix_id = str(item["matrix_id"])
        latest = latest_by_matrix.get(matrix_id)
        if latest is None:
            issues.append(
                {
                    "status": WARN,
                    "name": "install_smoke_missing",
                    "matrix_id": matrix_id,
                    "detail": f"{matrix_id} has no install smoke receipt",
                    "suggested_next_command": f"brigade release smoke record --depth {item['depth']} --harnesses {','.join(item['harnesses']) or 'none'} --status passed",
                }
            )
            continue
        if latest.get("status") not in {"passed", "skipped"}:
            issues.append(
                {
                    "status": WARN,
                    "name": "install_smoke_failed",
                    "matrix_id": matrix_id,
                    "receipt_id": latest.get("receipt_id"),
                    "detail": f"{matrix_id} latest smoke is {latest.get('status')}",
                    "suggested_next_command": f"brigade release smoke show {latest.get('receipt_id')}",
                }
            )
        completed = work_cmd._parse_iso_datetime(latest.get("completed_at"))
        if completed is not None and (now - completed).total_seconds() / 3600 > INSTALL_SMOKE_STALE_HOURS:
            issues.append(
                {
                    "status": WARN,
                    "name": "install_smoke_stale",
                    "matrix_id": matrix_id,
                    "receipt_id": latest.get("receipt_id"),
                    "detail": f"{matrix_id} install smoke is stale",
                    "suggested_next_command": f"brigade release smoke record --depth {item['depth']} --harnesses {','.join(item['harnesses']) or 'none'} --status passed",
                }
            )
    return {
        "target_label": "release-install-smoke",
        "receipt_path_label": ".brigade/release/install-smoke/receipts.jsonl",
        "matrix": _install_smoke_matrix(),
        "matrix_count": len(INSTALL_SMOKE_MATRIX),
        "receipt_count": len(receipts),
        "latest_by_matrix": latest_by_matrix,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "issues": issues,
    }


def install_smoke_doctor(*, target: Path, json_output: bool = False) -> int:
    payload = install_smoke_health(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("release install smoke doctor")
    print(f"issues: {payload['issue_count']}")
    for issue in payload["issues"]:
        print(f"[{issue['status']}] {issue['name']}: {issue['detail']}")
    return 0
