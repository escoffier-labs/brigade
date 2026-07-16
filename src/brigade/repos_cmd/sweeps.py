# ruff: noqa: F401
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import actionqueue, config as brigade_config, reportstore, toml_compat as tomllib, work_cmd
from ..budgets import HANDOFF_BACKLOG_STALE_DAYS
from ..install import apply_gitignore
from ..localio import (
    read_json_dict as _read_json,
    read_jsonl_dicts as _read_jsonl,
    utc_now as _now,
    write_json as _write_json,
)
from ..render import emit
from ..selection import Selection, WRITER_INBOXES
from . import constants, fleet


def _import_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for issue in payload.get("issues", []):
        if not isinstance(issue, dict):
            continue
        repo_id = str(issue.get("repo_id") or "fleet")
        name = str(issue.get("name") or "repo_fleet_issue")
        detail = str(issue.get("detail") or name)
        fingerprint = work_cmd._stable_hash({"repo_id": repo_id, "name": name, "detail": detail})
        records.append(
            {
                "text": f"Resolve repository fleet issue: {detail}",
                "kind": "task",
                "source": "repo-fleet",
                "type": "docs",
                "priority": "normal",
                "template": "docs",
                "acceptance": [
                    "The repo fleet issue is resolved or explicitly deferred.",
                    "No private repository contents or paths are copied into public artifacts.",
                ],
                "metadata": {
                    "repo_id": repo_id,
                    "issue_type": name,
                    "safe_summary": detail,
                    "source_item_key": f"{repo_id}:{name}",
                    "source_fingerprint": fingerprint,
                },
            }
        )
    return records


def import_issues(*, target: Path, json_output: bool = False, dry_run: bool = False) -> int:
    payload = fleet.scan_payload(target)
    records = _import_records(payload)
    imported, skipped, dismissed = work_cmd._append_import_records(
        target.expanduser().resolve(), records, dry_run=dry_run
    )
    output = {
        "target": payload["target"],
        "created": len(imported),
        "skipped": len(skipped),
        "dismissed": len(dismissed),
        "dry_run": dry_run,
        "issue_count": payload["issue_count"],
    }
    text_lines = [
        f"repo_fleet_imports: {payload['target']}",
        f"created: {len(imported)}",
        f"skipped: {len(skipped)}",
        f"dismissed: {len(dismissed)}",
    ]
    if dry_run:
        text_lines.append("dry_run: true")
    return emit(output, json_output, text_lines, 0)


def _sweeps_root(target: Path) -> Path:
    return target / ".brigade" / "repos" / "sweeps"


def _sweep_json_path(path: Path) -> Path:
    return path / "sweep.json" if path.is_dir() else path


def _read_sweep(path: Path) -> dict[str, Any] | None:
    payload = _read_json(_sweep_json_path(path))
    if payload is not None:
        payload.pop("path", None)
        payload.setdefault("path_label", _sweep_json_path(path).parent.name)
    return payload


def _list_or_empty(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _list_sweep_dirs_newest_first(target: Path) -> list[Path]:
    root = _sweeps_root(target)
    if not root.is_dir():
        return []
    dirs = [child for child in root.iterdir() if child.is_dir()]
    dirs.sort(key=lambda path: path.name, reverse=True)
    return dirs


def _sweeps(target: Path) -> list[dict[str, Any]]:
    sweeps: list[dict[str, Any]] = []
    for child in _list_sweep_dirs_newest_first(target):
        payload = _read_sweep(child)
        if payload is not None:
            sweeps.append(payload)
    sweeps.sort(key=lambda item: str(item.get("started_at") or item.get("sweep_id") or ""), reverse=True)
    return sweeps


@dataclass
class HealthSweepSnapshot:
    latest: dict[str, Any] | None = None
    receipt_index: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)


