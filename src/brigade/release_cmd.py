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

from . import handoff_cmd, security_cmd, work_cmd

OK = "ok"
WARN = "warn"
FAIL = "fail"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _release_root(target: Path) -> Path:
    return target / ".brigade" / "release"


def _release_runs_root(target: Path) -> Path:
    return _release_root(target) / "runs"


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


def _evidence(target: Path, *, base_ref: str | None) -> dict[str, Any]:
    sweep = work_cmd._scanner_sweep_health(target)
    review = work_cmd._review_health(target)
    handoffs = handoff_cmd.draft_queue_payload(target)
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
        },
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
    payload = _payload(target, base_ref=base_ref, run_checks=True)
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
