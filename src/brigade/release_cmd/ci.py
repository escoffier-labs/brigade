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
    payload["source_fingerprint"] = work_cmd._stable_hash(
        {key: value for key, value in payload.items() if key != "finding_id"}
    )
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
            if (
                major
                and action.lower() in CI_DEPRECATED_ACTION_MAJORS
                and major in CI_DEPRECATED_ACTION_MAJORS[action.lower()]
            ):
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
    findings.sort(
        key=lambda item: (
            str(item.get("path_label") or ""),
            int(item.get("line") or 0),
            str(item.get("finding_id") or ""),
        )
    )
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


def ci_import_issues(
    *, target: Path, summary_path: Path | None = None, dry_run: bool = False, json_output: bool = False
) -> int:
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
    return scrub.available()


def _content_guard_command(
    target: Path, *, policy: str, introduced: bool, base_ref: str | None
) -> tuple[list[str] | None, dict[str, str], str | None]:
    scanner_dir = scrub.scanner_dir()
    env: dict[str, str] = {}
    if not scanner_dir.is_dir():
        return None, env, f"content guard not available at {scanner_dir}"
    policy_ref = Path(policy)
    if os.environ.get("CONTENT_GUARD_DIR") and policy_ref.parent == Path("."):
        policy_name = policy_ref.name if policy_ref.suffix == ".json" else f"{policy_ref.name}.json"
        policy_path = scanner_dir / "policies" / policy_name
    else:
        try:
            policy_path = scrub.policy_path(target, policy)
        except ValueError as exc:
            return None, env, str(exc)
    if not policy_path.is_file():
        return None, env, f"content guard policy not found: {policy_path}"
    module = scrub.scanner_module()
    if module == "content_guard":
        env["PYTHONPATH"] = str(scanner_dir / "src")
    if introduced and base_ref:
        return (
            [
                sys.executable,
                "-m",
                f"{module}.git_scan",
                "--history",
                "--range",
                f"{base_ref}..HEAD",
                "--policy",
                str(policy_path),
            ],
            env,
            None,
        )
    return [sys.executable, "-m", module, "scan", str(target), "--policy", str(policy_path)], env, None


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