def _required_health_receipt_keys(entries: list[constants.RepoEntry]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for entry in entries:
        if not entry.enabled:
            continue
        for command in entry.health_commands:
            keys.add((entry.repo_id, command.label))
    return keys


def _index_sweep_health_receipts(
    sweep: dict[str, Any],
    receipt_index: dict[tuple[str, str], dict[str, Any]],
) -> None:
    sweep_id = sweep.get("sweep_id")
    sweep_status = sweep.get("status")
    sweep_path_label = sweep.get("path_label") or sweep_id
    for repo in _list_or_empty(sweep.get("repos")):
        if not isinstance(repo, dict):
            continue
        repo_id = str(repo.get("repo_id") or "")
        if not repo_id:
            continue
        for command in _list_or_empty(repo.get("commands")):
            if not isinstance(command, dict):
                continue
            label = str(command.get("label") or "")
            if not label:
                continue
            key = (repo_id, label)
            if key in receipt_index:
                continue
            receipt_index[key] = {
                "sweep_id": sweep_id,
                "sweep_status": sweep_status,
                "sweep_path_label": sweep_path_label,
                "repo_id": repo_id,
                "label": label,
                "status": command.get("status"),
                "exit_code": command.get("exit_code"),
                "timed_out": command.get("timed_out"),
                "started_at": command.get("started_at"),
                "completed_at": command.get("completed_at"),
                "stdout_log_label": command.get("stdout_log_label"),
                "stderr_log_label": command.get("stderr_log_label"),
            }


def _health_sweep_snapshot(target: Path, entries: list[constants.RepoEntry]) -> HealthSweepSnapshot:
    required = _required_health_receipt_keys(entries)
    latest: dict[str, Any] | None = None
    receipt_index: dict[tuple[str, str], dict[str, Any]] = {}
    for sweep_dir in _list_sweep_dirs_newest_first(target):
        sweep = _read_sweep(sweep_dir)
        if sweep is None:
            continue
        if latest is None:
            latest = sweep
        _index_sweep_health_receipts(sweep, receipt_index)
        if not required or required.issubset(receipt_index):
            break
    return HealthSweepSnapshot(latest=latest, receipt_index=receipt_index)


def latest_sweep(target: Path) -> dict[str, Any] | None:
    sweeps = _sweeps(target)
    return sweeps[0] if sweeps else None


def _resolve_sweep(target: Path, sweep_id: str) -> tuple[dict[str, Any] | None, str | None]:
    if sweep_id == "latest":
        latest = latest_sweep(target)
        return (latest, None) if latest else (None, "repo fleet sweep not found: latest")
    matches = [item for item in _sweeps(target) if str(item.get("sweep_id") or "").startswith(sweep_id)]
    if not matches:
        return None, f"repo fleet sweep not found: {sweep_id}"
    if len(matches) > 1:
        return None, f"repo fleet sweep id is ambiguous: {sweep_id}"
    return matches[0], None


def _sweep_commands() -> list[constants.SweepCommand]:
    return [
        constants.SweepCommand(
            "center-report-build", [sys.executable, "-m", "brigade", "center", "report", "build", "--json"]
        ),
        constants.SweepCommand(
            "release-plan", [sys.executable, "-m", "brigade", "release", "plan", "--base-ref", "", "--json"]
        ),
        constants.SweepCommand("work-brief", [sys.executable, "-m", "brigade", "work", "brief", "--json"]),
    ]


def _commands_for_entry(entry: constants.RepoEntry) -> list[constants.SweepCommand]:
    return [*_sweep_commands(), *entry.health_commands]


def _health_command_registry_payload(
    target: Path,
    *,
    entries: list[constants.RepoEntry] | None = None,
    errors: list[str] | None = None,
    config_loaded: bool | None = None,
    health_snapshot: HealthSweepSnapshot | None = None,
) -> dict[str, Any]:
    target = target.expanduser().resolve()
    if entries is None:
        entries, errors, config_loaded = fleet._load_config(target)
    else:
        errors = list(errors or [])
        config_loaded = bool(config_loaded)
    snapshot = health_snapshot or _health_sweep_snapshot(target, entries)
    checks: list[dict[str, Any]] = []
    repos: list[dict[str, Any]] = []
    if errors:
        checks.extend(
            {
                "status": constants.WARN,
                "name": "repo_health_command_config",
                "detail": fleet._safe_text(error, target, "repo-fleet", "repo fleet"),
            }
            for error in errors
        )
    elif config_loaded:
        checks.append(
            {"status": constants.OK, "name": "repo_health_command_config", "detail": constants.CONFIG_REL_PATH}
        )
    receipt_index = snapshot.receipt_index
    for entry in entries:
        if not entry.enabled:
            continue
        command_rows: list[dict[str, Any]] = []
        seen_labels: set[str] = set()
        duplicate_labels: set[str] = set()
        for command in entry.health_commands:
            if command.label in seen_labels:
                duplicate_labels.add(command.label)
            seen_labels.add(command.label)
            receipt = receipt_index.get((entry.repo_id, command.label))
            stale = False
            age_hours: float | None = None
            if receipt is None:
                checks.append(
                    {
                        "status": constants.WARN,
                        "name": "repo_health_command_receipt_missing",
                        "detail": f"{entry.repo_id}:{command.label} has no sweep receipt",
                        "repo_id": entry.repo_id,
                        "command_label": command.label,
                        "suggested_next_command": f"brigade repos sweep run --repo {entry.repo_id}",
                    }
                )
            else:
                completed_at = fleet._parse_time(receipt.get("completed_at"))
                if completed_at is not None:
                    age_hours = round((_now() - completed_at).total_seconds() / 3600, 2)
                    stale = age_hours > constants.HEALTH_COMMAND_RECEIPT_STALE_HOURS
                if stale:
                    checks.append(
                        {
                            "status": constants.WARN,
                            "name": "repo_health_command_receipt_stale",
                            "detail": f"{entry.repo_id}:{command.label} receipt is stale",
                            "repo_id": entry.repo_id,
                            "command_label": command.label,
                            "age_hours": age_hours,
                            "suggested_next_command": f"brigade repos sweep run --repo {entry.repo_id} --force",
                        }
                    )
                if receipt.get("status") != "completed":
                    checks.append(
                        {
                            "status": constants.WARN,
                            "name": "repo_health_command_failed",
                            "detail": f"{entry.repo_id}:{command.label} latest receipt status is {receipt.get('status') or 'unknown'}",
                            "repo_id": entry.repo_id,
                            "command_label": command.label,
                            "suggested_next_command": f"brigade repos sweep show {receipt.get('sweep_id')}",
                        }
                    )
            command_rows.append(
                {
                    "label": command.label,
                    "timeout": command.timeout,
                    "argv_label": command.label,
                    "latest_receipt": receipt,
                    "receipt_status": receipt.get("status") if isinstance(receipt, dict) else "missing",
                    "receipt_age_hours": age_hours,
                    "stale": stale,
                    "source_fingerprint": fleet._fingerprint_payload(
                        {"repo_id": entry.repo_id, "label": command.label, "timeout": command.timeout}
                    ),
                }
            )
        for label in sorted(duplicate_labels):
            checks.append(
                {
                    "status": constants.WARN,
                    "name": "repo_health_command_duplicate_label",
                    "detail": f"{entry.repo_id}:{label} is configured more than once",
                    "repo_id": entry.repo_id,
                    "command_label": label,
                }
            )
        repos.append(
            {
                "repo_id": entry.repo_id,
                "repo_label": entry.label,
                "enabled": entry.enabled,
                "exists": entry.path.is_dir(),
                "health_command_count": len(command_rows),
                "health_commands": command_rows,
            }
        )
    issues = [check for check in checks if check.get("status") != constants.OK]
    return {
        "schema_version": 1,
        "target_label": "repo-fleet",
        "config_path_label": constants.CONFIG_REL_PATH,
        "config_loaded": config_loaded,
        "receipt_stale_hours": constants.HEALTH_COMMAND_RECEIPT_STALE_HOURS,
        "repos": repos,
        "repo_count": len(repos),
        "health_command_count": sum(int(repo.get("health_command_count") or 0) for repo in repos),
        "checks": checks,
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "suggested_next_commands": ["brigade repos sweep run --all --force"] if issues else [],
        "privacy": {
            "argv_redacted": True,
            "safe_labels_only": True,
        },
    }


def health_commands(*, target: Path, json_output: bool = False) -> int:
    payload = _health_command_registry_payload(target)
    rc = 0 if payload["config_loaded"] and not payload["issues"] else 1
    command_lines = [
        f"- {repo['repo_id']}:{command['label']} timeout={command['timeout']} receipt={command['receipt_status']}"
        for repo in payload["repos"]
        for command in repo.get("health_commands", [])
    ]
    text_lines = [
        "repo health commands: repo-fleet",
        f"commands: {payload['health_command_count']}",
        f"issues: {payload['issue_count']}",
        *command_lines,
        *[f"[{issue['status']}] {issue['name']}: {issue['detail']}" for issue in payload["issues"]],
    ]
    return emit(payload, json_output, text_lines, rc)


def _latest_sweep_for_repo(target: Path, repo_id: str) -> dict[str, Any] | None:
    for sweep in _sweeps(target):
        for result in _list_or_empty(sweep.get("repos")):
            if isinstance(result, dict) and result.get("repo_id") == repo_id and result.get("status") == "completed":
                return sweep
    return None


def _select_sweep_entries(
    target: Path,
    *,
    repo_ids: list[str] | None = None,
    include_disabled: bool = False,
    stale_only: bool = False,
    force: bool = False,
) -> tuple[list[constants.RepoEntry], list[str], bool]:
    entries, errors, config_loaded = fleet._load_config(target)
    wanted = set(repo_ids or [])
    selected: list[constants.RepoEntry] = []
    for entry in entries:
        if wanted and entry.repo_id not in wanted:
            continue
        if not entry.enabled and not include_disabled:
            continue
        if stale_only and not force and _latest_sweep_for_repo(target, entry.repo_id) is not None:
            continue
        selected.append(entry)
    missing = sorted(wanted - {entry.repo_id for entry in entries})
    errors.extend(f"repo not found: {repo_id}" for repo_id in missing)
    return selected, errors, config_loaded


def _sweep_plan_payload(
    target: Path,
    *,
    repo_ids: list[str] | None = None,
    include_disabled: bool = False,
    stale_only: bool = False,
    force: bool = False,
    all_repos: bool = False,
) -> dict[str, Any]:
    target = target.expanduser().resolve()
    selected, errors, config_loaded = _select_sweep_entries(
        target,
        repo_ids=repo_ids,
        include_disabled=include_disabled,
        stale_only=stale_only,
        force=force or all_repos,
    )
    command_labels = sorted({command.label for entry in selected for command in _commands_for_entry(entry)})
    safe_errors = [fleet._safe_text(error, target, "repo-fleet", "repo fleet") for error in errors]
    return {
        "target_label": "repo-fleet",
        "config_path_label": constants.CONFIG_REL_PATH,
        "config_loaded": config_loaded,
        "errors": safe_errors,
        "mode": "all" if all_repos else ("stale-only" if stale_only else "selected"),
        "repos": [
            {
                "repo_id": entry.repo_id,
                "repo_label": entry.label,
                "enabled": entry.enabled,
                "exists": entry.path.is_dir(),
                "stale": _latest_sweep_for_repo(target, entry.repo_id) is None,
                "commands": [
                    {"label": command.label, "timeout": command.timeout} for command in _commands_for_entry(entry)
                ],
            }
            for entry in selected
        ],
        "repo_count": len(selected),
        "command_labels": command_labels,
    }


def sweep_plan(
    *,
    target: Path,
    repo_ids: list[str] | None = None,
    all_repos: bool = False,
    stale_only: bool = False,
    include_disabled: bool = False,
    force: bool = False,
    json_output: bool = False,
) -> int:
    payload = _sweep_plan_payload(
        target,
        repo_ids=repo_ids,
        include_disabled=include_disabled,
        stale_only=stale_only,
        force=force,
        all_repos=all_repos,
    )
    rc = 0 if payload["config_loaded"] and not payload["errors"] else 1
    repo_lines = []
    for repo in payload["repos"]:
        labels = ",".join(command["label"] for command in repo.get("commands", []))
        repo_lines.append(f"- {repo['repo_id']} {repo['repo_label']} commands={labels}")
    text_lines = [
        f"repo fleet sweep plan: {payload['target_label']}",
        f"repos: {payload['repo_count']}",
        *repo_lines,
        *[f"[warn] {error}" for error in payload["errors"]],
    ]
    return emit(payload, json_output, text_lines, rc)


def _summarize_output(text: str, repo_path: Path, repo_id: str, label: str, limit: int = 240) -> str:
    safe = fleet._safe_text(text.replace("\n", " "), repo_path, repo_id, label).strip()
    return work_cmd._short(safe, limit)


def _run_sweep_command(entry: constants.RepoEntry, command: constants.SweepCommand, sweep_dir: Path) -> dict[str, Any]:
    started = _now()
    command_dir = sweep_dir / "logs" / entry.repo_id / command.label
    command_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    source_path = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(source_path) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        result = subprocess.run(
            command.argv,
            cwd=entry.path,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=command.timeout,
            env=env,
        )
        exit_code = result.returncode
        timed_out = False
        stdout = result.stdout
        stderr = result.stderr
    except subprocess.TimeoutExpired as exc:
        exit_code = None
        timed_out = True
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
    except OSError as exc:
        exit_code = None
        timed_out = False
        stdout = ""
        stderr = f"failed to run sweep command: {exc}"
    completed = _now()
    (command_dir / "stdout.log").write_text(stdout)
    (command_dir / "stderr.log").write_text(stderr)
    return {
        "label": command.label,
        "argv_label": command.label,
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "duration_seconds": round((completed - started).total_seconds(), 3),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "status": "timeout" if timed_out else ("completed" if exit_code == 0 else "failed"),
        "stdout_summary": _summarize_output(stdout, entry.path, entry.repo_id, entry.label),
        "stderr_summary": _summarize_output(stderr, entry.path, entry.repo_id, entry.label),
        "stdout_log_label": f"{entry.repo_id}/{command.label}/stdout.log",
        "stderr_log_label": f"{entry.repo_id}/{command.label}/stderr.log",
    }


def sweep_run(
    *,
    target: Path,
    repo_ids: list[str] | None = None,
    all_repos: bool = False,
    stale_only: bool = False,
    include_disabled: bool = False,
    force: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    plan = _sweep_plan_payload(
        target,
        repo_ids=repo_ids,
        include_disabled=include_disabled,
        stale_only=stale_only,
        force=force,
        all_repos=all_repos,
    )
    selected, errors, config_loaded = _select_sweep_entries(
        target,
        repo_ids=repo_ids,
        include_disabled=include_disabled,
        stale_only=stale_only,
        force=force or all_repos,
    )
    started = _now()
    sweep_id = f"{started.strftime('%Y%m%d-%H%M%S')}-repo-fleet-sweep-{uuid4().hex[:6]}"
    sweep_dir = _sweeps_root(target) / sweep_id
    repo_results: list[dict[str, Any]] = []
    for entry in selected:
        repo_started = _now()
        if not entry.path.is_dir():
            repo_results.append(
                {
                    "repo_id": entry.repo_id,
                    "repo_label": entry.label,
                    "status": "failed",
                    "started_at": repo_started.isoformat(),
                    "completed_at": _now().isoformat(),
                    "commands": [],
                    "warning_count": 1,
                    "blocker_count": 0,
                    "warnings": [{"name": "repo_missing", "detail": f"{entry.repo_id} is not reachable"}],
                    "receipt_labels": [],
                }
            )
            continue
        command_results = [_run_sweep_command(entry, command, sweep_dir) for command in _commands_for_entry(entry)]
        repo_completed = _now()
        failed = [command for command in command_results if command.get("status") != "completed"]
        state = fleet._repo_brigade_state(entry)
        receipt_labels = []
        latest_report_ref = (
            state.get("latest_operator_report") if isinstance(state.get("latest_operator_report"), dict) else None
        )
        if latest_report_ref:
            receipt_labels.append(
                {
                    "repo_id": entry.repo_id,
                    "repo_label": entry.label,
                    "kind": "operator-report",
                    "id": latest_report_ref.get("id"),
                }
            )
        latest_release = (
            state.get("latest_release_readiness") if isinstance(state.get("latest_release_readiness"), dict) else None
        )
        if latest_release:
            receipt_labels.append(
                {
                    "repo_id": entry.repo_id,
                    "repo_label": entry.label,
                    "kind": "release-readiness",
                    "id": latest_release.get("id"),
                }
            )
        repo_results.append(
            {
                "repo_id": entry.repo_id,
                "repo_label": entry.label,
                "status": "completed" if not failed else "failed",
                "started_at": repo_started.isoformat(),
                "completed_at": repo_completed.isoformat(),
                "duration_seconds": round((repo_completed - repo_started).total_seconds(), 3),
                "commands": command_results,
                "warning_count": len(_list_or_empty(state.get("warnings"))),
                "blocker_count": len(_list_or_empty(state.get("blockers"))),
                "warnings": _list_or_empty(state.get("warnings")),
                "receipt_labels": receipt_labels,
            }
        )
    completed = _now()
    failed_count = sum(1 for repo in repo_results if repo.get("status") != "completed")
    payload = {
        "sweep_id": sweep_id,
        "target_label": "repo-fleet",
        "path_label": sweep_id,
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "duration_seconds": round((completed - started).total_seconds(), 3),
        "status": "completed" if failed_count == 0 and not errors and config_loaded else "failed",
        "config_loaded": config_loaded,
        "errors": plan.get("errors", errors),
        "plan": plan,
        "repos": repo_results,
        "repo_count": len(repo_results),
        "failed_count": failed_count,
        "suggested_next_commands": [
            "brigade repos report build",
            "brigade repos report closeout latest",
            "brigade repos actions build latest",
        ],
    }
    _write_json(sweep_dir / "sweep.json", payload)
    rc = 0 if payload["status"] == "completed" else 1
    text_lines = [
        f"repo fleet sweep: {sweep_id}",
        f"status: {payload['status']}",
        f"repos: {payload['repo_count']}",
        f"failed: {failed_count}",
        f"path_label: {sweep_id}",
    ]
    return emit(payload, json_output, text_lines, rc)


def sweep_runs(*, target: Path, limit: int = 20, json_output: bool = False) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    sweeps = _sweeps(target)[:limit]
    payload = {
        "target_label": "repo-fleet",
        "sweeps_root_label": ".brigade/repos/sweeps",
        "sweeps": sweeps,
        "sweep_count": len(sweeps),
    }
    text_lines = [
        "repo fleet sweeps: repo-fleet",
        *[
            f"- {sweep.get('sweep_id')} [{sweep.get('status')}] repos={sweep.get('repo_count')} {sweep.get('started_at')}"
            for sweep in sweeps
        ],
    ]
    return emit(payload, json_output, text_lines, 0)


def sweep_show(*, target: Path, sweep_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    sweep, error = _resolve_sweep(target, sweep_id)
    if sweep is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    payload = {"target_label": "repo-fleet", "sweep": sweep}
    text_lines = [
        f"repo fleet sweep: {sweep.get('sweep_id')}",
        f"status: {sweep.get('status')}",
        f"repos: {sweep.get('repo_count')}",
        f"path_label: {sweep.get('path_label')}",
    ]
    return emit(payload, json_output, text_lines, 0)


def sweep_closeout(
    *,
    target: Path,
    sweep_id: str = "latest",
    status: str = "reviewed",
    reason: str | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if status not in {"reviewed", "deferred", "superseded", "archived"}:
        print("error: --status must be one of reviewed, deferred, superseded, archived", file=sys.stderr)
        return 2
    sweep, error = _resolve_sweep(target, sweep_id)
    if sweep is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    sweep_path = _sweeps_root(target) / str(sweep.get("sweep_id") or "")
    if not sweep_path.is_dir():
        print(f"error: repo fleet sweep path is missing: {sweep.get('sweep_id')}", file=sys.stderr)
        return 2
    payload = {
        "target_label": "repo-fleet",
        "sweep_id": sweep.get("sweep_id"),
        "status": status,
        "reason": reason or f"repo fleet sweep marked {status}",
        "reviewed_at": _now().isoformat(),
        "source_fingerprint": fleet._fingerprint_payload(
            {"sweep_id": sweep.get("sweep_id"), "repos": sweep.get("repos")}
        ),
    }
    closeout_path = sweep_path / "CLOSEOUT.json"
    payload["path_label"] = f"{sweep.get('sweep_id')}:CLOSEOUT.json"
    _write_json(closeout_path, payload)
    sweep["closeout"] = payload
    _write_json(sweep_path / "sweep.json", sweep)
    text_lines = [f"repo fleet sweep closeout: {sweep.get('sweep_id')}", f"status: {status}"]
    return emit(payload, json_output, text_lines, 0)


def sweep_health(
    target: Path,
    *,
    health_snapshot: HealthSweepSnapshot | None = None,
) -> dict[str, Any]:
    target = target.expanduser().resolve()
    latest = health_snapshot.latest if health_snapshot is not None else latest_sweep(target)
    checks: list[dict[str, Any]] = []
    if latest is None:
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_fleet_sweep_missing",
                "detail": "no repo fleet sweep has been run",
                "suggested_next_command": "brigade repos sweep run",
            }
        )
        return {"latest": None, "checks": checks, "issue_count": len(checks), "top_issue": checks[0]}
    closeout = latest.get("closeout") if isinstance(latest.get("closeout"), dict) else None
    if not closeout or closeout.get("status") not in {"reviewed", "deferred", "superseded", "archived"}:
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_fleet_sweep_unclosed",
                "detail": f"{latest.get('sweep_id')} has not been closed out",
                "suggested_next_command": f"brigade repos sweep closeout {latest.get('sweep_id')}",
            }
        )
    if latest.get("status") != "completed":
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_fleet_sweep_failed",
                "detail": f"{latest.get('sweep_id')} did not complete",
                "suggested_next_command": f"brigade repos sweep show {latest.get('sweep_id')}",
            }
        )
    return {"latest": latest, "checks": checks, "issue_count": len(checks), "top_issue": checks[0] if checks else None}


