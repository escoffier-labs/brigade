"""Scanner run, doctor, and health operations."""

from __future__ import annotations
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import dogfood_cmd
from ..install import apply_gitignore
from . import constants, helpers, ledger as ledger_mod, config as config_mod

from . import sweeps as sweeps_mod


def _scanner_read_receipt(path: Path) -> dict[str, Any] | None:
    receipt = path / "receipt.json" if path.is_dir() else path
    if not receipt.is_file():
        return None
    try:
        data = json.loads(receipt.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("path", str(receipt.parent))
    return data


def _scanner_receipts(target: Path) -> list[dict[str, Any]]:
    root = helpers._scanner_runs_root(target)
    if not root.is_dir():
        return []
    receipts = [_scanner_read_receipt(path) for path in root.iterdir() if path.is_dir()]
    valid = [item for item in receipts if isinstance(item, dict)]
    valid.sort(key=lambda item: str(item.get("started_at") or item.get("run_id") or ""), reverse=True)
    return valid


def _scanner_read_sweep(path: Path) -> dict[str, Any] | None:
    report = path / "sweep.json" if path.is_dir() else path
    if not report.is_file():
        return None
    try:
        data = json.loads(report.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("path", str(report.parent))
    return data


def _scanner_sweeps(target: Path) -> list[dict[str, Any]]:
    root = helpers._scanner_sweeps_root(target)
    if not root.is_dir():
        return []
    sweeps = [_scanner_read_sweep(path) for path in root.iterdir() if path.is_dir()]
    valid = [item for item in sweeps if isinstance(item, dict)]
    valid.sort(key=lambda item: str(item.get("started_at") or item.get("sweep_id") or ""), reverse=True)
    return valid


def _scanner_latest_sweep(target: Path) -> dict[str, Any] | None:
    sweeps = _scanner_sweeps(target)
    return sweeps[0] if sweeps else None


def _scanner_latest_success(target: Path, scanner_id: str) -> dict[str, Any] | None:
    for receipt in _scanner_receipts(target):
        if (
            receipt.get("scanner_id") == scanner_id
            and receipt.get("status") == "completed"
            and receipt.get("exit_code") == 0
        ):
            return receipt
    return None


def _scanner_is_due(target: Path, scanner: dict[str, Any], *, now: datetime | None = None) -> bool:
    now = now or helpers._now()
    scanner_id = str(scanner.get("id") or "")
    latest = _scanner_latest_success(target, scanner_id)
    if latest is None:
        return True
    started = helpers._parse_iso_datetime(latest.get("completed_at") or latest.get("started_at"))
    if started is None:
        return True
    cadence = str(scanner.get("cadence") or "")
    if cadence.startswith("hourly@"):
        return (now - started).total_seconds() >= 3600
    if cadence.startswith("daily@"):
        return now.date() > started.date()
    return False


def _scanner_due_items(target: Path, scanners: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [scanner for scanner in scanners if scanner.get("enabled", True) and _scanner_is_due(target, scanner)]


def _scanner_running_receipts(target: Path) -> list[dict[str, Any]]:
    return [receipt for receipt in _scanner_receipts(target) if receipt.get("status") == "running"]


def _scanner_output_snapshot(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if not path.exists():
        return {"path": str(path), "exists": False}
    stat = path.stat()
    return {
        "path": str(path),
        "exists": True,
        "is_dir": path.is_dir(),
        "size": stat.st_size if path.is_file() else None,
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


def _scanner_run_summary(text: str, limit: int = 1200) -> str:
    rendered = text.strip()
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3].rstrip() + "..."


def _scanner_run_receipt_path(run: dict[str, Any]) -> str | None:
    path = run.get("path")
    if isinstance(path, str) and path.strip():
        return str(Path(path) / "receipt.json")
    return None


def _scanner_import_fingerprint(record: dict[str, Any], *, scanner: dict[str, Any]) -> str:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    existing = metadata.get("source_fingerprint")
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    return helpers._stable_hash(
        {
            "scanner_id": scanner.get("id"),
            "scanner_source": scanner.get("source"),
            "source_item_key": ledger_mod._import_source_key(record),
            "text": record.get("text"),
            "kind": record.get("kind"),
            "type": record.get("type"),
            "priority": record.get("priority"),
            "template": record.get("template"),
            "acceptance": record.get("acceptance"),
        }
    )


def _scanner_import_provenance(
    *,
    target: Path,
    scanner: dict[str, Any],
    run: dict[str, Any],
    record: dict[str, Any],
) -> dict[str, Any]:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    output_after = run.get("output_after") if isinstance(run.get("output_after"), dict) else None
    provenance = {
        "scanner_id": scanner.get("id"),
        "scanner_source": scanner.get("source"),
        "scanner_run_id": run.get("run_id"),
        "scanner_receipt_path": _scanner_run_receipt_path(run),
        "scanner_output_path_snapshot": output_after,
        "source_fingerprint": _scanner_import_fingerprint(record, scanner=scanner),
    }
    import_path = config_mod._scanner_import_path(target, scanner)
    if import_path is not None:
        provenance["scanner_import_path"] = str(import_path)
    return {key: value for key, value in {**metadata, **provenance}.items() if value is not None}


def _scanner_enrich_import_records(
    *,
    target: Path,
    scanner: dict[str, Any],
    run: dict[str, Any],
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for record in records:
        item = dict(record)
        item["metadata"] = _scanner_import_provenance(target=target, scanner=scanner, run=run, record=record)
        enriched.append(item)
    return enriched


def _scanner_stamp_new_imports(
    *,
    target: Path,
    scanner: dict[str, Any],
    run: dict[str, Any],
    before_ids: set[str],
) -> list[str]:
    imports = ledger_mod._read_imports(target)
    changed = 0
    stamped_ids: list[str] = []
    for item in imports:
        import_id = item.get("id")
        if not isinstance(import_id, str) or import_id in before_ids:
            continue
        if item.get("source") != scanner.get("source"):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if metadata.get("scanner_run_id"):
            continue
        item["metadata"] = _scanner_import_provenance(target=target, scanner=scanner, run=run, record=item)
        item["updated_at"] = helpers._now().isoformat()
        changed += 1
        stamped_ids.append(import_id)
    if changed:
        ledger_mod._write_imports(target, imports)
    return stamped_ids


def _scanner_validate_import_output(
    target: Path,
    scanner: dict[str, Any],
) -> tuple[Path | None, list[dict[str, Any]], list[str]]:
    import_path = config_mod._scanner_import_path(target, scanner)
    if import_path is None:
        # No import_path means a self-importing scanner: its command appends to
        # the inbox directly (e.g. `brigade work import memory-refresh`,
        # `brigade handoff sync-issues`), so there is no JSONL file for the sweep
        # to ingest. Skip it silently rather than failing the whole sweep.
        return None, [], []
    if scanner.get("import_format", "jsonl") != "jsonl":
        return import_path, [], [f"{scanner.get('id')}: import_format must be jsonl"]
    if not import_path.is_file():
        return import_path, [], [f"{scanner.get('id')}: import file not found: {import_path}"]
    records, errors = ledger_mod._load_import_jsonl(import_path)
    return import_path, records, [f"{scanner.get('id')}: {error}" for error in errors]


def _scanner_run_one(
    target: Path,
    scanner: dict[str, Any],
    *,
    force: bool = False,
) -> dict[str, Any]:
    scanner_id = str(scanner.get("id") or "scanner")
    command = str(scanner.get("command") or "")
    argv, blocker = config_mod._scanner_argv(command)
    output_path = config_mod._scanner_output_path(target, scanner)
    import_path = config_mod._scanner_import_path(target, scanner)
    cwd = config_mod._scanner_cwd(target, scanner)
    started = helpers._now()
    run_id = f"{started.strftime('%Y%m%d-%H%M%S')}-{helpers._slug(scanner_id)}-{uuid4().hex[:6]}"
    run_dir = helpers._scanner_runs_root(target) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    receipt_path = run_dir / "receipt.json"
    receipt: dict[str, Any] = {
        "run_id": run_id,
        "scanner_id": scanner_id,
        "source": scanner.get("source"),
        "status": "running",
        "path": str(run_dir),
        "target": str(target),
        "cwd": str(cwd),
        "command": command,
        "argv": argv or [],
        "started_at": started.isoformat(),
        "timeout": scanner.get("timeout"),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "output_path": str(output_path) if output_path is not None else None,
        "output_before": _scanner_output_snapshot(output_path),
        "import_path": str(import_path) if import_path is not None else None,
        "import_format": scanner.get("import_format", "jsonl") if import_path is not None else None,
        "forced": force,
    }
    helpers._write_json(receipt_path, receipt)
    if blocker is not None:
        completed = helpers._now()
        receipt.update(
            {
                "status": "failed",
                "completed_at": completed.isoformat(),
                "duration_seconds": (completed - started).total_seconds(),
                "exit_code": None,
                "timed_out": False,
                "error": blocker,
                "stdout_summary": "",
                "stderr_summary": blocker,
                "output_after": _scanner_output_snapshot(output_path),
            }
        )
        stdout_path.write_text("")
        stderr_path.write_text(blocker + "\n")
        helpers._write_json(receipt_path, receipt)
        return receipt
    if not cwd.is_dir():
        completed = helpers._now()
        error = f"scanner cwd does not exist: {cwd}"
        receipt.update(
            {
                "status": "failed",
                "completed_at": completed.isoformat(),
                "duration_seconds": (completed - started).total_seconds(),
                "exit_code": None,
                "timed_out": False,
                "error": error,
                "stdout_summary": "",
                "stderr_summary": error,
                "output_after": _scanner_output_snapshot(output_path),
            }
        )
        stdout_path.write_text("")
        stderr_path.write_text(error + "\n")
        helpers._write_json(receipt_path, receipt)
        return receipt
    try:
        completed_process = subprocess.run(
            argv,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=float(scanner.get("timeout") or 300),
            shell=False,
        )
        stdout = completed_process.stdout or ""
        stderr = completed_process.stderr or ""
        stdout_path.write_text(stdout)
        stderr_path.write_text(stderr)
        completed = helpers._now()
        receipt.update(
            {
                "status": "completed" if completed_process.returncode == 0 else "failed",
                "completed_at": completed.isoformat(),
                "duration_seconds": (completed - started).total_seconds(),
                "exit_code": completed_process.returncode,
                "timed_out": False,
                "stdout_summary": _scanner_run_summary(stdout),
                "stderr_summary": _scanner_run_summary(stderr),
                "output_after": _scanner_output_snapshot(output_path),
            }
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        stdout_path.write_text(stdout)
        stderr_path.write_text(stderr)
        completed = helpers._now()
        receipt.update(
            {
                "status": "failed",
                "completed_at": completed.isoformat(),
                "duration_seconds": (completed - started).total_seconds(),
                "exit_code": None,
                "timed_out": True,
                "error": f"scanner timed out after {scanner.get('timeout')} seconds",
                "stdout_summary": _scanner_run_summary(stdout),
                "stderr_summary": _scanner_run_summary(stderr),
                "output_after": _scanner_output_snapshot(output_path),
            }
        )
    helpers._write_json(receipt_path, receipt)
    return receipt


def _scanner_plan_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    scanners, errors = config_mod._load_scanner_config(target)
    enabled = [scanner for scanner in scanners if scanner.get("enabled", True)]
    planned: list[dict[str, Any]] = []
    for scanner in enabled:
        start = config_mod._scanner_start_minute(str(scanner.get("cadence", "")))
        if start is None:
            continue
        duration = config_mod._scanner_duration_minutes(scanner)
        planned.append(
            {
                "id": scanner.get("id"),
                "source": scanner.get("source"),
                "command": scanner.get("command"),
                "cadence": scanner.get("cadence"),
                "start_minute": start,
                "start": config_mod._format_clock_minutes(start),
                "duration_minutes": duration,
                "end": config_mod._format_clock_minutes(start + duration),
                "conflict_window": scanner.get("conflict_window"),
                "output_path": scanner.get("output_path"),
                "import_path": scanner.get("import_path"),
                "import_format": scanner.get("import_format", "jsonl") if scanner.get("import_path") else None,
            }
        )
    planned.sort(key=lambda item: int(item.get("start_minute", 0)))

    conflicts: list[dict[str, Any]] = []
    for index, left in enumerate(planned):
        left_start = int(left["start_minute"])
        left_end = left_start + int(left["duration_minutes"])
        left_window = config_mod._scanner_window_minutes(str(left.get("conflict_window") or ""))
        for right in planned[index + 1 :]:
            right_start = int(right["start_minute"])
            right_end = right_start + int(right["duration_minutes"])
            right_window = config_mod._scanner_window_minutes(str(right.get("conflict_window") or ""))
            if left_start < right_end and right_start < left_end:
                conflicts.append({"type": "run_overlap", "scanners": [left["id"], right["id"]]})
            if left_window and right_window and left_window[0] < right_window[1] and right_window[0] < left_window[1]:
                conflicts.append({"type": "window_overlap", "scanners": [left["id"], right["id"]]})
            if abs(right_start - left_start) < 15:
                conflicts.append({"type": "clustered_runs", "scanners": [left["id"], right["id"]]})

    suggestions: list[dict[str, Any]] = []
    next_start: int | None = None
    for item in planned:
        current = int(item["start_minute"])
        suggested = current if next_start is None else max(current, next_start)
        suggestions.append(
            {
                "id": item["id"],
                "current": item["cadence"],
                "suggested_start": config_mod._format_clock_minutes(suggested),
                "suggested_cadence": f"daily@{config_mod._format_clock_minutes(suggested)}"
                if str(item.get("cadence", "")).startswith("daily@")
                else f"hourly@{suggested % 60:02d}",
            }
        )
        next_start = suggested + 15

    return {
        "target": str(target),
        "config_path": str(helpers._scanner_config_path(target)),
        "valid": not errors,
        "errors": errors,
        "scanners": scanners,
        "planned": planned,
        "conflicts": conflicts,
        "suggestions": suggestions,
    }


def _required_scanner_ids(target: Path) -> tuple[str, ...]:
    """Required local-producer scanner ids for this target.

    chat-memory-sweep only matters when the repo actually has an enabled chat
    surface. A code repo with every surface disabled (or no chat-surfaces.toml
    at all) has nothing to sweep, so it should not be nagged to enable it.
    """
    from .. import chat_cmd

    surfaces = chat_cmd.health(target).get("surfaces")
    surfaces = surfaces if isinstance(surfaces, list) else []
    chat_active = any(isinstance(surface, dict) and surface.get("enabled") for surface in surfaces)
    return tuple(
        scanner_id for scanner_id in constants.SCANNER_REQUIRED_IDS if scanner_id != "chat-memory-sweep" or chat_active
    )


def _scanner_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    plan = _scanner_plan_payload(target)
    scanners = plan["scanners"] if isinstance(plan.get("scanners"), list) else []
    checks: list[dict[str, Any]] = []
    if not helpers._scanner_config_path(target).is_file():
        checks.append(
            {
                "status": constants.WARN,
                "name": "scanner_config",
                "detail": f"missing, run `brigade work scanners init --target {target}`",
            }
        )
    elif plan.get("valid"):
        checks.append({"status": constants.OK, "name": "scanner_config", "detail": plan["config_path"]})
    else:
        checks.append({"status": constants.FAIL, "name": "scanner_config", "detail": "; ".join(plan.get("errors", []))})

    by_id = {scanner.get("id"): scanner for scanner in scanners if isinstance(scanner, dict)}
    required_ids = _required_scanner_ids(target)
    missing_required = [scanner_id for scanner_id in required_ids if scanner_id not in by_id]
    disabled_required = [
        scanner_id
        for scanner_id in required_ids
        if isinstance(by_id.get(scanner_id), dict) and not by_id[scanner_id].get("enabled", True)
    ]
    if missing_required or disabled_required:
        detail_parts = []
        if missing_required:
            detail_parts.append(f"missing={','.join(missing_required)}")
        if disabled_required:
            detail_parts.append(f"disabled={','.join(disabled_required)}")
        checks.append({"status": constants.WARN, "name": "scanner_required", "detail": "; ".join(detail_parts)})
    else:
        checks.append(
            {"status": constants.OK, "name": "scanner_required", "detail": "required local producers are enabled"}
        )

    bad_commands = []
    for scanner in scanners:
        if not scanner.get("enabled", True):
            continue
        _, blocker = config_mod._scanner_argv(str(scanner.get("command") or ""))
        if blocker is not None:
            bad_commands.append(str(scanner.get("id")))
    if bad_commands:
        checks.append({"status": constants.WARN, "name": "scanner_commands", "detail": ", ".join(bad_commands)})
    else:
        checks.append(
            {"status": constants.OK, "name": "scanner_commands", "detail": "enabled scanner commands are resolvable"}
        )

    stale_outputs: list[str] = []
    missing_outputs: list[str] = []
    now = helpers._now() if scanners else None
    for scanner in scanners:
        if not scanner.get("enabled", True):
            continue
        output = scanner.get("output_path")
        if not isinstance(output, str) or not output.strip():
            continue
        path = Path(output).expanduser()
        path = path if path.is_absolute() else target / path
        if not path.exists():
            missing_outputs.append(str(scanner.get("id")))
            continue
        if now is None:
            continue
        age_hours = (now.timestamp() - path.stat().st_mtime) / 3600
        if age_hours > constants.SCANNER_OUTPUT_STALE_HOURS:
            stale_outputs.append(f"{scanner.get('id')}={age_hours:.1f}h")
    if missing_outputs or stale_outputs:
        parts = []
        if missing_outputs:
            parts.append(f"missing={','.join(missing_outputs)}")
        if stale_outputs:
            parts.append(f"stale={','.join(stale_outputs)}")
        checks.append({"status": constants.WARN, "name": "scanner_outputs", "detail": "; ".join(parts)})
    else:
        checks.append(
            {"status": constants.OK, "name": "scanner_outputs", "detail": "enabled scanner outputs exist and are fresh"}
        )

    conflicts = plan.get("conflicts") if isinstance(plan.get("conflicts"), list) else []
    if conflicts:
        rendered = ", ".join(
            f"{item.get('type')}:{'/'.join(str(v) for v in item.get('scanners', []))}" for item in conflicts[:5]
        )
        checks.append({"status": constants.WARN, "name": "scanner_schedule", "detail": rendered})
    elif plan.get("valid"):
        checks.append({"status": constants.OK, "name": "scanner_schedule", "detail": "no scanner schedule conflicts"})

    receipts = _scanner_receipts(target)
    malformed_receipts = []
    runs_root = helpers._scanner_runs_root(target)
    if runs_root.is_dir():
        for path in runs_root.iterdir():
            if path.is_dir() and _scanner_read_receipt(path) is None:
                malformed_receipts.append(path.name)
    if malformed_receipts:
        checks.append(
            {"status": constants.FAIL, "name": "scanner_run_receipts", "detail": ", ".join(malformed_receipts[:5])}
        )

    running = [receipt for receipt in receipts if receipt.get("status") == "running"]
    if running:
        checks.append(
            {
                "status": constants.WARN,
                "name": "scanner_runs_running",
                "detail": ", ".join(str(item.get("run_id")) for item in running[:5]),
            }
        )

    recent_failed = [receipt for receipt in receipts if receipt.get("status") == "failed" or receipt.get("timed_out")][
        :5
    ]
    if recent_failed:
        rendered = ", ".join(f"{item.get('scanner_id')}:{item.get('run_id')}" for item in recent_failed)
        checks.append({"status": constants.WARN, "name": "scanner_runs_failed", "detail": rendered})
    elif receipts:
        checks.append({"status": constants.OK, "name": "scanner_runs_failed", "detail": "none"})

    missing_logs = []
    for receipt in receipts[:20]:
        for key in ("stdout_path", "stderr_path"):
            value = receipt.get(key)
            if isinstance(value, str) and value and not Path(value).is_file():
                missing_logs.append(f"{receipt.get('run_id')}:{key}")
    if missing_logs:
        checks.append({"status": constants.WARN, "name": "scanner_run_logs", "detail": ", ".join(missing_logs[:5])})
    elif receipts:
        checks.append({"status": constants.OK, "name": "scanner_run_logs", "detail": "receipt logs exist"})

    stale_successes: list[str] = []
    if scanners:
        now = helpers._now()
        for scanner in scanners:
            if not scanner.get("enabled", True):
                continue
            latest_success = _scanner_latest_success(target, str(scanner.get("id") or ""))
            if latest_success is None:
                continue
            completed = helpers._parse_iso_datetime(
                latest_success.get("completed_at") or latest_success.get("started_at")
            )
            if completed is None:
                stale_successes.append(str(scanner.get("id")))
                continue
            age_hours = (now - completed).total_seconds() / 3600
            if age_hours > constants.SCANNER_RUN_STALE_HOURS:
                stale_successes.append(f"{scanner.get('id')}={age_hours:.1f}h")
    if stale_successes:
        checks.append(
            {"status": constants.WARN, "name": "scanner_runs_stale", "detail": ", ".join(stale_successes[:5])}
        )
    elif receipts and plan.get("valid"):
        checks.append({"status": constants.OK, "name": "scanner_runs_stale", "detail": "none"})

    due = _scanner_due_items(target, scanners)
    if due:
        checks.append(
            {
                "status": constants.WARN,
                "name": "scanner_runs_due",
                "detail": ", ".join(str(item.get("id")) for item in due[:5]),
            }
        )
    elif plan.get("valid"):
        checks.append({"status": constants.OK, "name": "scanner_runs_due", "detail": "none"})

    next_run = plan.get("planned", [None])[0] if plan.get("planned") else None
    latest_run = receipts[0] if receipts else None
    return {
        "target": str(target),
        "config_path": str(helpers._scanner_config_path(target)),
        "checks": checks,
        "plan": plan,
        "next_run": next_run,
        "latest_run": latest_run,
        "due": due,
    }


def _scanner_sweep_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    checks: list[dict[str, Any]] = []
    latest = _scanner_latest_sweep(target)
    due = _scanner_health(target).get("due")
    due_count = len(due) if isinstance(due, list) else 0
    review: dict[str, Any] | None = None
    if latest is None:
        checks.append({"status": constants.WARN, "name": "scanner_sweeps", "detail": "none, run `brigade work sweep`"})
    else:
        status = str(latest.get("status") or "unknown")
        if status == "failed":
            checks.append({"status": constants.WARN, "name": "scanner_sweep_failed", "detail": latest.get("sweep_id")})
        else:
            checks.append(
                {
                    "status": constants.OK,
                    "name": "scanner_sweep_latest",
                    "detail": f"{latest.get('sweep_id')} [{status}]",
                }
            )
        completed = helpers._parse_iso_datetime(latest.get("completed_at") or latest.get("started_at"))
        if completed is not None:
            age_hours = (helpers._now() - completed).total_seconds() / 3600
            if age_hours > constants.SCANNER_SWEEP_STALE_HOURS:
                checks.append(
                    {
                        "status": constants.WARN,
                        "name": "scanner_sweep_stale",
                        "detail": f"{latest.get('sweep_id')}={age_hours:.1f}h",
                    }
                )
        review, _ = sweeps_mod._sweep_review_payload(target, str(latest.get("sweep_id") or "latest"))
        if isinstance(review, dict):
            checks.extend(review["issues"])
    return {
        "target": str(target),
        "sweeps_root": str(helpers._scanner_sweeps_root(target)),
        "latest": latest,
        "checks": checks,
        "due_count": due_count,
        "suggested_command": "brigade work sweep" if due_count else None,
        "review": {
            "top_pending_import": review.get("top_pending_import") if isinstance(review, dict) else None,
            "issue_count": len(review.get("issues", [])) if isinstance(review, dict) else 0,
            "issues": review.get("issues", []) if isinstance(review, dict) else [],
        },
    }


def _scanner_health_issue_records(target: Path) -> list[dict[str, Any]]:
    health = _scanner_health(target)
    records: list[dict[str, Any]] = []
    for check in health["checks"]:
        if check.get("status") == constants.OK:
            continue
        name = str(check.get("name"))
        detail = str(check.get("detail"))
        records.append(
            {
                "text": f"Repair scanner health issue {name}: {detail}",
                "kind": "task",
                "source": "scanner-health",
                "type": "workflow",
                "priority": "normal",
                "template": "bugfix",
                "acceptance": [f"`brigade work scanners doctor` no longer reports {name}."],
                "metadata": {
                    "scanner_health_check": name,
                    "scanner_health_status": check.get("status"),
                    "scanner_health_detail": detail,
                    "source_item_key": f"scanner-health:{name}",
                    "source_fingerprint": helpers._stable_hash({"name": name, "detail": detail}),
                },
            }
        )
    return records


def _scanner_source_map(target: Path) -> dict[str, dict[str, Any]]:
    scanners, errors = config_mod._load_scanner_config(target)
    if errors:
        return {}
    by_source: dict[str, dict[str, Any]] = {}
    for scanner in scanners:
        for key in ("source", "id"):
            value = scanner.get(key)
            if isinstance(value, str) and value.strip():
                by_source[value.strip()] = scanner
    return by_source


def scanners_init(*, target: Path, force: bool = False, update_gitignore: bool = True) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = helpers._scanner_config_path(target)
    if path.exists() and not force:
        print(f"error: scanner config already exists: {path}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config_mod._format_scanner_toml())
    print(f"scanner_config: {path}")
    print(f"scanners: {len(constants.SCANNER_DEFAULTS)}")
    if update_gitignore:
        result = apply_gitignore(target, helpers._work_selection(target, dogfood_cmd.default_handoff_inbox(target)))
        print(f"gitignore: {result}")
    else:
        print("gitignore: skipped")
    print("next_command: brigade work scanners plan")
    return 0


def scanners_list(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    scanners, errors = config_mod._load_scanner_config(target)
    payload = {
        "target": str(target),
        "config_path": str(helpers._scanner_config_path(target)),
        "valid": not errors,
        "errors": errors,
        "scanners": scanners,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if not errors else 1
    print(f"work scanners: {target}")
    print(f"config_path: {helpers._scanner_config_path(target)}")
    if errors:
        print(f"errors: {len(errors)}")
        for error in errors:
            print(f"- {error}")
        return 1
    if not scanners:
        print("scanners: none")
        return 0
    for scanner in scanners:
        status = "enabled" if scanner.get("enabled", True) else "disabled"
        print(f"- {scanner.get('id')} [{status}] {scanner.get('cadence')} source={scanner.get('source')}")
        print(f"  command: {scanner.get('command')}")
        print(f"  output: {scanner.get('output_path')}")
        if scanner.get("import_path"):
            print(f"  import: {scanner.get('import_path')} ({scanner.get('import_format', 'jsonl')})")
    return 0


def scanners_show(*, target: Path, scanner_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    scanners, errors = config_mod._load_scanner_config(target)
    scanner = None
    for item in scanners:
        if item.get("id") == scanner_id:
            scanner = item
            break
    payload = {
        "target": str(target),
        "config_path": str(helpers._scanner_config_path(target)),
        "valid": not errors,
        "errors": errors,
        "scanner": scanner,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if scanner is not None and not errors else 1
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    if scanner is None:
        print(f"error: scanner not found: {scanner_id}", file=sys.stderr)
        return 1
    print(f"scanner: {scanner.get('id')}")
    print(f"enabled: {scanner.get('enabled')}")
    print(f"source: {scanner.get('source')}")
    print(f"cadence: {scanner.get('cadence')}")
    print(f"timeout: {scanner.get('timeout')}")
    print(f"output_path: {scanner.get('output_path')}")
    if scanner.get("import_path"):
        print(f"import_path: {scanner.get('import_path')}")
        print(f"import_format: {scanner.get('import_format', 'jsonl')}")
    print(f"conflict_window: {scanner.get('conflict_window')}")
    print(f"command: {scanner.get('command')}")
    return 0


def scanners_plan(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _scanner_plan_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"work scanners plan: {target}")
    print(f"config_path: {payload['config_path']}")
    if payload["errors"]:
        print(f"errors: {len(payload['errors'])}")
        for error in payload["errors"]:
            print(f"- {error}")
        return 1
    planned = payload.get("planned") if isinstance(payload.get("planned"), list) else []
    if not planned:
        print("planned: none")
    else:
        print("planned:")
        for item in planned:
            print(
                f"- {item.get('id')} {item.get('start')}-{item.get('end')} "
                f"{item.get('cadence')} output={item.get('output_path')}"
            )
    conflicts = payload.get("conflicts") if isinstance(payload.get("conflicts"), list) else []
    if conflicts:
        print("conflicts:")
        for item in conflicts:
            print(f"- {item.get('type')}: {', '.join(str(v) for v in item.get('scanners', []))}")
    else:
        print("conflicts: none")
    suggestions = payload.get("suggestions") if isinstance(payload.get("suggestions"), list) else []
    if suggestions:
        print("suggested_schedule:")
        for item in suggestions:
            print(f"- {item.get('id')}: {item.get('suggested_cadence')}")
    return 0


def scanners_doctor(*, target: Path, json_output: bool = False, import_issues: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    health = _scanner_health(target)
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    skipped_dismissed: list[dict[str, Any]] = []
    if import_issues:
        records = _scanner_health_issue_records(target)
        imported, skipped, skipped_dismissed = ledger_mod._append_import_records(target, records)
        health["import_issues"] = {
            "created": len(imported),
            "skipped": len(skipped),
            "dismissed": len(skipped_dismissed),
            "imports": imported,
        }
    if json_output:
        print(json.dumps(health, indent=2, sort_keys=True))
        return 0 if not any(check.get("status") == constants.FAIL for check in health["checks"]) else 1
    print(f"work scanners doctor: {target}")
    print(f"config_path: {health['config_path']}")
    for check in health["checks"]:
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))
    next_run = health.get("next_run")
    if isinstance(next_run, dict):
        print(f"next_scanner: {next_run.get('id')} {next_run.get('start')} {next_run.get('cadence')}")
    if import_issues:
        print(f"imported_issues: {len(imported)}")
        print(f"skipped_issues: {len(skipped)}")
        print(f"dismissed_issues: {len(skipped_dismissed)}")
    return 0 if not any(check.get("status") == constants.FAIL for check in health["checks"]) else 1


def _select_scanners_for_run(
    target: Path,
    *,
    scanner_id: str | None,
    all_matching: bool,
    due: bool,
    include_disabled: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    scanners, errors = config_mod._load_scanner_config(target)
    if errors:
        return [], [], errors
    if scanner_id:
        selected = [item for item in scanners if item.get("id") == scanner_id]
        if not selected:
            return [], [], [f"scanner not found: {scanner_id}"]
    elif all_matching or due:
        selected = list(scanners)
    else:
        return [], [], ["scanner id, --all, or --due is required"]
    runnable: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for scanner in selected:
        if not scanner.get("enabled", True) and not include_disabled:
            if scanner_id:
                return [], [], [f"scanner disabled: {scanner_id}"]
            skipped.append({"scanner": scanner, "reason": "disabled"})
            continue
        if due and not _scanner_is_due(target, scanner):
            skipped.append({"scanner": scanner, "reason": "not_due"})
            continue
        runnable.append(scanner)
    return runnable, skipped, []


def _scanners_run_payload(
    *,
    target: Path,
    scanner_id: str | None = None,
    all_matching: bool = False,
    due: bool = False,
    include_disabled: bool = False,
    force: bool = False,
    ingest_output: bool = False,
    require_selector: bool = True,
) -> tuple[dict[str, Any], int]:
    target = target.expanduser().resolve()
    if not target.is_dir():
        return {
            "target": str(target),
            "errors": [f"--target is not a directory: {target}"],
            "runs": [],
            "skipped": [],
        }, 2
    selector_count = sum(1 for item in (scanner_id, all_matching, due) if bool(item))
    if require_selector and selector_count != 1:
        error = "pass exactly one of scanner id, --all, or --due"
        return {"target": str(target), "errors": [error], "runs": [], "skipped": []}, 2
    if not require_selector and selector_count > 1:
        error = "pass only one of scanner id, --all, or --due"
        return {"target": str(target), "errors": [error], "runs": [], "skipped": []}, 2
    if not helpers._scanner_config_path(target).is_file():
        error = f"scanner config missing: {helpers._scanner_config_path(target)}"
        return {"target": str(target), "errors": [error], "runs": [], "skipped": []}, 2
    running = _scanner_running_receipts(target)
    if running and not force:
        error = f"scanner run already in progress: {running[0].get('run_id')}"
        return {"target": str(target), "errors": [error], "runs": [], "skipped": []}, 2
    selected, skipped, errors = _select_scanners_for_run(
        target,
        scanner_id=scanner_id,
        all_matching=all_matching,
        due=due,
        include_disabled=include_disabled,
    )
    if errors:
        return {"target": str(target), "errors": errors, "runs": [], "skipped": skipped}, 2
    before_counts = ledger_mod._import_counts(ledger_mod._pending_imports(target))
    runs: list[dict[str, Any]] = []
    contexts: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for scanner in selected:
        before_ids = {
            str(item.get("id"))
            for item in ledger_mod._read_imports(target)
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        run = _scanner_run_one(target, scanner, force=force)
        stamped_ids = _scanner_stamp_new_imports(target=target, scanner=scanner, run=run, before_ids=before_ids)
        run["provenance_imports_stamped"] = len(stamped_ids)
        if stamped_ids:
            run["stamped_import_ids"] = stamped_ids
        if run.get("path"):
            helpers._write_json(Path(str(run["path"])) / "receipt.json", run)
        runs.append(run)
        contexts.append((scanner, run))
    ingest_errors: list[str] = []
    ingest_payloads: list[tuple[dict[str, Any], dict[str, Any], Path, list[dict[str, Any]]]] = []
    if ingest_output:
        for scanner, run in contexts:
            if run.get("status") != "completed":
                continue
            path, records, errors = _scanner_validate_import_output(target, scanner)
            if errors:
                ingest_errors.extend(errors)
                continue
            if path is not None:
                ingest_payloads.append(
                    (
                        scanner,
                        run,
                        path,
                        _scanner_enrich_import_records(target=target, scanner=scanner, run=run, records=records),
                    )
                )
        if ingest_errors:
            after_counts = ledger_mod._import_counts(ledger_mod._pending_imports(target))
            payload = {
                "target": str(target),
                "runs_root": str(helpers._scanner_runs_root(target)),
                "selected": len(selected),
                "completed": len([run for run in runs if run.get("status") == "completed"]),
                "failed": len([run for run in runs if run.get("status") != "completed"]),
                "skipped": [
                    {"scanner_id": item["scanner"].get("id"), "reason": item["reason"]}
                    for item in skipped
                    if isinstance(item.get("scanner"), dict)
                ],
                "imports_before": before_counts,
                "imports_after": after_counts,
                "ingest_output": True,
                "ingest_errors": ingest_errors,
                "runs": runs,
            }
            return payload, 2
        for _scanner, run, path, records in ingest_payloads:
            imported, skipped_records, skipped_dismissed = ledger_mod._append_import_records(target, records)
            run["ingest_output"] = {
                "path": str(path),
                "created": len(imported),
                "skipped": len(skipped_records),
                "dismissed": len(skipped_dismissed),
                "records": len(records),
                "created_import_ids": [str(item.get("id")) for item in imported if isinstance(item.get("id"), str)],
                "skipped_source_fingerprints": [
                    fingerprint for record in skipped_records if (fingerprint := ledger_mod._import_fingerprint(record))
                ],
                "dismissed_source_fingerprints": [
                    fingerprint
                    for record in skipped_dismissed
                    if (fingerprint := ledger_mod._import_fingerprint(record))
                ],
            }
            if run.get("path"):
                helpers._write_json(Path(str(run["path"])) / "receipt.json", run)
    after_counts = ledger_mod._import_counts(ledger_mod._pending_imports(target))
    payload = {
        "target": str(target),
        "runs_root": str(helpers._scanner_runs_root(target)),
        "selected": len(selected),
        "completed": len([run for run in runs if run.get("status") == "completed"]),
        "failed": len([run for run in runs if run.get("status") != "completed"]),
        "skipped": [
            {"scanner_id": item["scanner"].get("id"), "reason": item["reason"]}
            for item in skipped
            if isinstance(item.get("scanner"), dict)
        ],
        "imports_before": before_counts,
        "imports_after": after_counts,
        "ingest_output": ingest_output,
        "ingest_errors": ingest_errors,
        "runs": runs,
    }
    return payload, 0 if payload["failed"] == 0 else 1


def scanners_run(
    *,
    target: Path,
    scanner_id: str | None = None,
    all_matching: bool = False,
    due: bool = False,
    include_disabled: bool = False,
    force: bool = False,
    ingest_output: bool = False,
    json_output: bool = False,
) -> int:
    payload, rc = _scanners_run_payload(
        target=target,
        scanner_id=scanner_id,
        all_matching=all_matching,
        due=due,
        include_disabled=include_disabled,
        force=force,
        ingest_output=ingest_output,
    )
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return rc
    print(f"work scanners run: {payload.get('target')}")
    print(f"runs_root: {payload['runs_root']}")
    print(f"selected: {payload['selected']}")
    print(f"completed: {payload['completed']}")
    print(f"failed: {payload['failed']}")
    for item in payload["skipped"]:
        print(f"skipped: {item['scanner_id']} {item['reason']}")
    runs = payload.get("runs") if isinstance(payload.get("runs"), list) else []
    for run in runs:
        print(
            f"- {run.get('run_id')} {run.get('scanner_id')} "
            f"[{run.get('status')}] exit={run.get('exit_code')} timed_out={run.get('timed_out')}"
        )
        if run.get("error"):
            print(f"  error: {run.get('error')}")
        if run.get("ingest_output"):
            ingest = run["ingest_output"]
            print(
                "  ingest_output: "
                f"created={ingest.get('created')} skipped={ingest.get('skipped')} dismissed={ingest.get('dismissed')}"
            )
        if run.get("provenance_imports_stamped"):
            print(f"  provenance_imports_stamped: {run.get('provenance_imports_stamped')}")
        print(f"  logs: {run.get('stdout_path')} {run.get('stderr_path')}")
    before_counts = payload.get("imports_before") if isinstance(payload.get("imports_before"), dict) else {}
    after_counts = payload.get("imports_after") if isinstance(payload.get("imports_after"), dict) else {}
    print(f"pending_imports_before: {before_counts.get('total', 0)}")
    print(f"pending_imports_after: {after_counts.get('total', 0)}")
    return rc


def scanners_runs(*, target: Path, json_output: bool = False, limit: int = 20) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    receipts = _scanner_receipts(target)[:limit]
    payload = {"target": str(target), "runs_root": str(helpers._scanner_runs_root(target)), "runs": receipts}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"work scanner runs: {target}")
    print(f"runs_root: {payload['runs_root']}")
    if not receipts:
        print("runs: none")
        return 0
    for receipt in receipts:
        print(
            f"- {receipt.get('run_id')} {receipt.get('scanner_id')} "
            f"[{receipt.get('status')}] exit={receipt.get('exit_code')} {receipt.get('started_at')}"
        )
    return 0


def scanners_run_show(*, target: Path, run_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    matches = [receipt for receipt in _scanner_receipts(target) if str(receipt.get("run_id") or "").startswith(run_id)]
    if not matches:
        print(f"error: scanner run not found: {run_id}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"error: scanner run id is ambiguous: {run_id}", file=sys.stderr)
        return 2
    receipt = matches[0]
    if json_output:
        print(json.dumps({"target": str(target), "run": receipt}, indent=2, sort_keys=True))
        return 0
    print(f"scanner_run: {receipt.get('run_id')}")
    print(f"scanner: {receipt.get('scanner_id')}")
    print(f"source: {receipt.get('source')}")
    print(f"status: {receipt.get('status')}")
    print(f"started_at: {receipt.get('started_at')}")
    if receipt.get("completed_at"):
        print(f"completed_at: {receipt.get('completed_at')}")
    print(f"duration_seconds: {receipt.get('duration_seconds')}")
    print(f"exit_code: {receipt.get('exit_code')}")
    print(f"timed_out: {receipt.get('timed_out')}")
    print(f"stdout: {receipt.get('stdout_path')}")
    print(f"stderr: {receipt.get('stderr_path')}")
    if receipt.get("stdout_summary"):
        print(f"stdout_summary: {helpers._short(str(receipt.get('stdout_summary')))}")
    if receipt.get("stderr_summary"):
        print(f"stderr_summary: {helpers._short(str(receipt.get('stderr_summary')))}")
    return 0
