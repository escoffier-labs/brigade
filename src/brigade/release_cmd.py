"""Local release readiness receipts."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import context_cmd, handoff_cmd, learn_cmd, memory_cmd, projects_cmd, repos_cmd, roadmap_cmd, security_cmd, tools_cmd, work_cmd

OK = "ok"
WARN = "warn"
FAIL = "fail"
RELEASE_CANDIDATE_STALE_HOURS = 168


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
    from . import center_cmd

    sweep = work_cmd._scanner_sweep_health(target)
    review = work_cmd._review_health(target)
    handoffs = handoff_cmd.draft_queue_payload(target)
    context_health = context_cmd.health(target)
    learning_health = learn_cmd.health(target)
    projects_health = projects_cmd.health(target)
    repo_health = repos_cmd.health(target)
    roadmap_health = roadmap_cmd.health(target)
    tool_health = tools_cmd.health(target)
    memory_health = memory_cmd.health(target)
    backup_health = work_cmd._backup_health(target)
    acceptance = work_cmd._acceptance_payload(target)
    operator_report_health = center_cmd.report_health(target)
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
        "handoff_drafts": {
            "counts": handoffs.get("counts"),
            "issue_count": handoffs.get("issue_count"),
            "top_issue": handoffs.get("top_issue"),
            "latest_ingest_run": handoffs.get("latest_ingest_run"),
            "latest_closeout": _latest_closeout_json(target / ".brigade" / "handoffs" / "closeouts"),
        },
        "backup": {
            "valid": backup_health.get("valid"),
            "issue_count": backup_health.get("issue_count"),
            "top_issue": backup_health.get("top_issue"),
            "latest_closeout": backup_health.get("latest_closeout"),
        },
        "tool_catalog": {
            "valid": tool_health.get("valid"),
            "issue_count": tool_health.get("issue_count"),
            "top_issue": tool_health.get("top_issue"),
            "call_queue": tool_health.get("call_queue"),
            "run_history": tool_health.get("run_history"),
            "checkpoints": tool_health.get("checkpoints"),
        },
        "task_acceptance": {
            "coverage": acceptance.get("coverage"),
            "issue_count": acceptance.get("issue_count"),
            "top_issue": acceptance.get("top_issue"),
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
        },
        "projects": {
            "project_count": projects_health.get("project_count"),
            "issue_count": projects_health.get("issue_count"),
            "top_issue": projects_health.get("top_issue"),
        },
        "learning": {
            "candidate_count": learning_health.get("candidate_count"),
            "issue_count": learning_health.get("issue_count"),
            "top_issue": learning_health.get("top_issue"),
        },
        "repo_fleet": {
            "repo_count": repo_health.get("repo_count"),
            "issue_count": repo_health.get("issue_count"),
            "top_issue": repo_health.get("top_issue"),
        },
        "roadmap": {
            "issue_count": roadmap_health.get("issue_count"),
            "top_issue": roadmap_health.get("top_issue"),
        },
        "operator_report": {
            "issue_count": operator_report_health.get("issue_count"),
            "top_issue": operator_report_health.get("top_issue"),
            "latest": operator_report_health.get("latest"),
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
    sweep = evidence.get("scanner_sweep") if isinstance(evidence.get("scanner_sweep"), dict) else {}
    sweep_review = sweep.get("review") if isinstance(sweep.get("review"), dict) else {}
    if int(sweep_review.get("issue_count") or 0) > 0:
        blockers.append(f"scanner sweep has unresolved issue(s): {sweep_review.get('issue_count')}")
    security = evidence.get("security") if isinstance(evidence.get("security"), dict) else {}
    if int(security.get("issue_count") or 0) > 0:
        blockers.append(f"security has open issue(s): {security.get('issue_count')}")
    handoffs = evidence.get("handoff_drafts") if isinstance(evidence.get("handoff_drafts"), dict) else {}
    if int(handoffs.get("issue_count") or 0) > 0:
        blockers.append(f"handoff draft queue has issue(s): {handoffs.get('issue_count')}")
    operator_report = evidence.get("operator_report") if isinstance(evidence.get("operator_report"), dict) else {}
    if int(operator_report.get("issue_count") or 0) > 0:
        top_report = operator_report.get("top_issue") if isinstance(operator_report.get("top_issue"), dict) else {}
        warnings.append(f"operator report has issue(s): {top_report.get('detail') or operator_report.get('issue_count')}")
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


def _commit_subjects(target: Path, base_ref: str | None) -> list[str]:
    args = ["log", "--format=%s"]
    if base_ref:
        args.append(f"{base_ref}..HEAD")
    else:
        args.extend(["-n", "20"])
    result = _git(target, *args)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


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
            items.append(line.strip()[2:])
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
        "security_closeout": evidence.get("security_closeout"),
        "handoff_drafts": evidence.get("handoff_drafts"),
        "backup": evidence.get("backup"),
        "tool_catalog": evidence.get("tool_catalog"),
        "task_acceptance": evidence.get("task_acceptance"),
        "memory_care": evidence.get("memory_care"),
        "context": evidence.get("context"),
        "projects": evidence.get("projects"),
        "learning": evidence.get("learning"),
        "operator_report": evidence.get("operator_report"),
        "repo_fleet": evidence.get("repo_fleet"),
        "roadmap": evidence.get("roadmap"),
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
        "blockers": readiness.get("blockers") if isinstance(readiness.get("blockers"), list) else [],
        "warnings": readiness.get("warnings") if isinstance(readiness.get("warnings"), list) else [],
        "suggested_next_commands": [
            "brigade release doctor",
            "brigade work verify run",
            "brigade work closeout latest",
            "brigade release candidate build",
        ],
    }


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


def _receipt_newer_than_candidate(receipt: dict[str, Any] | None, candidate_created: datetime | None) -> bool:
    if receipt is None or candidate_created is None:
        return False
    stamp = work_cmd._parse_iso_datetime(receipt.get("completed_at") or receipt.get("created_at") or receipt.get("started_at"))
    return bool(stamp and stamp > candidate_created)


def candidate_compare(*, target: Path, candidate_id: str = "latest", json_output: bool = False) -> int:
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