def _reports_root(target: Path) -> Path:
    return target / ".brigade" / "repos" / "reports"


def _reports_archive_root(target: Path) -> Path:
    return target / ".brigade" / "repos" / "reports-archive"


def _report_json_path(path: Path) -> Path:
    return reportstore.bundle_json_path(path, "FLEET_EVIDENCE.json")


def _read_report(path: Path) -> dict[str, Any] | None:
    return reportstore.read_bundle(path, "FLEET_EVIDENCE.json")


def _reports(target: Path, *, include_archived: bool = False) -> list[dict[str, Any]]:
    roots = [_reports_root(target)]
    if include_archived:
        roots.append(_reports_archive_root(target))
    return reportstore.list_bundles(roots, _read_report, id_field="report_id")


def latest_report(target: Path) -> dict[str, Any] | None:
    reports = _reports(target)
    return reports[0] if reports else None


def _resolve_report(target: Path, report_id: str) -> tuple[dict[str, Any] | None, str | None]:
    reports = [] if report_id == "latest" else _reports(target, include_archived=True)
    return reportstore.resolve_bundle(
        reports, report_id, id_field="report_id", label="fleet report", latest=lambda: latest_report(target)
    )


def _report_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    entries, errors, config_loaded = fleet._load_config(target)
    repo_states = [fleet._repo_brigade_state(entry) for entry in entries if entry.enabled]
    sweep = sweep_health(target)
    health_registry = _health_command_registry_payload(target)
    blockers = [item for repo in repo_states for item in repo.get("blockers", []) if isinstance(item, dict)]
    warnings = [item for repo in repo_states for item in repo.get("warnings", []) if isinstance(item, dict)]
    health_command_warnings = [item for item in health_registry.get("issues", []) if isinstance(item, dict)]
    receipt_refs = [ref for repo in repo_states for ref in repo.get("receipt_references", []) if isinstance(ref, dict)]
    payload = {
        "schema_version": 1,
        "target": str(target),
        "config_path": str(constants.config_path(target)),
        "config_loaded": config_loaded,
        "config_errors": errors,
        "generated_at": _now().isoformat(),
        "repo_count": len(repo_states),
        "repos": repo_states,
        "blocker_count": len(blockers),
        "warning_count": len(warnings) + len(errors) + len(health_command_warnings),
        "blockers": blockers,
        "warnings": warnings
        + [{"name": "repo_fleet_config", "detail": error} for error in errors]
        + health_command_warnings,
        "receipt_references": receipt_refs,
        "latest_sweep": _safe_sweep_ref(sweep.get("latest") if isinstance(sweep.get("latest"), dict) else None),
        "sweep_health": {"issue_count": sweep.get("issue_count"), "top_issue": sweep.get("top_issue")},
        "health_commands": {
            "health_command_count": health_registry.get("health_command_count"),
            "issue_count": health_registry.get("issue_count"),
            "top_issue": health_registry.get("top_issue"),
            "repos": health_registry.get("repos"),
        },
        "suggested_next_commands": [
            repo.get("suggested_command") for repo in repo_states if repo.get("suggested_command")
        ],
    }
    payload["report_fingerprint"] = fleet._fingerprint_payload(
        {
            "repos": repo_states,
            "warnings": payload["warnings"],
            "blockers": blockers,
            "receipts": receipt_refs,
        }
    )
    return payload


