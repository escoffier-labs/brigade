"""Local release readiness receipts."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import context_cmd, handoff_cmd, learn_cmd, memory_cmd, phases_cmd, projects_cmd, repos_cmd, research_cmd, roadmap_cmd, security_cmd, tools_cmd, work_cmd
from .selection import KNOWN_HARNESSES

OK = "ok"
WARN = "warn"
FAIL = "fail"
RELEASE_CANDIDATE_STALE_HOURS = 168
PHASE_REPORT_STALE_HOURS = 24
RELEASE_PRIVATE_VALUE_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_]*(?:api[_-]?key|secret|token|password|passwd|pwd)[A-Za-z0-9_]*)\b\s*[:=]\s*['\"]?([A-Za-z0-9_./+=:-]{8,})"
)
RELEASE_PRIVATE_PATH_RE = re.compile(r"(?<!`)/(?:home|Users|private|mnt|Volumes)/[^\s`)]+")
SCHEMA_MANIFEST_VERSION = 1
INSTALL_SMOKE_STALE_HOURS = 168
INSTALL_SMOKE_STATUSES = {"passed", "failed", "skipped", "blocked"}
INSTALL_SMOKE_MATRIX = (
    {"matrix_id": "repo-none", "depth": "repo", "harnesses": []},
    {"matrix_id": "repo-claude", "depth": "repo", "harnesses": ["claude"]},
    {"matrix_id": "repo-codex", "depth": "repo", "harnesses": ["codex"]},
    {"matrix_id": "workspace-claude", "depth": "workspace", "harnesses": ["claude"]},
    {"matrix_id": "workspace-claude-openclaw", "depth": "workspace", "harnesses": ["claude", "openclaw"]},
    {"matrix_id": "workspace-codex-openclaw", "depth": "workspace", "harnesses": ["codex", "openclaw"]},
    {"matrix_id": "workspace-claude-codex-openclaw", "depth": "workspace", "harnesses": ["claude", "codex", "openclaw"]},
)
CI_DEPRECATION_PATTERNS = (
    re.compile(r"(?i)\bnode(?:\.js)?\s*(12|16)\b.*\b(deprecat|unsupported|retired)"),
    re.compile(r"(?i)\b(deprecat|unsupported|retired)\b.*\bnode(?:\.js)?\s*(12|16)\b"),
    re.compile(r"(?i)\bgithub actions?\b.*\b(deprecat|unsupported|retired)\b"),
)
CI_DEPRECATED_ACTION_MAJORS = {
    "actions/cache": {"1", "2", "3"},
    "actions/checkout": {"1", "2", "3"},
    "actions/download-artifact": {"1", "2", "3"},
    "actions/github-script": {"1", "2", "3", "4", "5", "6"},
    "actions/setup-go": {"1", "2", "3", "4"},
    "actions/setup-java": {"1", "2", "3"},
    "actions/setup-node": {"1", "2", "3"},
    "actions/setup-python": {"1", "2", "3", "4"},
    "actions/upload-artifact": {"1", "2", "3"},
}
CI_ACTION_REF_RE = re.compile(r"uses:\s*['\"]?([^@\s'\"]+)@([^@\s'\"]+)")


def _field(name: str, field_type: str, detail: str) -> dict[str, str]:
    return {"name": name, "type": field_type, "detail": detail}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _release_root(target: Path) -> Path:
    return target / ".brigade" / "release"


def _release_runs_root(target: Path) -> Path:
    return _release_root(target) / "runs"


def _release_candidates_root(target: Path) -> Path:
    return _release_root(target) / "candidates"


def _release_candidates_archive_root(target: Path) -> Path:
    return _release_candidates_root(target) / "archive"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return records
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _git(target: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(target), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _git_value(target: Path, *args: str) -> str | None:
    result = _git(target, *args)
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _git_state(target: Path) -> dict[str, Any]:
    snapshot = work_cmd._git_snapshot(target)
    status_result = _git(target, "status", "--porcelain=v1")
    status = status_result.stdout if status_result.returncode == 0 else ""
    tracked_dirty = [line for line in status.splitlines() if line and not line.startswith("??")]
    upstream = _git_value(target, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    ahead = behind = None
    if upstream:
        counts = _git_value(target, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
        if counts:
            parts = counts.split()
            if len(parts) == 2:
                ahead, behind = int(parts[0]), int(parts[1])
    snapshot.update(
        {
            "head": _git_value(target, "rev-parse", "HEAD"),
            "short_head": _git_value(target, "rev-parse", "--short", "HEAD"),
            "tracked_dirty_files": tracked_dirty,
            "tracked_dirty_count": len(tracked_dirty),
            "upstream": upstream,
            "ahead": ahead,
            "behind": behind,
        }
    )
    return snapshot


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _latest_work_closeout(target: Path) -> dict[str, Any] | None:
    root = target / ".brigade" / "work" / "closeouts"
    if not root.is_dir():
        return None
    closeouts: list[dict[str, Any]] = []
    for child in root.iterdir():
        payload = _read_json(child / "closeout.json") if child.is_dir() else None
        if payload is not None:
            payload.setdefault("path", str(child / "closeout.json"))
            closeouts.append(payload)
    closeouts.sort(key=lambda item: str(item.get("created_at") or item.get("closeout_id") or ""), reverse=True)
    return closeouts[0] if closeouts else None


def _latest_review_closeout(target: Path) -> dict[str, Any] | None:
    for receipt in work_cmd._review_receipts(target):
        closeout = receipt.get("closeout")
        if isinstance(closeout, dict):
            return {"run_id": receipt.get("run_id"), **closeout}
    return None


def _latest_closeout_json(root: Path) -> dict[str, Any] | None:
    if not root.is_dir():
        return None
    candidates = sorted(root.glob("*/closeout.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        payload = _read_json(path)
        if payload is not None:
            payload.setdefault("path", str(path))
            return payload
    return None


def _security_summary(target: Path) -> dict[str, Any]:
    health = security_cmd.health(target)
    return {
        "valid": health.get("valid"),
        "issue_count": health.get("issue_count"),
        "top_issue": health.get("top_issue"),
        "top_finding": health.get("top_finding"),
        "evidence": health.get("evidence"),
        "template_privacy": health.get("template_privacy"),
        "latest_closeout": health.get("latest_closeout"),
    }


def _changed_files(target: Path, base_ref: str | None) -> list[str]:
    files: set[str] = set()
    status_result = _git(target, "status", "--porcelain=v1")
    status = status_result.stdout if status_result.returncode == 0 else ""
    for line in status.splitlines():
        if not line:
            continue
        files.add(line[3:] if len(line) > 3 else line)
    if base_ref:
        result = _git(target, "diff", "--name-only", f"{base_ref}...HEAD")
        if result.returncode == 0:
            files.update(line for line in result.stdout.splitlines() if line.strip())
    return sorted(files)


def _docs_warnings(target: Path, base_ref: str | None) -> list[str]:
    changed = _changed_files(target, base_ref)
    user_facing = [
        path
        for path in changed
        if path.startswith("src/brigade/")
        and not path.startswith("src/brigade/templates/")
        and path.endswith(".py")
    ]
    if not user_facing:
        return []
    warnings: list[str] = []
    for required in ("README.md", "CHANGELOG.md", "ROADMAP.md"):
        if required not in changed:
            warnings.append(f"user-facing changes detected but {required} was not changed")
    return warnings


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
    receipts.sort(key=lambda item: str(item.get("completed_at") or item.get("created_at") or item.get("receipt_id") or ""), reverse=True)
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
        "receipt_id": str(payload.get("receipt_id") or f"install-smoke-{work_cmd._stable_hash({'matrix_id': matrix_id, 'completed_at': completed_at, 'status': status})[:16]}"),
        "matrix_id": matrix_id,
        "depth": depth,
        "harnesses": harnesses,
        "status": status,
        "command_label": _release_safe_text(str(payload.get("command_label") or f"brigade init --depth {depth} --harnesses {','.join(harnesses) or 'none'}")),
        "safe_summary": _release_safe_text(str(payload.get("safe_summary") or payload.get("summary") or f"install smoke {status}")),
        "stdout_summary": _release_safe_text(str(payload.get("stdout_summary") or "")),
        "stderr_summary": _release_safe_text(str(payload.get("stderr_summary") or "")),
        "duration_seconds": payload.get("duration_seconds"),
        "created_at": completed_at,
        "completed_at": completed_at,
    }
    record["source_fingerprint"] = work_cmd._stable_hash({key: value for key, value in record.items() if key not in {"receipt_id", "created_at", "completed_at"}})
    return record


def install_smoke_plan(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    latest_by_matrix = {str(receipt.get("matrix_id") or ""): receipt for receipt in _read_install_smoke_receipts(target)}
    matrix = []
    for item in _install_smoke_matrix():
        latest = latest_by_matrix.get(str(item.get("matrix_id") or ""))
        matrix.append({**item, "latest_receipt_id": latest.get("receipt_id") if isinstance(latest, dict) else None, "latest_status": latest.get("status") if isinstance(latest, dict) else None})
    payload = {"target": str(target), "receipt_path_label": ".brigade/release/install-smoke/receipts.jsonl", "matrix": matrix, "matrix_count": len(matrix)}
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
        payload = {"depth": depth, "harnesses": harnesses, "status": status, "command_label": command_label, "safe_summary": summary}
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
    payload_out = {"target": str(target), "record": record, "receipt_path_label": ".brigade/release/install-smoke/receipts.jsonl"}
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
            issues.append({"status": WARN, "name": "install_smoke_missing", "matrix_id": matrix_id, "detail": f"{matrix_id} has no install smoke receipt", "suggested_next_command": f"brigade release smoke record --depth {item['depth']} --harnesses {','.join(item['harnesses']) or 'none'} --status passed"})
            continue
        if latest.get("status") not in {"passed", "skipped"}:
            issues.append({"status": WARN, "name": "install_smoke_failed", "matrix_id": matrix_id, "receipt_id": latest.get("receipt_id"), "detail": f"{matrix_id} latest smoke is {latest.get('status')}", "suggested_next_command": f"brigade release smoke show {latest.get('receipt_id')}"})
        completed = work_cmd._parse_iso_datetime(latest.get("completed_at"))
        if completed is not None and (now - completed).total_seconds() / 3600 > INSTALL_SMOKE_STALE_HOURS:
            issues.append({"status": WARN, "name": "install_smoke_stale", "matrix_id": matrix_id, "receipt_id": latest.get("receipt_id"), "detail": f"{matrix_id} install smoke is stale", "suggested_next_command": f"brigade release smoke record --depth {item['depth']} --harnesses {','.join(item['harnesses']) or 'none'} --status passed"})
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


def _safe_path_label(target: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(target.resolve()).as_posix()
    except ValueError:
        return path.name


def _ci_safe_excerpt(text: str) -> str:
    excerpt = _release_safe_text(text.strip())
    excerpt = security_cmd._redact_secret_evidence(excerpt)
    if len(excerpt) > 220:
        return excerpt[:217].rstrip() + "..."
    return excerpt


def _ci_log_paths(target: Path, summary_path: Path | None) -> list[Path]:
    if summary_path is not None:
        return [summary_path.expanduser()]
    candidates = [
        target / ".brigade" / "ci" / "github-actions-summary.txt",
        target / ".brigade" / "ci" / "github-actions.log",
        target / ".brigade" / "release" / "ci-summary.txt",
    ]
    workflow_dir = target / ".github" / "workflows"
    if workflow_dir.is_dir():
        candidates.extend(sorted(workflow_dir.glob("*.log")))
    return candidates


def _ci_finding(
    *,
    source: str,
    path_label: str,
    line: int | None,
    title: str,
    excerpt: str,
    action: str | None = None,
    node_runtime: str | None = None,
) -> dict[str, Any]:
    payload = {
        "source": source,
        "path_label": path_label,
        "line": line,
        "title": title,
        "category": "ci-platform-deprecation",
        "severity": "medium",
        "safe_excerpt": excerpt,
        "action": action,
        "node_runtime": node_runtime,
        "suggested_next_command": "Review the workflow action version and update it manually if appropriate.",
    }
    payload["finding_id"] = f"ci-deprecation-{work_cmd._stable_hash(payload)[:16]}"
    payload["source_fingerprint"] = work_cmd._stable_hash({key: value for key, value in payload.items() if key != "finding_id"})
    return payload


def _ci_findings_from_log(target: Path, path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    path_label = _safe_path_label(target, path)
    if not path.is_file():
        return findings, {"path_label": path_label, "exists": False}
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return findings, {"path_label": path_label, "exists": True, "readable": False}
    for index, line in enumerate(lines, start=1):
        if not any(pattern.search(line) for pattern in CI_DEPRECATION_PATTERNS):
            continue
        node_match = re.search(r"(?i)node(?:\.js)?\s*(12|16)", line)
        action_match = re.search(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+)", line)
        findings.append(
            _ci_finding(
                source="summary",
                path_label=path_label,
                line=index,
                title="GitHub Actions platform deprecation warning",
                excerpt=_ci_safe_excerpt(line),
                action=action_match.group(1) if action_match else None,
                node_runtime=f"node{node_match.group(1)}" if node_match else None,
            )
        )
    return findings, {"path_label": path_label, "exists": True, "readable": True, "line_count": len(lines)}


def _ci_findings_from_workflows(target: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    workflow_dir = target / ".github" / "workflows"
    findings: list[dict[str, Any]] = []
    workflows: list[dict[str, Any]] = []
    if not workflow_dir.is_dir():
        return findings, workflows
    for path in sorted([*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")]):
        path_label = _safe_path_label(target, path)
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            workflows.append({"path_label": path_label, "exists": True, "readable": False})
            continue
        workflows.append({"path_label": path_label, "exists": True, "readable": True, "line_count": len(lines)})
        for index, line in enumerate(lines, start=1):
            match = CI_ACTION_REF_RE.search(line)
            if not match:
                continue
            action, ref = match.group(1), match.group(2)
            major_match = re.match(r"v?(\d+)(?:\D|$)", ref)
            major = major_match.group(1) if major_match else None
            if major and action.lower() in CI_DEPRECATED_ACTION_MAJORS and major in CI_DEPRECATED_ACTION_MAJORS[action.lower()]:
                findings.append(
                    _ci_finding(
                        source="workflow",
                        path_label=path_label,
                        line=index,
                        title="GitHub Actions action may use a deprecated Node runtime",
                        excerpt=_ci_safe_excerpt(line),
                        action=f"{action}@{ref}",
                        node_runtime="node16-or-older",
                    )
                )
    return findings, workflows


def ci_platform_payload(target: Path, *, summary_path: Path | None = None) -> dict[str, Any]:
    target = target.expanduser().resolve()
    findings: list[dict[str, Any]] = []
    logs: list[dict[str, Any]] = []
    for path in _ci_log_paths(target, summary_path):
        log_findings, log_summary = _ci_findings_from_log(target, path)
        findings.extend(log_findings)
        logs.append(log_summary)
    workflow_findings, workflows = _ci_findings_from_workflows(target)
    findings.extend(workflow_findings)
    findings.sort(key=lambda item: (str(item.get("path_label") or ""), int(item.get("line") or 0), str(item.get("finding_id") or "")))
    return {
        "target_label": "release-ci",
        "status": WARN if findings else OK,
        "issue_count": len(findings),
        "top_issue": findings[0] if findings else None,
        "findings": findings,
        "logs": logs,
        "workflows": workflows,
        "suggested_next_commands": ["brigade release ci doctor", "brigade release ci import-issues"],
    }


def ci_doctor(*, target: Path, summary_path: Path | None = None, json_output: bool = False) -> int:
    payload = ci_platform_payload(target, summary_path=summary_path)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("release ci platform doctor")
    print(f"issues: {payload['issue_count']}")
    for finding in payload["findings"]:
        print(f"- {finding.get('path_label')}:{finding.get('line')} {finding.get('title')}")
    return 0


def _ci_import_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for finding in payload.get("findings") if isinstance(payload.get("findings"), list) else []:
        if not isinstance(finding, dict):
            continue
        records.append(
            {
                "text": f"Review CI platform deprecation: {finding.get('action') or finding.get('path_label')}",
                "kind": "task",
                "source": "ci-platform-deprecation",
                "type": "workflow",
                "priority": "normal",
                "template": "docs",
                "acceptance": [
                    "The GitHub Actions platform warning is reviewed against the local workflow.",
                    "Any workflow change is made manually and verified through the normal release readiness loop.",
                    "No GitHub workflow, branch, tag, release, or remote state is mutated by Brigade.",
                ],
                "metadata": {
                    "finding_id": finding.get("finding_id"),
                    "path_label": finding.get("path_label"),
                    "line": finding.get("line"),
                    "action": finding.get("action"),
                    "node_runtime": finding.get("node_runtime"),
                    "safe_excerpt": finding.get("safe_excerpt"),
                    "source_item_key": f"{finding.get('path_label')}:{finding.get('line')}:{finding.get('action')}",
                    "source_fingerprint": finding.get("source_fingerprint"),
                },
            }
        )
    return records


def ci_import_issues(*, target: Path, summary_path: Path | None = None, dry_run: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload = ci_platform_payload(target, summary_path=summary_path)
    records = _ci_import_records(payload)
    imported, skipped, dismissed = work_cmd._append_import_records(target, records, dry_run=dry_run)
    result = {
        "target_label": "release-ci",
        "dry_run": dry_run,
        "issue_count": len(records),
        "created": len(imported),
        "skipped": len(skipped),
        "dismissed": len(dismissed),
    }
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print("release ci platform imports")
    print(f"created: {len(imported)}")
    print(f"skipped: {len(skipped)}")
    print(f"dismissed: {len(dismissed)}")
    return 0


def _content_guard_available(target: Path) -> bool:
    if shutil.which("content-guard"):
        return True
    scanner_dir = Path(os.environ.get("CONTENT_GUARD_DIR", str(Path.home() / "repos" / "content-guard")))
    return scanner_dir.is_dir()


def _content_guard_command(target: Path, *, policy: str, introduced: bool, base_ref: str | None) -> tuple[list[str] | None, dict[str, str], str | None]:
    scanner_dir = Path(os.environ.get("CONTENT_GUARD_DIR", str(Path.home() / "repos" / "content-guard")))
    env: dict[str, str] = {}
    if scanner_dir.is_dir():
        policy_path = scanner_dir / "policies" / f"{policy}.json"
        env["PYTHONPATH"] = str(scanner_dir / "src")
        if introduced and base_ref:
            return [
                sys.executable,
                "-m",
                "content_guard.git_scan",
                "--history",
                "--range",
                f"{base_ref}..HEAD",
                "--policy",
                str(policy_path),
            ], env, None
        return [
            sys.executable,
            "-m",
            "content_guard",
            "scan",
            str(target),
            "--policy",
            str(policy_path),
        ], env, None
    executable = shutil.which("content-guard")
    if executable:
        if introduced and base_ref:
            return [executable, "git-scan", "--history", "--range", f"{base_ref}..HEAD", "--policy", policy], env, None
        return [executable, "scan", str(target), "--policy", policy], env, None
    return None, env, "content-guard not available"


def _run_content_guard_check(
    target: Path,
    *,
    name: str,
    policy: str,
    base_ref: str | None = None,
) -> dict[str, Any]:
    introduced = name == "introduced"
    argv, env_updates, error = _content_guard_command(target, policy=policy, introduced=introduced, base_ref=base_ref)
    if error or argv is None:
        return {"name": f"content_guard_{name}", "status": WARN, "detail": error or "not available", "available": False}
    env = os.environ.copy()
    env.update(env_updates)
    result = subprocess.run(
        argv,
        cwd=target,
        env=env,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    status = OK if result.returncode == 0 else FAIL
    return {
        "name": f"content_guard_{name}",
        "status": status,
        "available": True,
        "exit_code": result.returncode,
        "argv": argv,
        "stdout_summary": work_cmd._scanner_run_summary(result.stdout or ""),
        "stderr_summary": work_cmd._scanner_run_summary(result.stderr or ""),
        "detail": "clean" if result.returncode == 0 else "content-guard reported findings",
    }


def _read_release_receipt(path: Path) -> dict[str, Any] | None:
    receipt = path / "receipt.json" if path.is_dir() else path
    payload = _read_json(receipt)
    if payload is not None:
        payload.setdefault("path", str(receipt.parent))
    return payload


def _release_receipts(target: Path) -> list[dict[str, Any]]:
    root = _release_runs_root(target)
    if not root.is_dir():
        return []
    receipts = [_read_release_receipt(path) for path in root.iterdir() if path.is_dir()]
    valid = [item for item in receipts if isinstance(item, dict)]
    valid.sort(key=lambda item: str(item.get("started_at") or item.get("run_id") or ""), reverse=True)
    return valid


def _latest_release_receipt(target: Path) -> dict[str, Any] | None:
    receipts = _release_receipts(target)
    return receipts[0] if receipts else None


def _resolve_release_receipt(target: Path, run_id: str) -> tuple[dict[str, Any] | None, str | None]:
    receipts = _release_receipts(target)
    if run_id == "latest":
        return (receipts[0], None) if receipts else (None, "release run not found: latest")
    matches = [item for item in receipts if str(item.get("run_id") or "").startswith(run_id)]
    if not matches:
        return None, f"release run not found: {run_id}"
    if len(matches) > 1:
        return None, f"release run id is ambiguous: {run_id}"
    return matches[0], None


def _read_candidate(path: Path) -> dict[str, Any] | None:
    candidate_path = path / "EVIDENCE.json" if path.is_dir() else path
    payload = _read_json(candidate_path)
    if payload is not None:
        payload.setdefault("path", str(candidate_path.parent))
    return payload


def _release_candidates(target: Path, *, include_archived: bool = False) -> list[dict[str, Any]]:
    roots = [_release_candidates_root(target)]
    if include_archived:
        roots.append(_release_candidates_archive_root(target))
    candidates: list[dict[str, Any]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if child.name == "archive" or not child.is_dir():
                continue
            payload = _read_candidate(child)
            if payload is not None:
                candidates.append(payload)
    candidates.sort(key=lambda item: str(item.get("created_at") or item.get("candidate_id") or ""), reverse=True)
    return candidates


def _latest_candidate(target: Path) -> dict[str, Any] | None:
    candidates = _release_candidates(target)
    return candidates[0] if candidates else None


def _phase_release_checks(target: Path) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    health = phases_cmd.health(target)
    latest_closeout = health.get("latest_closeout") if isinstance(health.get("latest_closeout"), dict) else None
    latest_report = health.get("latest_report") if isinstance(health.get("latest_report"), dict) else None
    if latest_closeout is not None and int(latest_closeout.get("unresolved_issue_count") or 0) > 0:
        checks.append({"status": WARN, "name": "phase_ledger_unresolved_closeout", "detail": str(latest_closeout.get("closeout_id"))})
    latest_report_compare = health.get("latest_report_compare") if isinstance(health.get("latest_report_compare"), dict) else None
    if latest_report_compare is not None and int(latest_report_compare.get("issue_count") or 0) > 0:
        top_compare = latest_report_compare.get("top_issue") if isinstance(latest_report_compare.get("top_issue"), dict) else {}
        checks.append({"status": WARN, "name": "phase_ledger_report_compare_issue", "detail": str(top_compare.get("name") or latest_report_compare.get("issue_count"))})
    latest_session = health.get("latest_session") if isinstance(health.get("latest_session"), dict) else None
    latest_session_checkpoint = health.get("latest_session_checkpoint") if isinstance(health.get("latest_session_checkpoint"), dict) else None
    latest_session_checkpoint_compare = health.get("latest_session_checkpoint_compare") if isinstance(health.get("latest_session_checkpoint_compare"), dict) else None
    latest_session_report = health.get("latest_session_report") if isinstance(health.get("latest_session_report"), dict) else None
    latest_session_gate = health.get("latest_session_gate") if isinstance(health.get("latest_session_gate"), dict) else None
    if latest_session and latest_session.get("status") not in {"closed", "archived"}:
        checks.append({"status": WARN, "name": "phase_session_active", "detail": str(latest_session.get("session_id"))})
        if latest_session_report is None:
            checks.append({"status": WARN, "name": "phase_session_missing_report", "detail": str(latest_session.get("session_id"))})
        if latest_session.get("closeout_status") is None and latest_session.get("current_phase_id") is None:
            checks.append({"status": WARN, "name": "phase_session_missing_closeout", "detail": str(latest_session.get("session_id"))})
    if latest_session_gate and not latest_session_gate.get("safe_to_claim_complete"):
        top_blocker = latest_session_gate.get("top_blocker") if isinstance(latest_session_gate.get("top_blocker"), dict) else {}
        checks.append({"status": WARN, "name": "phase_session_gate_blocked", "detail": str(top_blocker.get("name") or latest_session_gate.get("blocker_count"))})
    if latest_session_checkpoint and latest_session_checkpoint.get("status") == "blocked":
        checks.append({"status": WARN, "name": "phase_session_checkpoint_blocked", "detail": str(latest_session_checkpoint.get("checkpoint_id"))})
    if latest_session_checkpoint_compare and int(latest_session_checkpoint_compare.get("issue_count") or 0) > 0:
        top_checkpoint = latest_session_checkpoint_compare.get("top_issue") if isinstance(latest_session_checkpoint_compare.get("top_issue"), dict) else {}
        checks.append({"status": WARN, "name": "phase_session_checkpoint_compare_issue", "detail": str(top_checkpoint.get("name") or latest_session_checkpoint_compare.get("issue_count"))})
    if int(health.get("open_action_count") or 0) > 0:
        checks.append({"status": WARN, "name": "phase_session_unresolved_actions", "detail": str(health.get("open_action_count"))})
    records = phases_cmd._records(target)
    pushed_without_closeout = []
    for record in records:
        if record.get("status") != "pushed":
            continue
        phase_id = str(record.get("phase_id") or "")
        if phase_id and not phases_cmd._phase_has_current_closeout(target, phase_id, record):
            pushed_without_closeout.append(phase_id)
    if pushed_without_closeout:
        checks.append({"status": WARN, "name": "phase_ledger_unreviewed_pushed_phase", "detail": ", ".join(pushed_without_closeout[:5])})
    if records:
        record_times = [
            parsed
            for parsed in (work_cmd._parse_iso_datetime(record.get("updated_at") or record.get("completed_at") or record.get("created_at")) for record in records)
            if parsed is not None
        ]
        latest_record_time = max(record_times) if record_times else None
        report_time = work_cmd._parse_iso_datetime(latest_report.get("created_at")) if latest_report else None
        if latest_report is None:
            checks.append({"status": WARN, "name": "phase_ledger_missing_report", "detail": "no phase ledger report found"})
        elif latest_record_time and report_time and latest_record_time > report_time:
            checks.append({"status": WARN, "name": "phase_ledger_stale_report", "detail": str(latest_report.get("report_id"))})
        elif report_time and _now() - report_time > timedelta(hours=PHASE_REPORT_STALE_HOURS):
            checks.append({"status": WARN, "name": "phase_ledger_stale_report", "detail": str(latest_report.get("report_id"))})
    return checks


def _resolve_candidate(target: Path, candidate_id: str) -> tuple[dict[str, Any] | None, str | None]:
    candidates = _release_candidates(target, include_archived=True)
    if candidate_id == "latest":
        latest = _latest_candidate(target)
        return (latest, None) if latest else (None, "release candidate not found: latest")
    matches = [
        item
        for item in candidates
        if str(item.get("candidate_id") or "").startswith(candidate_id)
    ]
    if not matches:
        return None, f"release candidate not found: {candidate_id}"
    if len(matches) > 1:
        return None, f"release candidate id is ambiguous: {candidate_id}"
    return matches[0], None


def _evidence(target: Path, *, base_ref: str | None) -> dict[str, Any]:
    from . import center_cmd, daily_cmd

    sweep = work_cmd._scanner_sweep_health(target)
    review = work_cmd._review_health(target)
    handoffs = handoff_cmd.draft_queue_payload(target)
    research_handoffs = research_cmd.health(target)
    context_health = context_cmd.health(target)
    learning_health = learn_cmd.health(target)
    projects_health = projects_cmd.health(target)
    repo_health = repos_cmd.health(target)
    repo_daily_use = repos_cmd.daily_use_health(target)
    roadmap_health = roadmap_cmd.health(target)
    dogfood_health = _release_dogfood_health(target)
    tool_health = tools_cmd.health(target)
    memory_health = memory_cmd.health(target)
    backup_health = work_cmd._backup_health(target)
    acceptance = work_cmd._acceptance_payload(target)
    inbox_quality = work_cmd._inbox_quality_payload(target)
    phase_ledger = phases_cmd.health(target)
    operator_report_health = center_cmd.report_health(target)
    operator_actions_health = center_cmd.actions_health(target)
    ci_platform = ci_platform_payload(target)
    install_smoke = install_smoke_health(target)
    return {
        "git": _git_state(target),
        "latest_work_closeout": _latest_work_closeout(target),
        "latest_verification": work_cmd._latest_verify_receipt(target),
        "latest_review_closeout": _latest_review_closeout(target),
        "scanner_sweep": {
            "latest": sweep.get("latest"),
            "review": sweep.get("review"),
            "due_count": sweep.get("due_count"),
        },
        "code_review": {
            "latest_run": review.get("latest_run"),
            "latest_unclosed_run": review.get("latest_unclosed_run"),
            "unresolved_finding_count": review.get("unresolved_finding_count"),
            "top_unresolved_finding": review.get("top_unresolved_finding"),
        },
        "security": _security_summary(target),
        "ci_platform": {
            "status": ci_platform.get("status"),
            "issue_count": ci_platform.get("issue_count"),
            "top_issue": ci_platform.get("top_issue"),
            "findings": ci_platform.get("findings"),
            "logs": ci_platform.get("logs"),
            "workflows": ci_platform.get("workflows"),
        },
        "install_smoke": {
            "issue_count": install_smoke.get("issue_count"),
            "top_issue": install_smoke.get("top_issue"),
            "matrix_count": install_smoke.get("matrix_count"),
            "receipt_count": install_smoke.get("receipt_count"),
            "latest_by_matrix": install_smoke.get("latest_by_matrix"),
            "issues": install_smoke.get("issues"),
        },
        "handoff_drafts": {
            "counts": handoffs.get("counts"),
            "issue_count": handoffs.get("issue_count"),
            "top_issue": handoffs.get("top_issue"),
            "latest_ingest_run": handoffs.get("latest_ingest_run"),
            "latest_closeout": _latest_closeout_json(target / ".brigade" / "handoffs" / "closeouts"),
        },
        "research_handoffs": {
            "run_count": research_handoffs.get("run_count"),
            "issue_count": research_handoffs.get("issue_count"),
            "top_issue": research_handoffs.get("top_issue"),
        },
        "backup": {
            "valid": backup_health.get("valid"),
            "issue_count": backup_health.get("issue_count"),
            "raw_issue_count": backup_health.get("raw_issue_count"),
            "quieted_issue_count": backup_health.get("quieted_issue_count"),
            "restore_rehearsal_issue_count": backup_health.get("restore_rehearsal_issue_count"),
            "changed_fingerprint_count": backup_health.get("changed_fingerprint_count"),
            "operator_summary": backup_health.get("operator_summary"),
            "top_issue": backup_health.get("top_issue"),
            "restore_rehearsal_issues": backup_health.get("restore_rehearsal_issues"),
            "latest_closeout": backup_health.get("latest_closeout"),
        },
        "tool_catalog": {
            "valid": tool_health.get("valid"),
            "issue_count": tool_health.get("issue_count"),
            "raw_issue_count": tool_health.get("raw_issue_count"),
            "top_issue": tool_health.get("top_issue"),
            "packs": tool_health.get("packs"),
            "parity": tool_health.get("parity"),
            "sync_plan": tool_health.get("sync_plan"),
            "call_queue": tool_health.get("call_queue"),
            "run_history": tool_health.get("run_history"),
            "checkpoints": tool_health.get("checkpoints"),
        },
        "task_acceptance": {
            "coverage": acceptance.get("coverage"),
            "issue_count": acceptance.get("issue_count"),
            "top_issue": acceptance.get("top_issue"),
            "pending_with_acceptance": acceptance.get("pending_with_acceptance"),
            "pending_missing_acceptance": acceptance.get("pending_missing_acceptance"),
            "done_with_completion": acceptance.get("done_with_completion"),
            "done_missing_completion": acceptance.get("done_missing_completion"),
            "done_missing_completed_acceptance": acceptance.get("done_missing_completed_acceptance"),
            "review_findings": acceptance.get("review_findings"),
            "latest_work_closeout": acceptance.get("latest_work_closeout"),
            "issues": acceptance.get("issues"),
        },
        "inbox_quality": {
            "pending_count": inbox_quality.get("pending_count"),
            "issue_count": inbox_quality.get("issue_count"),
            "issue_counts": inbox_quality.get("issue_counts"),
            "top_issue": inbox_quality.get("top_issue"),
            "best_import": inbox_quality.get("best_import"),
            "noisy_sources": inbox_quality.get("noisy_sources"),
        },
        "memory_care": {
            "valid": memory_health.get("valid"),
            "issue_count": memory_health.get("issue_count"),
            "top_issue": memory_health.get("top_issue"),
            "latest_closeout": memory_health.get("latest_closeout"),
        },
        "context": {
            "pack_count": context_health.get("pack_count"),
            "issue_count": context_health.get("issue_count"),
            "top_issue": context_health.get("top_issue"),
            "latest": context_health.get("latest"),
            "sync": context_health.get("sync"),
        },
        "projects": {
            "project_count": projects_health.get("project_count"),
            "issue_count": projects_health.get("issue_count"),
            "top_issue": projects_health.get("top_issue"),
            "readiness": projects_health.get("readiness"),
            "closeout": projects_health.get("closeout"),
        },
        "learning": {
            "candidate_count": learning_health.get("candidate_count"),
            "raw_candidate_count": learning_health.get("raw_candidate_count"),
            "quieted_candidate_count": learning_health.get("quieted_candidate_count"),
            "changed_fingerprint_count": learning_health.get("changed_fingerprint_count"),
            "issue_count": learning_health.get("issue_count"),
            "top_issue": learning_health.get("top_issue"),
            "latest_closeout": learning_health.get("latest_closeout"),
            "replay": learning_health.get("replay"),
        },
        "repo_fleet": {
            "repo_count": repo_health.get("repo_count"),
            "issue_count": repo_health.get("issue_count"),
            "top_issue": repo_health.get("top_issue"),
            "report": repo_health.get("report"),
            "actions": repo_health.get("actions"),
            "sweep": repo_health.get("sweep"),
            "release_train": repo_health.get("release_train"),
        },
        "repo_fleet_daily_use": {
            "repo_count": repo_daily_use.get("repo_count"),
            "issue_count": repo_daily_use.get("issue_count"),
            "top_issue": repo_daily_use.get("top_issue"),
            "report_issue_count": repo_daily_use.get("report_issue_count"),
            "action_issue_count": repo_daily_use.get("action_issue_count"),
            "sweep_issue_count": repo_daily_use.get("sweep_issue_count"),
            "release_train_issue_count": repo_daily_use.get("release_train_issue_count"),
            "manual_only": repo_daily_use.get("manual_only"),
            "privacy": repo_daily_use.get("privacy"),
        },
        "roadmap": {
            "issue_count": roadmap_health.get("issue_count"),
            "top_issue": roadmap_health.get("top_issue"),
        },
        "phase_ledger": {
            "record_count": phase_ledger.get("record_count"),
            "open_count": phase_ledger.get("open_count"),
            "issue_count": phase_ledger.get("issue_count"),
            "top_issue": phase_ledger.get("top_issue"),
            "latest": phase_ledger.get("latest"),
            "latest_closeout": phase_ledger.get("latest_closeout"),
            "latest_report": phase_ledger.get("latest_report"),
            "latest_report_compare": phase_ledger.get("latest_report_compare"),
            "latest_session": phase_ledger.get("latest_session"),
            "latest_session_checkpoint": phase_ledger.get("latest_session_checkpoint"),
            "latest_session_checkpoint_compare": phase_ledger.get("latest_session_checkpoint_compare"),
            "latest_session_gate": phase_ledger.get("latest_session_gate"),
            "latest_session_report": phase_ledger.get("latest_session_report"),
            "closeout_count": phase_ledger.get("closeout_count"),
        },
        "operator_report": {
            "issue_count": operator_report_health.get("issue_count"),
            "top_issue": operator_report_health.get("top_issue"),
            "latest": operator_report_health.get("latest"),
            "latest_diff": operator_report_health.get("latest_diff"),
        },
        "operator_center_contract": {
            key: value
            for key, value in center_cmd._center_contract_health(target).items()
            if key
            in {
                "schema_version",
                "schema",
                "required_schema_ids",
                "schema_ids",
                "missing_schema_ids",
                "required_item_fields",
                "activity_count",
                "review_count",
                "template_count",
                "issue_count",
                "top_issue",
            }
        },
        "operator_actions": {
            "action_count": operator_actions_health.get("action_count"),
            "open_count": operator_actions_health.get("open_count"),
            "top_action": operator_actions_health.get("top_action"),
            "issue_count": operator_actions_health.get("issue_count"),
            "top_issue": operator_actions_health.get("top_issue"),
        },
        "daily_driver": {
            "health": daily_cmd.health(target),
            "latest_run": daily_cmd._latest_run(target),
            "latest_plan": daily_cmd._latest_plan(target),
            "telemetry": daily_cmd.telemetry_payload(target).get("metrics"),
        },
        "daily_hardening": {
            "audit": {
                key: value
                for key, value in daily_cmd.hardening_audit_payload(target).items()
                if key
                in {
                    "phase_range",
                    "phase_count",
                    "implemented_phase_count",
                    "finding_count",
                    "raw_finding_count",
                    "quieted_count",
                    "top_issue",
                    "workstreams",
                }
            },
            "latest_closeout": daily_cmd._latest_hardening_closeout(target),
        },
        "release_dogfood": {
            "issue_count": dogfood_health.get("issue_count"),
            "top_issue": dogfood_health.get("top_issue"),
            "latest_readiness": dogfood_health.get("latest_readiness"),
            "latest_candidate": dogfood_health.get("latest_candidate"),
            "latest_daily_run": dogfood_health.get("latest_daily_run"),
        },
        "security_closeout": _latest_closeout_json(target / ".brigade" / "security" / "closeouts"),
        "docs": {
            "base_ref": base_ref,
            "changed_files": _changed_files(target, base_ref),
        },
    }


def _assess(evidence: dict[str, Any], checks: list[dict[str, Any]], docs_warnings: list[str]) -> tuple[list[str], list[str]]:
    blockers: list[str] = []
    warnings = list(docs_warnings)
    git = evidence.get("git") if isinstance(evidence.get("git"), dict) else {}
    if git.get("tracked_dirty_count"):
        blockers.append(f"tracked files are dirty: {git.get('tracked_dirty_count')}")
    closeout = evidence.get("latest_work_closeout") if isinstance(evidence.get("latest_work_closeout"), dict) else None
    if closeout is None:
        blockers.append("missing work closeout")
    elif not closeout.get("ready"):
        blockers.append(f"latest work closeout is not ready: {closeout.get('closeout_id')}")
    verify = evidence.get("latest_verification") if isinstance(evidence.get("latest_verification"), dict) else None
    if verify is None:
        blockers.append("missing verification receipt")
    elif verify.get("status") != "completed":
        blockers.append(f"latest verification did not complete: {verify.get('run_id')}")
    review = evidence.get("code_review") if isinstance(evidence.get("code_review"), dict) else {}
    if review.get("latest_unclosed_run"):
        run = review["latest_unclosed_run"]
        blockers.append(f"review run is not closed out: {run.get('run_id') if isinstance(run, dict) else run}")
    if int(review.get("unresolved_finding_count") or 0) > 0:
        blockers.append(f"code review has unresolved finding(s): {review.get('unresolved_finding_count')}")
    task_acceptance = evidence.get("task_acceptance") if isinstance(evidence.get("task_acceptance"), dict) else {}
    if int(task_acceptance.get("issue_count") or 0) > 0:
        top_acceptance = task_acceptance.get("top_issue") if isinstance(task_acceptance.get("top_issue"), dict) else {}
        blockers.append(f"task acceptance has issue(s): {top_acceptance.get('detail') or task_acceptance.get('issue_count')}")
    sweep = evidence.get("scanner_sweep") if isinstance(evidence.get("scanner_sweep"), dict) else {}
    sweep_review = sweep.get("review") if isinstance(sweep.get("review"), dict) else {}
    if int(sweep_review.get("issue_count") or 0) > 0:
        blockers.append(f"scanner sweep has unresolved issue(s): {sweep_review.get('issue_count')}")
    security = evidence.get("security") if isinstance(evidence.get("security"), dict) else {}
    if int(security.get("issue_count") or 0) > 0:
        blockers.append(f"security has open issue(s): {security.get('issue_count')}")
    ci_platform = evidence.get("ci_platform") if isinstance(evidence.get("ci_platform"), dict) else {}
    if int(ci_platform.get("issue_count") or 0) > 0:
        top_ci = ci_platform.get("top_issue") if isinstance(ci_platform.get("top_issue"), dict) else {}
        warnings.append(f"ci platform deprecation warning(s): {top_ci.get('safe_excerpt') or ci_platform.get('issue_count')}")
    install_smoke = evidence.get("install_smoke") if isinstance(evidence.get("install_smoke"), dict) else {}
    if int(install_smoke.get("issue_count") or 0) > 0:
        top_smoke = install_smoke.get("top_issue") if isinstance(install_smoke.get("top_issue"), dict) else {}
        warnings.append(f"install smoke matrix issue(s): {top_smoke.get('detail') or install_smoke.get('issue_count')}")
    handoffs = evidence.get("handoff_drafts") if isinstance(evidence.get("handoff_drafts"), dict) else {}
    if int(handoffs.get("issue_count") or 0) > 0:
        blockers.append(f"handoff draft queue has issue(s): {handoffs.get('issue_count')}")
    research_handoffs = evidence.get("research_handoffs") if isinstance(evidence.get("research_handoffs"), dict) else {}
    if int(research_handoffs.get("issue_count") or 0) > 0:
        top_research = research_handoffs.get("top_issue") if isinstance(research_handoffs.get("top_issue"), dict) else {}
        warnings.append(f"research handoff export issue(s): {top_research.get('run_id') or research_handoffs.get('issue_count')}")
    operator_report = evidence.get("operator_report") if isinstance(evidence.get("operator_report"), dict) else {}
    if int(operator_report.get("issue_count") or 0) > 0:
        top_report = operator_report.get("top_issue") if isinstance(operator_report.get("top_issue"), dict) else {}
        warnings.append(f"operator report has issue(s): {top_report.get('detail') or operator_report.get('issue_count')}")
    operator_actions = evidence.get("operator_actions") if isinstance(evidence.get("operator_actions"), dict) else {}
    if int(operator_actions.get("open_count") or 0) > 0:
        top_action = operator_actions.get("top_action") if isinstance(operator_actions.get("top_action"), dict) else {}
        warnings.append(f"operator action queue has open action(s): {top_action.get('action_id') or operator_actions.get('open_count')}")
    repo_fleet = evidence.get("repo_fleet") if isinstance(evidence.get("repo_fleet"), dict) else {}
    repo_actions = repo_fleet.get("actions") if isinstance(repo_fleet.get("actions"), dict) else {}
    if int(repo_actions.get("open_count") or 0) > 0:
        top_action = repo_actions.get("top_action") if isinstance(repo_actions.get("top_action"), dict) else {}
        warnings.append(f"repo fleet action queue has open action(s): {top_action.get('fleet_action_id') or repo_actions.get('open_count')}")
    repo_sweep = repo_fleet.get("sweep") if isinstance(repo_fleet.get("sweep"), dict) else {}
    if int(repo_sweep.get("issue_count") or 0) > 0:
        top_sweep = repo_sweep.get("top_issue") if isinstance(repo_sweep.get("top_issue"), dict) else {}
        warnings.append(f"repo fleet sweep has issue(s): {top_sweep.get('detail') or repo_sweep.get('issue_count')}")
    repo_release = repo_fleet.get("release_train") if isinstance(repo_fleet.get("release_train"), dict) else {}
    if int(repo_release.get("issue_count") or 0) > 0:
        top_release = repo_release.get("top_issue") if isinstance(repo_release.get("top_issue"), dict) else {}
        warnings.append(f"repo fleet release train has issue(s): {top_release.get('detail') or repo_release.get('issue_count')}")
    for check in checks:
        if check.get("status") == FAIL:
            blockers.append(f"{check.get('name')}: {check.get('detail')}")
        elif check.get("status") == WARN:
            warnings.append(f"{check.get('name')}: {check.get('detail')}")
    return blockers, warnings


def _payload(target: Path, *, base_ref: str | None, run_checks: bool, policy: str = "public-repo") -> dict[str, Any]:
    evidence = _evidence(target, base_ref=base_ref)
    checks: list[dict[str, Any]] = []
    if run_checks:
        checks.append(_run_content_guard_check(target, name="tip", policy=policy, base_ref=base_ref))
        if base_ref:
            checks.append(_run_content_guard_check(target, name="introduced", policy=policy, base_ref=base_ref))
    elif not _content_guard_available(target):
        checks.append({"name": "content_guard", "status": WARN, "detail": "content-guard not available", "available": False})
    blockers, warnings = _assess(evidence, checks, _docs_warnings(target, base_ref))
    return {
        "target": str(target),
        "base_ref": base_ref,
        "policy": policy,
        "release_runs_root": str(_release_runs_root(target)),
        "status": "ready" if not blockers else "blocked",
        "ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
        "evidence": evidence,
    }


def _payload_with_candidate_health(payload: dict[str, Any], target: Path) -> dict[str, Any]:
    candidate_health = _candidate_health(target)
    checks = list(payload.get("checks") if isinstance(payload.get("checks"), list) else [])
    checks.extend(candidate_health.get("checks") if isinstance(candidate_health.get("checks"), list) else [])
    checks.extend(_phase_release_checks(target))
    latest_candidate = candidate_health.get("latest") if isinstance(candidate_health.get("latest"), dict) else None
    if latest_candidate is not None:
        audit = _candidate_audit_payload(target, latest_candidate)
        checks.extend(
            {
                "status": issue.get("status", WARN),
                "name": f"release_candidate_audit_{issue.get('name')}",
                "detail": issue.get("detail"),
            }
            for issue in audit.get("issues", [])
        )
    blockers, warnings = _assess(
        payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {},
        checks,
        _docs_warnings(target, payload.get("base_ref") if isinstance(payload.get("base_ref"), str) else None),
    )
    updated = {
        **payload,
        "status": "ready" if not blockers else "blocked",
        "ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
        "release_candidate_health": candidate_health,
    }
    return updated


def _write_release_markdown(path: Path, receipt: dict[str, Any]) -> None:
    lines = [
        "# Brigade Release Readiness",
        "",
        f"- Run: `{receipt.get('run_id')}`",
        f"- Status: {receipt.get('status')}",
        f"- Ready: {receipt.get('ready')}",
        f"- Target: `{receipt.get('target')}`",
        "",
        "## Blockers",
        "",
    ]
    blockers = receipt.get("blockers") if isinstance(receipt.get("blockers"), list) else []
    lines.extend(f"- {item}" for item in blockers) if blockers else lines.append("- none")
    lines.extend(["", "## Warnings", ""])
    warnings = receipt.get("warnings") if isinstance(receipt.get("warnings"), list) else []
    lines.extend(f"- {item}" for item in warnings) if warnings else lines.append("- none")
    path.with_name("summary.md").write_text("\n".join(lines) + "\n")


def _candidate_docs_touch(changed_files: list[str]) -> dict[str, bool]:
    return {name: name in changed_files for name in ("README.md", "CHANGELOG.md", "ROADMAP.md")}


def _release_safe_text(text: str) -> str:
    redacted = RELEASE_PRIVATE_VALUE_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    return RELEASE_PRIVATE_PATH_RE.sub("[redacted-path]", redacted)


def _commit_subjects(target: Path, base_ref: str | None) -> list[str]:
    args = ["log", "--format=%s"]
    if base_ref:
        args.append(f"{base_ref}..HEAD")
    else:
        args.extend(["-n", "20"])
    result = _git(target, *args)
    if result.returncode != 0:
        return []
    return [_release_safe_text(line.strip()) for line in result.stdout.splitlines() if line.strip()]


def _changelog_unreleased(path: Path) -> list[str]:
    changelog = path / "CHANGELOG.md"
    if not changelog.is_file():
        return []
    lines = changelog.read_text().splitlines()
    capture = False
    items: list[str] = []
    for line in lines:
        if line.startswith("## [Unreleased]"):
            capture = True
            continue
        if capture and line.startswith("## "):
            break
        if capture and line.strip().startswith("- "):
            items.append(_release_safe_text(line.strip()[2:]))
        if len(items) >= 20:
            break
    return items


def _latest_release_or_payload(target: Path, *, base_ref: str | None) -> dict[str, Any]:
    latest = _latest_release_receipt(target)
    if latest is not None:
        return latest
    payload = _payload(target, base_ref=base_ref, run_checks=True)
    return {
        **payload,
        "run_id": "inline-readiness",
        "path": None,
        "started_at": _now().isoformat(),
        "completed_at": _now().isoformat(),
    }


def _candidate_payload(target: Path, *, base_ref: str | None) -> dict[str, Any]:
    readiness = _latest_release_or_payload(target, base_ref=base_ref)
    evidence = readiness.get("evidence") if isinstance(readiness.get("evidence"), dict) else {}
    git = evidence.get("git") if isinstance(evidence.get("git"), dict) else _git_state(target)
    changed_files = evidence.get("docs", {}).get("changed_files") if isinstance(evidence.get("docs"), dict) else None
    if not isinstance(changed_files, list):
        changed_files = _changed_files(target, base_ref)
    return {
        "target": str(target),
        "base_ref": base_ref,
        "release_readiness": {
            "run_id": readiness.get("run_id"),
            "status": readiness.get("status"),
            "ready": readiness.get("ready"),
            "path": readiness.get("path"),
            "blockers": readiness.get("blockers") if isinstance(readiness.get("blockers"), list) else [],
            "warnings": readiness.get("warnings") if isinstance(readiness.get("warnings"), list) else [],
            "checks": readiness.get("checks") if isinstance(readiness.get("checks"), list) else [],
        },
        "release_readiness_receipt": readiness,
        "work_closeout": evidence.get("latest_work_closeout"),
        "verification": evidence.get("latest_verification"),
        "code_review": {
            "latest_closeout": evidence.get("latest_review_closeout"),
            "health": evidence.get("code_review"),
        },
        "scanner_sweep": evidence.get("scanner_sweep"),
        "security": evidence.get("security"),
        "ci_platform": evidence.get("ci_platform"),
        "install_smoke": evidence.get("install_smoke"),
        "security_closeout": evidence.get("security_closeout"),
        "handoff_drafts": evidence.get("handoff_drafts"),
        "backup": evidence.get("backup"),
        "tool_catalog": evidence.get("tool_catalog"),
        "task_acceptance": evidence.get("task_acceptance"),
        "inbox_quality": evidence.get("inbox_quality"),
        "memory_care": evidence.get("memory_care"),
        "context": evidence.get("context"),
        "projects": evidence.get("projects"),
        "learning": evidence.get("learning"),
        "operator_report": evidence.get("operator_report"),
        "operator_center_contract": evidence.get("operator_center_contract"),
        "daily_driver": evidence.get("daily_driver"),
        "daily_hardening": evidence.get("daily_hardening"),
        "release_dogfood": evidence.get("release_dogfood"),
        "repo_fleet": evidence.get("repo_fleet"),
        "repo_fleet_daily_use": evidence.get("repo_fleet_daily_use"),
        "roadmap": evidence.get("roadmap"),
        "phase_ledger": evidence.get("phase_ledger"),
        "git": git,
        "changed_files": changed_files,
        "docs_touch_status": _candidate_docs_touch([str(item) for item in changed_files]),
        "content_guard": {
            str(check.get("name")): check
            for check in readiness.get("checks", [])
            if isinstance(check, dict) and str(check.get("name", "")).startswith("content_guard")
        },
        "release_notes_inputs": {
            "changelog_unreleased": _changelog_unreleased(target),
            "commit_subjects": _commit_subjects(target, base_ref),
            "touched_docs": [path for path in changed_files if str(path).startswith("docs/")],
        },
        "command_contract": _command_contract_snapshot(target),
        "blockers": readiness.get("blockers") if isinstance(readiness.get("blockers"), list) else [],
        "warnings": readiness.get("warnings") if isinstance(readiness.get("warnings"), list) else [],
        "suggested_next_commands": [
            "brigade release doctor",
            "brigade work verify run",
            "brigade work closeout latest",
            "brigade release candidate build",
        ],
    }


def _command_contract_snapshot(target: Path) -> dict[str, Any]:
    payload = roadmap_cmd.command_contract_payload(target)
    snapshot = {
        "cli_command_count": len(payload.get("cli_commands") if isinstance(payload.get("cli_commands"), list) else []),
        "documented_command_count": len(payload.get("normalized_documented_commands") if isinstance(payload.get("normalized_documented_commands"), list) else []),
        "issue_count": payload.get("issue_count"),
        "top_issue": payload.get("top_issue"),
    }
    snapshot["fingerprint"] = work_cmd._stable_hash(snapshot)
    return snapshot


def _candidate_health(target: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    latest = _latest_candidate(target)
    if latest is None:
        return {"latest": None, "checks": checks, "issue_count": 0, "top_issue": None}
    created = work_cmd._parse_iso_datetime(latest.get("created_at"))
    if created is not None:
        age_hours = (_now() - created).total_seconds() / 3600
        if age_hours > RELEASE_CANDIDATE_STALE_HOURS:
            checks.append({"status": WARN, "name": "release_candidate_stale", "detail": f"{latest.get('candidate_id')}={age_hours:.1f}h"})
    git = latest.get("git") if isinstance(latest.get("git"), dict) else {}
    current_head = _git_value(target, "rev-parse", "HEAD")
    if git.get("head") and current_head and git.get("head") != current_head:
        checks.append({"status": WARN, "name": "release_candidate_head_changed", "detail": f"{latest.get('candidate_id')} head changed"})
    readiness = latest.get("release_readiness") if isinstance(latest.get("release_readiness"), dict) else {}
    if readiness.get("ready") is False:
        checks.append({"status": WARN, "name": "release_candidate_blocked", "detail": f"{latest.get('candidate_id')} readiness was blocked"})
    for label, value in (
        ("release_candidate_missing_release_receipt", readiness.get("path")),
        ("release_candidate_missing_work_closeout", (latest.get("work_closeout") or {}).get("path") if isinstance(latest.get("work_closeout"), dict) else None),
        ("release_candidate_missing_verification", (latest.get("verification") or {}).get("path") if isinstance(latest.get("verification"), dict) else None),
    ):
        if value and not Path(str(value)).exists():
            checks.append({"status": WARN, "name": label, "detail": str(value)})
    return {"latest": latest, "checks": checks, "issue_count": len(checks), "top_issue": checks[0] if checks else None}


def _release_dogfood_health(target: Path) -> dict[str, Any]:
    from . import daily_cmd

    target = target.expanduser().resolve()
    def local_ref(payload: dict[str, Any] | None, id_field: str) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        return {
            "id": payload.get(id_field),
            "status": payload.get("status"),
            "path": payload.get("path"),
        }

    checks: list[dict[str, Any]] = []
    latest_readiness = _latest_release_receipt(target)
    latest_candidate = _latest_candidate(target)
    latest_daily_run = daily_cmd._latest_run(target)
    if latest_readiness is None:
        checks.append({"status": WARN, "name": "release_dogfood_readiness_missing", "detail": "no release readiness receipt found", "phase": 155, "suggested_next_command": "brigade release run"})
    elif not latest_readiness.get("ready"):
        checks.append({"status": WARN, "name": "release_dogfood_readiness_blocked", "detail": str(latest_readiness.get("run_id") or "latest"), "phase": 158, "suggested_next_command": f"brigade release show {latest_readiness.get('run_id')}"})
    if latest_candidate is None:
        checks.append({"status": WARN, "name": "release_dogfood_candidate_missing", "detail": "no release candidate evidence found", "phase": 156, "suggested_next_command": "brigade release candidate build"})
    else:
        for key in ("daily_driver", "daily_hardening", "inbox_quality", "repo_fleet_daily_use"):
            if not isinstance(latest_candidate.get(key), dict):
                checks.append({"status": WARN, "name": f"release_dogfood_candidate_missing_{key}", "detail": f"candidate missing {key}", "phase": 157, "suggested_next_command": "brigade release candidate build"})
        candidate_path = Path(str(latest_candidate.get("path") or ""))
        publish_plan = candidate_path / "PUBLISH_PLAN.md"
        if publish_plan.is_file():
            unsafe_lines = [
                line
                for line in publish_plan.read_text().splitlines()
                if any(command in line for command in ("git push", "git tag", "gh release create"))
                and "Manual-only" not in line
            ]
            if unsafe_lines:
                checks.append({"status": WARN, "name": "release_dogfood_publish_plan_not_manual", "detail": "remote-mutating publish command is not marked manual-only", "phase": 160, "suggested_next_command": f"brigade release candidate show {latest_candidate.get('candidate_id')}"})
        else:
            checks.append({"status": WARN, "name": "release_dogfood_publish_plan_missing", "detail": "candidate publish plan is missing", "phase": 160, "suggested_next_command": "brigade release candidate build"})
    if latest_daily_run is not None:
        if latest_daily_run.get("closeout_status") and "verification_status" not in latest_daily_run:
            checks.append({"status": WARN, "name": "release_dogfood_daily_closeout_missing_verification", "detail": str(latest_daily_run.get("run_id") or "latest"), "phase": 161, "suggested_next_command": "brigade daily closeout --json"})
    schema_ids = {schema["id"] for schema in _schema_manifest_schemas()}
    if "release-dogfood-health" not in schema_ids:
        checks.append({"status": WARN, "name": "release_dogfood_schema_missing", "detail": "schema manifest missing release dogfood health", "phase": 163, "suggested_next_command": "brigade release schema"})
    return {
        "schema_version": SCHEMA_MANIFEST_VERSION,
        "schema": {"name": "release-dogfood-health", "version": SCHEMA_MANIFEST_VERSION},
        "target": str(target),
        "latest_readiness": local_ref(latest_readiness, "run_id"),
        "latest_candidate": local_ref(latest_candidate, "candidate_id"),
        "latest_daily_run": local_ref(latest_daily_run, "run_id"),
        "checks": checks,
        "issue_count": len(checks),
        "top_issue": checks[0] if checks else None,
    }


def _schema_manifest_schemas() -> list[dict[str, Any]]:
    return [
        {
            "id": "release-readiness-receipt",
            "file": ".brigade/release/runs/<run-id>/receipt.json",
            "description": "Local release readiness receipt.",
            "required_fields": [
                _field("run_id", "string", "Unique local release run id."),
                _field("target", "string", "Inspected repo or workspace."),
                _field("status", "string", "ready or blocked."),
                _field("ready", "boolean", "True when no blockers were found."),
                _field("blockers", "array<string>", "Blocking readiness findings."),
                _field("warnings", "array<string>", "Non-blocking readiness findings."),
                _field("checks", "array<object>", "Local check summaries."),
                _field("evidence", "object", "Collected subsystem evidence."),
            ],
            "optional_fields": [
                _field("started_at", "string", "Start timestamp."),
                _field("completed_at", "string", "Completion timestamp."),
                _field("path", "string", "Local receipt directory."),
            ],
        },
        {
            "id": "release-candidate-evidence",
            "file": ".brigade/release/candidates/<candidate-id>/EVIDENCE.json",
            "description": "Local release candidate evidence packet.",
            "required_fields": [
                _field("candidate_id", "string", "Unique local candidate id."),
                _field("release_readiness", "object", "Readiness summary copied into the candidate."),
                _field("release_readiness_receipt", "object", "Full readiness receipt or inline readiness payload."),
                _field("git", "object", "Captured git state."),
                _field("changed_files", "array<string>", "Changed files for review."),
                _field("blockers", "array<string>", "Candidate blockers."),
                _field("warnings", "array<string>", "Candidate warnings."),
                _field("bundle_files", "array<string>", "Files written in the candidate bundle."),
            ],
            "optional_fields": [
                _field("work_closeout", "object", "Latest work closeout receipt."),
                _field("verification", "object", "Latest verification receipt."),
                _field("code_review", "object", "Code review closeout summary."),
                _field("security", "object", "Security health and closeout summary."),
                _field("ci_platform", "object", "Local GitHub Actions platform deprecation summary."),
                _field("install_smoke", "object", "Local install smoke matrix receipt summary."),
                _field("handoff_drafts", "object", "Handoff draft and ingest summary."),
            ],
        },
        {
            "id": "fleet-release-train-evidence",
            "file": ".brigade/repos/releases/<train-id>/FLEET_RELEASE_EVIDENCE.json",
            "description": "Local repo-fleet release train evidence packet.",
            "required_fields": [
                _field("train_id", "string", "Unique local train id."),
                _field("repos", "array<object>", "Safe per-repo release states."),
                _field("classifications", "object", "Per-repo readiness classes."),
                _field("blocker_count", "integer", "Total blocker count."),
                _field("warning_count", "integer", "Total warning count."),
            ],
            "optional_fields": [
                _field("closeout", "object", "Reviewed, deferred, superseded, or archived closeout state."),
                _field("manual_publish_plan", "object", "Manual-only publish checklist references."),
            ],
        },
        {
            "id": "fleet-release-waiver",
            "file": ".brigade/repos/releases/waivers.jsonl",
            "description": "Local waiver record for fleet release ready gates.",
            "required_fields": [
                _field("waiver_id", "string", "Stable waiver id."),
                _field("train_id", "string", "Release train id."),
                _field("scope", "string", "Waived blocker scope."),
                _field("status", "string", "active or revoked."),
                _field("reason", "string", "Reviewed reason."),
            ],
            "optional_fields": [
                _field("repo_id", "string", "Optional safe repo id."),
                _field("expires_at", "string", "Optional expiry timestamp."),
                _field("owner_label", "string", "Optional safe review owner label."),
                _field("source_fingerprint", "string", "Source fingerprint at waiver time."),
            ],
        },
        {
            "id": "fleet-release-manual-evidence",
            "file": ".brigade/repos/releases/evidence.jsonl",
            "description": "Local manual evidence record for fleet release steps.",
            "required_fields": [
                _field("evidence_id", "string", "Stable local evidence id."),
                _field("repo_id", "string", "Safe repo id."),
                _field("train_id", "string", "Release train id."),
                _field("step", "string", "Manual release step."),
                _field("status", "string", "completed, skipped, deferred, blocked, or missing."),
                _field("safe_summary", "string", "Private-safe summary."),
            ],
            "optional_fields": [
                _field("source_fingerprint", "string", "Fingerprint for reconciliation."),
                _field("receipt_label", "string", "Local receipt label."),
            ],
        },
        {
            "id": "release-dogfood-health",
            "file": "computed",
            "description": "Local self-dogfood release health summary used by daily hardening and release evidence.",
            "required_fields": [
                _field("latest_readiness", "object|null", "Latest local release readiness reference."),
                _field("latest_candidate", "object|null", "Latest local release candidate reference."),
                _field("latest_daily_run", "object|null", "Latest local daily run reference."),
                _field("checks", "array<object>", "Dogfood release checks."),
                _field("issue_count", "integer", "Number of dogfood health issues."),
            ],
        },
    ]


def _latest_fleet_release_train(target: Path) -> dict[str, Any] | None:
    try:
        return repos_cmd.latest_release_train(target)
    except Exception:
        return None


def _schema_manifest(target: Path) -> dict[str, Any]:
    latest_readiness = _latest_release_receipt(target)
    latest_candidate = _latest_candidate(target)
    latest_train = _latest_fleet_release_train(target)
    waivers_path = target / ".brigade" / "repos" / "releases" / "waivers.jsonl"
    manual_evidence_path = target / ".brigade" / "repos" / "releases" / "evidence.jsonl"
    checks: list[dict[str, Any]] = []
    checks.append(
        {
            "name": "release_readiness_latest",
            "status": OK if latest_readiness else WARN,
            "detail": str(latest_readiness.get("path")) if latest_readiness else "no release readiness receipt found",
        }
    )
    checks.append(
        {
            "name": "release_candidate_latest",
            "status": OK if latest_candidate else WARN,
            "detail": str(latest_candidate.get("path")) if latest_candidate else "no release candidate evidence found",
        }
    )
    candidate_health = _candidate_health(target)
    checks.extend(candidate_health.get("checks") if isinstance(candidate_health.get("checks"), list) else [])
    checks.append(
        {
            "name": "fleet_release_train_latest",
            "status": OK if latest_train else WARN,
            "detail": str(latest_train.get("path")) if latest_train else "no fleet release train evidence found",
        }
    )
    checks.append(
        {
            "name": "fleet_release_waivers",
            "status": OK,
            "detail": str(waivers_path) if waivers_path.exists() else "no waiver records found",
        }
    )
    checks.append(
        {
            "name": "fleet_release_manual_evidence",
            "status": OK,
            "detail": str(manual_evidence_path) if manual_evidence_path.exists() else "no manual evidence records found",
        }
    )
    return {
        "target": str(target),
        "manifest_version": SCHEMA_MANIFEST_VERSION,
        "generated_at": _now().isoformat(),
        "schema_count": len(_schema_manifest_schemas()),
        "schemas": _schema_manifest_schemas(),
        "latest": {
            "release_readiness": _receipt_ref(latest_readiness, "run_id"),
            "release_candidate": _receipt_ref(latest_candidate, "candidate_id"),
            "fleet_release_train": _receipt_ref(latest_train, "train_id"),
        },
        "checks": checks,
        "issue_count": len([check for check in checks if check.get("status") != OK]),
    }


def _receipt_ref(payload: dict[str, Any] | None, id_field: str) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    return {
        "id": payload.get(id_field),
        "path": payload.get("path"),
        "status": payload.get("status"),
    }


def _candidate_release_notes(candidate: dict[str, Any]) -> str:
    inputs = candidate.get("release_notes_inputs") if isinstance(candidate.get("release_notes_inputs"), dict) else {}
    changelog = inputs.get("changelog_unreleased") if isinstance(inputs.get("changelog_unreleased"), list) else []
    commits = inputs.get("commit_subjects") if isinstance(inputs.get("commit_subjects"), list) else []
    docs = inputs.get("touched_docs") if isinstance(inputs.get("touched_docs"), list) else []
    lines = ["# Release Notes Draft", "", "## Highlights", ""]
    if changelog:
        lines.extend(f"- {item}" for item in changelog[:10])
    else:
        lines.append("- review-needed: summarize user-visible changes.")
    lines.extend(["", "## Commit Subjects", ""])
    lines.extend(f"- {item}" for item in commits[:20]) if commits else lines.append("- review-needed: no commit subjects found for base ref.")
    lines.extend(["", "## Documentation Touched", ""])
    lines.extend(f"- `{item}`" for item in docs[:20]) if docs else lines.append("- review-needed: confirm docs coverage.")
    return "\n".join(lines) + "\n"


def _candidate_publish_plan(candidate: dict[str, Any]) -> str:
    head = candidate.get("git", {}).get("short_head") if isinstance(candidate.get("git"), dict) else None
    branch = candidate.get("git", {}).get("branch") if isinstance(candidate.get("git"), dict) else None
    lines = [
        "# Publish Plan",
        "",
        "- [ ] Review `RELEASE_CANDIDATE.md`.",
        "- [ ] Review `EVIDENCE.json`.",
        "- [ ] Run `brigade work verify run` if verification is stale.",
        "- [ ] Run `brigade work closeout latest` if work closeout is stale.",
        "- [ ] Run `brigade release doctor`.",
        "- [ ] Run content-guard through the configured pre-push hook or `brigade scrub`.",
        f"- [ ] Manual-only remote step: `git tag <version> {head or 'HEAD'}`.",
        f"- [ ] Manual-only remote step: `git push origin {branch or '<branch>'} --tags`.",
        "- [ ] Manual-only remote step: `gh release create <version> --notes-file RELEASE_NOTES_DRAFT.md`.",
    ]
    return "\n".join(lines) + "\n"


def _candidate_summary(candidate: dict[str, Any]) -> str:
    readiness = candidate.get("release_readiness") if isinstance(candidate.get("release_readiness"), dict) else {}
    lines = [
        "# Release Candidate",
        "",
        f"- Candidate: `{candidate.get('candidate_id')}`",
        f"- Status: {candidate.get('status')}",
        f"- Ready: {candidate.get('ready')}",
        f"- Readiness: `{readiness.get('run_id')}` [{readiness.get('status')}]",
        f"- Base ref: {candidate.get('base_ref')}",
        "",
        "## Blockers",
        "",
    ]
    blockers = candidate.get("blockers") if isinstance(candidate.get("blockers"), list) else []
    lines.extend(f"- {item}" for item in blockers) if blockers else lines.append("- none")
    lines.extend(["", "## Warnings", ""])
    warnings = candidate.get("warnings") if isinstance(candidate.get("warnings"), list) else []
    lines.extend(f"- {item}" for item in warnings) if warnings else lines.append("- none")
    lines.extend(["", "## Changed Files", ""])
    changed = candidate.get("changed_files") if isinstance(candidate.get("changed_files"), list) else []
    lines.extend(f"- `{item}`" for item in changed[:80]) if changed else lines.append("- none")
    return "\n".join(lines) + "\n"


def _write_candidate_bundle(candidate_dir: Path, candidate: dict[str, Any]) -> None:
    _write_json(candidate_dir / "EVIDENCE.json", candidate)
    (candidate_dir / "RELEASE_CANDIDATE.md").write_text(_candidate_summary(candidate))
    (candidate_dir / "RELEASE_NOTES_DRAFT.md").write_text(_candidate_release_notes(candidate))
    (candidate_dir / "PUBLISH_PLAN.md").write_text(_candidate_publish_plan(candidate))


def plan(*, target: Path, base_ref: str | None = "origin/main", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _payload(target, base_ref=base_ref, run_checks=False)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"release plan: {target}")
    print(f"status: {payload['status']}")
    print(f"blockers: {len(payload['blockers'])}")
    for blocker in payload["blockers"]:
        print(f"- {blocker}")
    print(f"warnings: {len(payload['warnings'])}")
    for warning in payload["warnings"]:
        print(f"- {warning}")
    print("run: brigade release run")
    return 0


def doctor(*, target: Path, base_ref: str | None = "origin/main", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _payload_with_candidate_health(_payload(target, base_ref=base_ref, run_checks=True), target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["ready"] else 1
    print(f"release doctor: {target}")
    print(f"status: {payload['status']}")
    for check in payload["checks"]:
        print(f"[{check['status']}] {check['name']}: {check['detail']}")
    for blocker in payload["blockers"]:
        print(f"blocker: {blocker}")
    for warning in payload["warnings"]:
        print(f"warning: {warning}")
    return 0 if payload["ready"] else 1


def schema(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _schema_manifest(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"release schema manifest: {target}")
    print(f"manifest_version: {payload['manifest_version']}")
    print(f"schemas: {payload['schema_count']}")
    print(f"issues: {payload['issue_count']}")
    for schema_item in payload["schemas"]:
        print(f"- {schema_item['id']}: {schema_item['file']}")
    for check in payload["checks"]:
        print(f"[{check['status']}] {check['name']}: {check['detail']}")
    return 0


def candidate_plan(*, target: Path, base_ref: str | None = "origin/main", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    candidate = _candidate_payload(target, base_ref=base_ref)
    candidate.update(
        {
            "candidate_id": "planned",
            "created_at": None,
            "status": candidate["release_readiness"].get("status"),
            "ready": candidate["release_readiness"].get("ready"),
            "candidate_root": str(_release_candidates_root(target)),
            "bundle_files": ["RELEASE_CANDIDATE.md", "RELEASE_NOTES_DRAFT.md", "PUBLISH_PLAN.md", "EVIDENCE.json"],
        }
    )
    if json_output:
        print(json.dumps(candidate, indent=2, sort_keys=True))
        return 0
    print(f"release candidate plan: {target}")
    print(f"status: {candidate['status']}")
    print(f"ready: {candidate['ready']}")
    print(f"blockers: {len(candidate['blockers'])}")
    for blocker in candidate["blockers"]:
        print(f"- {blocker}")
    print(f"candidate_root: {candidate['candidate_root']}")
    print("run: brigade release candidate build")
    return 0


def candidate_build(*, target: Path, base_ref: str | None = "origin/main", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    created = _now()
    candidate_id = f"{created.strftime('%Y%m%d-%H%M%S')}-candidate-{uuid4().hex[:6]}"
    candidate_dir = _release_candidates_root(target) / candidate_id
    candidate = _candidate_payload(target, base_ref=base_ref)
    candidate.update(
        {
            "candidate_id": candidate_id,
            "created_at": created.isoformat(),
            "status": candidate["release_readiness"].get("status"),
            "ready": candidate["release_readiness"].get("ready"),
            "path": str(candidate_dir),
            "bundle_files": ["RELEASE_CANDIDATE.md", "RELEASE_NOTES_DRAFT.md", "PUBLISH_PLAN.md", "EVIDENCE.json"],
        }
    )
    _write_candidate_bundle(candidate_dir, candidate)
    if json_output:
        print(json.dumps(candidate, indent=2, sort_keys=True))
        return 0
    print(f"release candidate: {candidate_id}")
    print(f"status: {candidate['status']}")
    print(f"ready: {candidate['ready']}")
    print(f"blockers: {len(candidate['blockers'])}")
    print(f"path: {candidate_dir}")
    return 0


def candidate_list(*, target: Path, limit: int = 20, json_output: bool = False) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    candidates = _release_candidates(target)[:limit]
    payload = {"target": str(target), "candidate_root": str(_release_candidates_root(target)), "candidates": candidates}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"release candidates: {target}")
    print(f"candidate_root: {payload['candidate_root']}")
    if not candidates:
        print("candidates: none")
        return 0
    for candidate in candidates:
        print(f"- {candidate.get('candidate_id')} [{candidate.get('status')}] ready={candidate.get('ready')} {candidate.get('created_at')}")
    return 0


def candidate_show(*, target: Path, candidate_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    candidate, error = _resolve_candidate(target, candidate_id)
    if candidate is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    if json_output:
        print(json.dumps(candidate, indent=2, sort_keys=True))
        return 0
    print(f"release candidate: {candidate.get('candidate_id')}")
    print(f"status: {candidate.get('status')}")
    print(f"ready: {candidate.get('ready')}")
    print(f"path: {candidate.get('path')}")
    print(f"blockers: {len(candidate.get('blockers') or [])}")
    for blocker in candidate.get("blockers") or []:
        print(f"- {blocker}")
    print(f"warnings: {len(candidate.get('warnings') or [])}")
    return 0


def candidate_archive(*, target: Path, candidate_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    candidate, error = _resolve_candidate(target, candidate_id)
    if candidate is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    source = Path(str(candidate.get("path") or ""))
    if not source.is_dir() or source.parent == _release_candidates_archive_root(target):
        print(f"error: release candidate cannot be archived: {candidate.get('candidate_id')}", file=sys.stderr)
        return 2
    archive_root = _release_candidates_archive_root(target)
    archive_root.mkdir(parents=True, exist_ok=True)
    destination = archive_root / source.name
    if destination.exists():
        print(f"error: archived release candidate already exists: {candidate.get('candidate_id')}", file=sys.stderr)
        return 2
    shutil.move(str(source), str(destination))
    payload = {
        "target": str(target),
        "candidate_id": candidate.get("candidate_id"),
        "archived_at": _now().isoformat(),
        "archive_path": str(destination),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"archived release candidate: {payload['candidate_id']}")
    print(f"archive_path: {payload['archive_path']}")
    return 0


def _candidate_bundle_files(candidate: dict[str, Any]) -> list[Path]:
    path = candidate.get("path")
    if not isinstance(path, str) or not path:
        return []
    root = Path(path)
    names = candidate.get("bundle_files") if isinstance(candidate.get("bundle_files"), list) else []
    return [root / str(name) for name in names if str(name)]


def _candidate_privacy_issues(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for path in _candidate_bundle_files(candidate):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if RELEASE_PRIVATE_VALUE_RE.search(text):
            issues.append({"status": WARN, "name": "candidate_privacy_secret_like_value", "detail": path.name})
        if RELEASE_PRIVATE_PATH_RE.search(text):
            issues.append({"status": WARN, "name": "candidate_privacy_private_path", "detail": path.name})
    return issues


def _candidate_reference_issues(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for key, value in (
        ("release_readiness", (candidate.get("release_readiness") or {}).get("path") if isinstance(candidate.get("release_readiness"), dict) else None),
        ("work_closeout", (candidate.get("work_closeout") or {}).get("path") if isinstance(candidate.get("work_closeout"), dict) else None),
        ("verification", (candidate.get("verification") or {}).get("path") if isinstance(candidate.get("verification"), dict) else None),
    ):
        if not value:
            issues.append({"status": WARN, "name": f"missing_{key}_evidence", "detail": "not captured in candidate"})
        elif not Path(str(value)).exists():
            issues.append({"status": WARN, "name": f"missing_{key}_receipt", "detail": str(value)})
    return issues


def _candidate_docs_changed_after_build(candidate: dict[str, Any]) -> list[str]:
    path = candidate.get("path")
    if not isinstance(path, str) or not path:
        return []
    evidence_path = Path(path) / "EVIDENCE.json"
    try:
        evidence_mtime = evidence_path.stat().st_mtime
    except OSError:
        return []
    target = Path(str(candidate.get("target") or "."))
    changed: list[str] = []
    for item in ("README.md", "CHANGELOG.md", "ROADMAP.md"):
        repo_file = target / item
        if repo_file.exists() and repo_file.stat().st_mtime > evidence_mtime:
            changed.append(item)
    return changed


def _candidate_audit_payload(target: Path, candidate: dict[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    created = work_cmd._parse_iso_datetime(candidate.get("created_at"))
    if created is not None:
        age_hours = (_now() - created).total_seconds() / 3600
        if age_hours > RELEASE_CANDIDATE_STALE_HOURS:
            issues.append({"status": WARN, "name": "candidate_stale", "detail": f"{age_hours:.1f}h"})
    git = candidate.get("git") if isinstance(candidate.get("git"), dict) else {}
    current_head = _git_value(target, "rev-parse", "HEAD")
    if git.get("head") and current_head and git.get("head") != current_head:
        issues.append({"status": WARN, "name": "candidate_head_changed", "detail": "current HEAD differs from candidate HEAD"})
    issues.extend(_candidate_reference_issues(candidate))
    docs_changed = _candidate_docs_changed_after_build(candidate)
    if docs_changed:
        issues.append({"status": WARN, "name": "candidate_docs_changed", "detail": ", ".join(docs_changed)})
    current_contract = _command_contract_snapshot(target)
    candidate_contract = candidate.get("command_contract") if isinstance(candidate.get("command_contract"), dict) else {}
    if not candidate_contract.get("fingerprint"):
        issues.append({"status": WARN, "name": "candidate_missing_command_contract", "detail": "candidate has no command contract fingerprint"})
    elif candidate_contract.get("fingerprint") != current_contract.get("fingerprint"):
        issues.append({"status": WARN, "name": "candidate_command_contract_changed", "detail": "current CLI/docs command contract differs from candidate"})
    issues.extend(_candidate_privacy_issues(candidate))
    return {
        "target": str(target),
        "candidate_id": candidate.get("candidate_id"),
        "candidate_path": candidate.get("path"),
        "status": "current" if not issues else "needs-review",
        "issue_count": len(issues),
        "issues": issues,
        "command_contract": {
            "candidate": candidate_contract or None,
            "current": current_contract,
        },
        "suggested_next_commands": [
            f"brigade release candidate compare {candidate.get('candidate_id')}",
            f"brigade release candidate closeout {candidate.get('candidate_id')} --status reviewed",
            f"brigade release candidate import-issues {candidate.get('candidate_id')}",
        ],
    }


def candidate_audit(*, target: Path, candidate_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    candidate, error = _resolve_candidate(target, candidate_id)
    if candidate is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    payload = _candidate_audit_payload(target, candidate)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["issue_count"] == 0 else 1
    print(f"release candidate audit: {candidate.get('candidate_id')}")
    print(f"status: {payload['status']}")
    print(f"issues: {payload['issue_count']}")
    for issue in payload["issues"]:
        print(f"[{issue['status']}] {issue['name']}: {issue['detail']}")
    return 0 if payload["issue_count"] == 0 else 1


def candidate_import_issues(*, target: Path, candidate_id: str = "latest", dry_run: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    candidate, error = _resolve_candidate(target, candidate_id)
    if candidate is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    audit = _candidate_audit_payload(target, candidate)
    records = []
    for issue in audit["issues"]:
        name = str(issue.get("name") or "candidate_issue")
        records.append(
            {
                "text": f"Review release candidate {candidate.get('candidate_id')}: {name}",
                "kind": "task",
                "source": "release-candidate",
                "priority": "high" if "privacy" in name or "missing" in name else "normal",
                "metadata": {
                    "candidate_id": candidate.get("candidate_id"),
                    "candidate_path": candidate.get("path"),
                    "issue_name": name,
                    "detail": issue.get("detail"),
                    "source_item_key": f"release-candidate:{candidate.get('candidate_id')}:{name}",
                    "source_fingerprint": work_cmd._stable_hash({"candidate": candidate.get("candidate_id"), "issue": issue}),
                },
            }
        )
    imported, skipped, skipped_dismissed = work_cmd._append_import_records(target, records, dry_run=dry_run)
    payload = {
        "target": str(target),
        "candidate_id": candidate.get("candidate_id"),
        "dry_run": dry_run,
        "issues": len(records),
        "imported": len(imported),
        "skipped_duplicates": len(skipped),
        "skipped_dismissed": len(skipped_dismissed),
        "imports": imported,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"release candidate issue imports: {candidate.get('candidate_id')}")
    print(f"dry_run: {dry_run}")
    print(f"issues: {payload['issues']}")
    print(f"imported: {payload['imported']}")
    print(f"skipped_duplicates: {payload['skipped_duplicates']}")
    print(f"skipped_dismissed: {payload['skipped_dismissed']}")
    return 0


def _receipt_newer_than_candidate(receipt: dict[str, Any] | None, candidate_created: datetime | None) -> bool:
    if receipt is None or candidate_created is None:
        return False
    stamp = work_cmd._parse_iso_datetime(receipt.get("completed_at") or receipt.get("created_at") or receipt.get("started_at"))
    return bool(stamp and stamp > candidate_created)


def candidate_compare(*, target: Path, candidate_id: str = "latest", json_output: bool = False) -> int:
    from . import center_cmd

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    candidate, error = _resolve_candidate(target, candidate_id)
    if candidate is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    candidate_created = work_cmd._parse_iso_datetime(candidate.get("created_at"))
    current_git = _git_state(target)
    candidate_git = candidate.get("git") if isinstance(candidate.get("git"), dict) else {}
    latest_release = _latest_release_receipt(target)
    latest_verify = work_cmd._latest_verify_receipt(target)
    review_health = work_cmd._review_health(target)
    latest_review = review_health.get("latest_run") if isinstance(review_health.get("latest_run"), dict) else None
    latest_sweep = work_cmd._scanner_sweep_health(target).get("latest")
    latest_security = security_cmd.health(target).get("evidence")
    changed_docs_after_candidate = []
    evidence_path = Path(str(candidate.get("path") or "")) / "EVIDENCE.json"
    evidence_mtime = evidence_path.stat().st_mtime if evidence_path.is_file() else None
    for path in ("README.md", "CHANGELOG.md", "ROADMAP.md"):
        repo_file = target / path
        if evidence_mtime is not None and repo_file.exists() and repo_file.stat().st_mtime > evidence_mtime:
            changed_docs_after_candidate.append(path)
    issues: list[dict[str, Any]] = []
    if candidate_git.get("head") and current_git.get("head") and candidate_git.get("head") != current_git.get("head"):
        issues.append({"status": WARN, "name": "candidate_head_changed", "detail": "current HEAD differs from candidate HEAD"})
    if _receipt_newer_than_candidate(latest_release, candidate_created):
        issues.append({"status": WARN, "name": "newer_release_readiness", "detail": str(latest_release.get("run_id"))})
    if _receipt_newer_than_candidate(latest_verify, candidate_created):
        issues.append({"status": WARN, "name": "newer_verification", "detail": str(latest_verify.get("run_id"))})
    if _receipt_newer_than_candidate(latest_review, candidate_created):
        issues.append({"status": WARN, "name": "newer_review_run", "detail": str(latest_review.get("run_id"))})
    if _receipt_newer_than_candidate(latest_sweep, candidate_created):
        issues.append({"status": WARN, "name": "newer_scanner_sweep", "detail": str(latest_sweep.get("sweep_id"))})
    security_generated = work_cmd._parse_iso_datetime((latest_security or {}).get("generated_at") if isinstance(latest_security, dict) else None)
    if candidate_created and security_generated and security_generated > candidate_created:
        issues.append({"status": WARN, "name": "newer_security_report", "detail": str((latest_security or {}).get("path"))})
    for key, value in (
        ("release_readiness", (candidate.get("release_readiness") or {}).get("path") if isinstance(candidate.get("release_readiness"), dict) else None),
        ("work_closeout", (candidate.get("work_closeout") or {}).get("path") if isinstance(candidate.get("work_closeout"), dict) else None),
        ("verification", (candidate.get("verification") or {}).get("path") if isinstance(candidate.get("verification"), dict) else None),
    ):
        if value and not Path(str(value)).exists():
            issues.append({"status": WARN, "name": f"missing_{key}_receipt", "detail": str(value)})
    if changed_docs_after_candidate:
        issues.append({"status": WARN, "name": "docs_changed_after_candidate", "detail": ", ".join(changed_docs_after_candidate)})
    operator_report = candidate.get("operator_report") if isinstance(candidate.get("operator_report"), dict) else {}
    candidate_report = operator_report.get("latest") if isinstance(operator_report.get("latest"), dict) else None
    current_report = center_cmd.latest_report(target)
    if isinstance(candidate_report, dict) and isinstance(current_report, dict):
        if candidate_report.get("report_id") != current_report.get("report_id"):
            issues.append({"status": WARN, "name": "newer_operator_report", "detail": str(current_report.get("report_id"))})
    elif current_report is not None and candidate_created and _receipt_newer_than_candidate(current_report, candidate_created):
        issues.append({"status": WARN, "name": "newer_operator_report", "detail": str(current_report.get("report_id"))})
    report_health = center_cmd.report_health(target)
    top_report_issue = report_health.get("top_issue") if isinstance(report_health.get("top_issue"), dict) else None
    if (
        top_report_issue
        and top_report_issue.get("name") != "operator_report_newer_activity"
        and (current_report is not None or candidate_report is not None)
    ):
        issues.append({"status": WARN, "name": "operator_report_health", "detail": str(top_report_issue.get("detail"))})
    candidate_phase = candidate.get("phase_ledger") if isinstance(candidate.get("phase_ledger"), dict) else {}
    current_phase = phases_cmd.health(target)
    if int(current_phase.get("issue_count") or 0) != int(candidate_phase.get("issue_count") or 0):
        issues.append({"status": WARN, "name": "phase_ledger_issue_count_changed", "detail": f"{candidate_phase.get('issue_count')} -> {current_phase.get('issue_count')}"})
    candidate_closeout = candidate_phase.get("latest_closeout") if isinstance(candidate_phase.get("latest_closeout"), dict) else None
    current_closeout = current_phase.get("latest_closeout") if isinstance(current_phase.get("latest_closeout"), dict) else None
    if isinstance(current_closeout, dict) and (not isinstance(candidate_closeout, dict) or candidate_closeout.get("closeout_id") != current_closeout.get("closeout_id")):
        issues.append({"status": WARN, "name": "newer_phase_closeout", "detail": str(current_closeout.get("closeout_id"))})
    candidate_report = candidate_phase.get("latest_report") if isinstance(candidate_phase.get("latest_report"), dict) else None
    current_phase_report = current_phase.get("latest_report") if isinstance(current_phase.get("latest_report"), dict) else None
    if isinstance(current_phase_report, dict) and (not isinstance(candidate_report, dict) or candidate_report.get("report_id") != current_phase_report.get("report_id")):
        issues.append({"status": WARN, "name": "newer_phase_report", "detail": str(current_phase_report.get("report_id"))})
    candidate_session = candidate_phase.get("latest_session") if isinstance(candidate_phase.get("latest_session"), dict) else None
    current_session = current_phase.get("latest_session") if isinstance(current_phase.get("latest_session"), dict) else None
    if isinstance(current_session, dict) and (not isinstance(candidate_session, dict) or candidate_session.get("session_id") != current_session.get("session_id") or candidate_session.get("status") != current_session.get("status")):
        issues.append({"status": WARN, "name": "newer_phase_session", "detail": str(current_session.get("session_id"))})
    candidate_session_report = candidate_phase.get("latest_session_report") if isinstance(candidate_phase.get("latest_session_report"), dict) else None
    current_session_report = current_phase.get("latest_session_report") if isinstance(current_phase.get("latest_session_report"), dict) else None
    if isinstance(current_session_report, dict) and (not isinstance(candidate_session_report, dict) or candidate_session_report.get("report_id") != current_session_report.get("report_id")):
        issues.append({"status": WARN, "name": "newer_phase_session_report", "detail": str(current_session_report.get("report_id"))})
    candidate_session_checkpoint = candidate_phase.get("latest_session_checkpoint") if isinstance(candidate_phase.get("latest_session_checkpoint"), dict) else None
    current_session_checkpoint = current_phase.get("latest_session_checkpoint") if isinstance(current_phase.get("latest_session_checkpoint"), dict) else None
    if isinstance(candidate_session_checkpoint, dict) or isinstance(current_session_checkpoint, dict):
        candidate_checkpoint_key = (
            candidate_session_checkpoint.get("checkpoint_id"),
            candidate_session_checkpoint.get("status"),
            candidate_session_checkpoint.get("suggested_next_command"),
        ) if isinstance(candidate_session_checkpoint, dict) else None
        current_checkpoint_key = (
            current_session_checkpoint.get("checkpoint_id"),
            current_session_checkpoint.get("status"),
            current_session_checkpoint.get("suggested_next_command"),
        ) if isinstance(current_session_checkpoint, dict) else None
        if candidate_checkpoint_key != current_checkpoint_key:
            detail = str((current_session_checkpoint or candidate_session_checkpoint or {}).get("checkpoint_id") or "missing")
            issues.append({"status": WARN, "name": "phase_session_checkpoint_changed", "detail": detail})
    candidate_checkpoint_compare = candidate_phase.get("latest_session_checkpoint_compare") if isinstance(candidate_phase.get("latest_session_checkpoint_compare"), dict) else None
    current_checkpoint_compare = current_phase.get("latest_session_checkpoint_compare") if isinstance(current_phase.get("latest_session_checkpoint_compare"), dict) else None
    if isinstance(candidate_checkpoint_compare, dict) or isinstance(current_checkpoint_compare, dict):
        candidate_compare_key = (
            candidate_checkpoint_compare.get("issue_count"),
            (candidate_checkpoint_compare.get("top_issue") or {}).get("name") if isinstance(candidate_checkpoint_compare, dict) and isinstance(candidate_checkpoint_compare.get("top_issue"), dict) else None,
        ) if isinstance(candidate_checkpoint_compare, dict) else None
        current_compare_key = (
            current_checkpoint_compare.get("issue_count"),
            (current_checkpoint_compare.get("top_issue") or {}).get("name") if isinstance(current_checkpoint_compare, dict) and isinstance(current_checkpoint_compare.get("top_issue"), dict) else None,
        ) if isinstance(current_checkpoint_compare, dict) else None
        if candidate_compare_key != current_compare_key:
            issues.append({"status": WARN, "name": "phase_session_checkpoint_compare_changed", "detail": f"{candidate_compare_key} -> {current_compare_key}"})
    candidate_session_gate = candidate_phase.get("latest_session_gate") if isinstance(candidate_phase.get("latest_session_gate"), dict) else None
    current_session_gate = current_phase.get("latest_session_gate") if isinstance(current_phase.get("latest_session_gate"), dict) else None
    if isinstance(candidate_session_gate, dict) or isinstance(current_session_gate, dict):
        candidate_gate_key = (
            candidate_session_gate.get("safe_to_claim_complete"),
            candidate_session_gate.get("blocker_count"),
            (candidate_session_gate.get("top_blocker") or {}).get("name") if isinstance(candidate_session_gate, dict) and isinstance(candidate_session_gate.get("top_blocker"), dict) else None,
        ) if isinstance(candidate_session_gate, dict) else None
        current_gate_key = (
            current_session_gate.get("safe_to_claim_complete"),
            current_session_gate.get("blocker_count"),
            (current_session_gate.get("top_blocker") or {}).get("name") if isinstance(current_session_gate, dict) and isinstance(current_session_gate.get("top_blocker"), dict) else None,
        ) if isinstance(current_session_gate, dict) else None
        if candidate_gate_key != current_gate_key:
            issues.append({"status": WARN, "name": "phase_session_gate_changed", "detail": f"{candidate_gate_key} -> {current_gate_key}"})
    phase_checks = _phase_release_checks(target)
    issues.extend({"status": check.get("status", WARN), "name": f"release_{check.get('name')}", "detail": check.get("detail")} for check in phase_checks)
    payload = {
        "target": str(target),
        "candidate_id": candidate.get("candidate_id"),
        "candidate_path": candidate.get("path"),
        "candidate_head": candidate_git.get("head"),
        "current_head": current_git.get("head"),
        "changed_docs_after_candidate": changed_docs_after_candidate,
        "issues": issues,
        "issue_count": len(issues),
        "status": "current" if not issues else "stale",
        "suggested_next_commands": [
            "brigade release doctor",
            "brigade release candidate build",
            f"brigade release candidate closeout {candidate.get('candidate_id')} --status superseded",
        ],
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if not issues else 1
    print(f"release candidate compare: {candidate.get('candidate_id')}")
    print(f"status: {payload['status']}")
    print(f"issues: {len(issues)}")
    for issue in issues:
        print(f"[{issue['status']}] {issue['name']}: {issue['detail']}")
    return 0 if not issues else 1


def candidate_closeout(
    *,
    target: Path,
    candidate_id: str = "latest",
    status: str = "reviewed",
    reason: str | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if status not in {"draft", "reviewed", "superseded", "archived"}:
        print("error: --status must be one of draft, reviewed, superseded, archived", file=sys.stderr)
        return 2
    candidate, error = _resolve_candidate(target, candidate_id)
    if candidate is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    created_at = _now().isoformat()
    payload = {
        "target": str(target),
        "candidate_id": candidate.get("candidate_id"),
        "candidate_path": candidate.get("path"),
        "status": status,
        "reason": reason or f"release candidate marked {status}",
        "reviewed_at": created_at,
        "candidate_head": (candidate.get("git") or {}).get("head") if isinstance(candidate.get("git"), dict) else None,
        "ready": candidate.get("ready"),
        "blocker_count": len(candidate.get("blockers") or []),
        "warning_count": len(candidate.get("warnings") or []),
    }
    candidate_path = Path(str(candidate.get("path") or ""))
    if not candidate_path.is_dir():
        print(f"error: release candidate path is missing: {candidate.get('path')}", file=sys.stderr)
        return 2
    closeout_path = candidate_path / "CLOSEOUT.json"
    payload["path"] = str(closeout_path)
    _write_json(closeout_path, payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"release candidate closeout: {candidate.get('candidate_id')}")
    print(f"status: {status}")
    print(f"path: {closeout_path}")
    return 0


def run(*, target: Path, base_ref: str | None = "origin/main", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    started = _now()
    run_id = f"{started.strftime('%Y%m%d-%H%M%S')}-release-{uuid4().hex[:6]}"
    payload = _payload(target, base_ref=base_ref, run_checks=True)
    completed = _now()
    receipt = {
        **payload,
        "run_id": run_id,
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "duration_seconds": (completed - started).total_seconds(),
        "path": str(_release_runs_root(target) / run_id),
    }
    receipt_path = _release_runs_root(target) / run_id / "receipt.json"
    _write_json(receipt_path, receipt)
    _write_release_markdown(receipt_path, receipt)
    if json_output:
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0 if receipt["ready"] else 1
    print(f"release run: {run_id}")
    print(f"status: {receipt['status']}")
    print(f"ready: {receipt['ready']}")
    print(f"blockers: {len(receipt['blockers'])}")
    print(f"warnings: {len(receipt['warnings'])}")
    print(f"receipt: {receipt_path}")
    return 0 if receipt["ready"] else 1


def runs(*, target: Path, limit: int = 20, json_output: bool = False) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    items = _release_receipts(target)[:limit]
    payload = {"target": str(target), "release_runs_root": str(_release_runs_root(target)), "runs": items}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"release runs: {target}")
    print(f"release_runs_root: {payload['release_runs_root']}")
    if not items:
        print("runs: none")
        return 0
    for item in items:
        print(f"- {item.get('run_id')} [{item.get('status')}] blockers={len(item.get('blockers') or [])} {item.get('started_at')}")
    return 0


def show(*, target: Path, run_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    receipt, error = _resolve_release_receipt(target, run_id)
    if receipt is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    if json_output:
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0
    print(f"release run: {receipt.get('run_id')}")
    print(f"status: {receipt.get('status')}")
    print(f"ready: {receipt.get('ready')}")
    print(f"blockers: {len(receipt.get('blockers') or [])}")
    for blocker in receipt.get("blockers") or []:
        print(f"- {blocker}")
    print(f"warnings: {len(receipt.get('warnings') or [])}")
    return 0