def _safe_sweep_ref(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    return {
        "sweep_id": payload.get("sweep_id"),
        "status": payload.get("status"),
        "started_at": payload.get("started_at"),
        "completed_at": payload.get("completed_at"),
        "repo_count": payload.get("repo_count"),
        "failed_count": payload.get("failed_count"),
    }


def _report_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Repo Fleet Report",
        "",
        f"- Report: `{payload.get('report_id', 'planned')}`",
        f"- Generated: {payload.get('generated_at')}",
        f"- Repos: {payload.get('repo_count')}",
        f"- Warnings: {payload.get('warning_count')}",
        f"- Blockers: {payload.get('blocker_count')}",
        "",
        "## Repos",
        "",
    ]
    repos = _list_or_empty(payload.get("repos"))
    for repo in repos:
        lines.append(
            f"- `{repo.get('repo_id')}` {repo.get('repo_label')} warnings={len(repo.get('warnings') if isinstance(repo.get('warnings'), list) else [])} blockers={len(repo.get('blockers') if isinstance(repo.get('blockers'), list) else [])}"
        )
        top = repo.get("action_queue") if isinstance(repo.get("action_queue"), dict) else {}
        top_action = top.get("top_action") if isinstance(top.get("top_action"), dict) else None
        if top_action:
            lines.append(f"  - top action: `{top_action.get('action_id')}` {top_action.get('safe_summary')}")
        if repo.get("suggested_command"):
            lines.append(f"  - next: `{repo.get('suggested_command')}`")
    if not repos:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Boundaries",
            "",
            "- local report only",
            "- no cloning",
            "- no remote mutation",
            "- no automatic action execution",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_report_bundle(path: Path, payload: dict[str, Any]) -> None:
    reportstore.write_bundle(
        path, payload, evidence_name="FLEET_EVIDENCE.json", documents={"FLEET_REPORT.md": _report_markdown(payload)}
    )


def report_plan(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload = _report_payload(target)
    payload.update(
        {
            "report_id": "planned",
            "reports_root": str(_reports_root(target)),
            "bundle_files": ["FLEET_REPORT.md", "FLEET_EVIDENCE.json"],
        }
    )
    rc = 0 if payload["config_loaded"] else 1
    text_lines = [
        f"repo fleet report plan: {target}",
        f"repos: {payload['repo_count']}",
        f"warnings: {payload['warning_count']}",
        f"reports_root: {payload['reports_root']}",
    ]
    return emit(payload, json_output, text_lines, rc)


def report_build(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload = _report_payload(target)
    created = _now()
    report_id = f"{created.strftime('%Y%m%d-%H%M%S')}-repo-fleet-report-{uuid4().hex[:6]}"
    report_dir = _reports_root(target) / report_id
    payload.update(
        {
            "report_id": report_id,
            "created_at": created.isoformat(),
            "path": str(report_dir),
            "bundle_files": ["FLEET_REPORT.md", "FLEET_EVIDENCE.json"],
        }
    )
    _write_report_bundle(report_dir, payload)
    rc = 0 if payload["config_loaded"] else 1
    text_lines = [
        f"repo fleet report: {report_id}",
        f"repos: {payload['repo_count']}",
        f"warnings: {payload['warning_count']}",
        f"path: {report_dir}",
    ]
    return emit(payload, json_output, text_lines, rc)


def report_list(*, target: Path, limit: int = 20, json_output: bool = False) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    reports = _reports(target)[:limit]
    payload = {
        "target": str(target),
        "reports_root": str(_reports_root(target)),
        "reports": reports,
        "report_count": len(reports),
    }
    text_lines = [
        f"repo fleet reports: {target}",
        *[
            f"- {report.get('report_id')} repos={report.get('repo_count')} warnings={report.get('warning_count')} {report.get('created_at')}"
            for report in reports
        ],
    ]
    return emit(payload, json_output, text_lines, 0)


def report_show(*, target: Path, report_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    report, error = _resolve_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    payload = {"target": str(target), "report": report}
    text_lines = [
        f"repo fleet report: {report.get('report_id')}",
        f"repos: {report.get('repo_count')}",
        f"warnings: {report.get('warning_count')}",
        f"path: {report.get('path')}",
    ]
    return emit(payload, json_output, text_lines, 0)


def report_archive(*, target: Path, report_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    report, error = _resolve_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    source = Path(str(report.get("path") or _reports_root(target) / str(report.get("report_id"))))
    if not source.is_dir():
        print(f"error: fleet report path is missing: {source}", file=sys.stderr)
        return 2
    destination, moved = reportstore.move_bundle(source, _reports_archive_root(target))
    if not moved:
        print(f"error: archived fleet report already exists: {destination}", file=sys.stderr)
        return 2
    payload = {
        "target": str(target),
        "report_id": report.get("report_id"),
        "status": "archived",
        "archive_path": str(destination),
    }
    text_lines = [f"archived repo fleet report: {report.get('report_id')}", f"path: {destination}"]
    return emit(payload, json_output, text_lines, 0)


def report_closeout(
    *,
    target: Path,
    report_id: str = "latest",
    status: str = "reviewed",
    reason: str | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if status not in reportstore.CLOSEOUT_STATUSES:
        print("error: --status must be one of reviewed, deferred, superseded, archived", file=sys.stderr)
        return 2
    report, error = _resolve_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    report_path = Path(str(report.get("path") or ""))
    if not report_path.is_dir():
        print(f"error: fleet report path is missing: {report.get('path')}", file=sys.stderr)
        return 2
    payload = {
        "target": str(target),
        "report_id": report.get("report_id"),
        "status": status,
        "reason": reason or f"repo fleet report marked {status}",
        "reviewed_at": _now().isoformat(),
        "report_fingerprint": report.get("report_fingerprint"),
    }
    reportstore.write_closeout(report_path, payload)
    report["closeout"] = payload
    _write_json(report_path / "FLEET_EVIDENCE.json", report)
    text_lines = [f"repo fleet report closeout: {report.get('report_id')}", f"status: {status}"]
    return emit(payload, json_output, text_lines, 0)


__all__ = tuple(name for name in globals() if not name.startswith("__"))
