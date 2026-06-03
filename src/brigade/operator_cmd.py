"""Safe local operator bootstrap commands."""
from __future__ import annotations

import json
import shutil
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

from . import __version__, center_cmd, chat_cmd, daily_cmd, dogfood_cmd, handoff_cmd, memory_cmd, notifications_cmd, repos_cmd, security_cmd, tools_cmd, work_cmd

PROFILES = {"local-operator", "internal-dogfood"}


def guide_payload(*, profile: str = "internal-dogfood") -> dict[str, Any]:
    profile = _validate_profile(profile)
    return {
        "profile": profile,
        "purpose": "Use Brigade as an explicit repo-local operator loop before substantial work.",
        "startup_commands": [
            f"brigade operator status --profile {profile} --target .",
            "brigade daily status --target .",
            "brigade daily plan --target .",
        ],
        "safe_run_command": "brigade daily run --target .",
        "onboarding_command": f"brigade operator init --profile {profile} --target .",
        "tool_sync_command": "brigade operator sync-tools --target .",
        "handoff_expectations": [
            "Write a Memory Handoff when Brigade usage, setup, readiness waivers, or repo workflow changes.",
            "Include concrete commands, what changed, what remains manual, and any local-only setup assumption.",
            "Do not copy raw .brigade evidence into committed docs.",
        ],
        "boundaries": [
            "Brigade does not run automatically.",
            "Brigade does not start daemons or install hooks.",
            "Brigade does not send notifications unless a separate hook is explicitly configured.",
            "Brigade does not ingest handoffs into canonical memory from operator init/status.",
            "Brigade does not publish, push, tag, or mutate remotes.",
        ],
    }


def guide(*, profile: str = "internal-dogfood", json_output: bool = False) -> int:
    try:
        payload = guide_payload(profile=profile)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("operator guide")
    print(f"profile: {payload['profile']}")
    print(payload["purpose"])
    print("startup:")
    for command in payload["startup_commands"]:
        print(f"- {command}")
    print(f"safe_run: {payload['safe_run_command']}")
    print(f"onboard: {payload['onboarding_command']}")
    print(f"sync_tools: {payload['tool_sync_command']}")
    print("handoffs:")
    for item in payload["handoff_expectations"]:
        print(f"- {item}")
    print("boundaries:")
    for item in payload["boundaries"]:
        print(f"- {item}")
    return 0


def _steps(target: Path, *, profile: str = "local-operator") -> list[dict[str, Any]]:
    steps = [
        {"id": "daily", "path": daily_cmd._config_path(target), "command": daily_cmd.init, "kwargs": {}},
        {"id": "handoff-sources", "path": handoff_cmd.default_sources_path(target), "command": handoff_cmd.sources_init, "kwargs": {}},
        {"id": "work-backup", "path": work_cmd._backup_config_path(target), "command": work_cmd.backup_init, "kwargs": {"update_gitignore": False}},
        {"id": "work-scanners", "path": work_cmd._scanner_config_path(target), "command": work_cmd.scanners_init, "kwargs": {"update_gitignore": False}},
        {"id": "work-review", "path": work_cmd._review_config_path(target), "command": work_cmd.review_init, "kwargs": {"update_gitignore": False}},
        {"id": "chat-surfaces", "path": chat_cmd._config_path(target), "command": chat_cmd.surfaces_init, "kwargs": {"update_gitignore": False}},
        {"id": "memory-care", "path": memory_cmd.config_path(target), "command": memory_cmd.init, "kwargs": {"update_gitignore": False}},
        {"id": "repo-fleet", "path": repos_cmd.config_path(target), "command": repos_cmd.init, "kwargs": {"update_gitignore": False}},
        {"id": "security", "path": security_cmd.config_path(target), "command": security_cmd.init, "kwargs": {}},
        {"id": "tools", "path": tools_cmd.config_path(target), "command": tools_cmd.init, "kwargs": {"update_gitignore": False}},
    ]
    if profile == "internal-dogfood":
        steps.insert(1, {"id": "dogfood", "path": dogfood_cmd.config_path(target), "command": dogfood_cmd.init, "kwargs": {}})
    return steps


def _validate_profile(profile: str) -> str:
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of: {', '.join(sorted(PROFILES))}")
    return profile


def plan_payload(target: Path, *, profile: str = "local-operator") -> dict[str, Any]:
    target = target.expanduser().resolve()
    profile = _validate_profile(profile)
    steps = []
    for step in _steps(target, profile=profile):
        path = step["path"]
        steps.append(
            {
                "id": step["id"],
                "path": str(path),
                "exists": path.exists(),
                "action": "skip" if path.exists() else "write",
            }
        )
    return {
        "target": str(target),
        "profile": profile,
        "steps": steps,
        "missing_count": sum(1 for step in steps if step["action"] == "write"),
        "boundaries": [
            "Does not start services.",
            "Only the internal-dogfood profile refreshes read-only security evidence.",
            "Does not ingest handoffs.",
            "Does not write canonical memory.",
            "Does not publish, push, tag, or mutate remotes.",
        ],
    }


def plan(*, target: Path, profile: str = "local-operator", json_output: bool = False) -> int:
    try:
        payload = plan_payload(target, profile=profile)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"operator bootstrap plan: {payload['target']}")
    print(f"profile: {payload['profile']}")
    for row in payload["steps"]:
        print(f"[{row['action']}] {row['id']}: {row['path']}")
    return 0


def init(
    *,
    target: Path,
    profile: str = "local-operator",
    force: bool = False,
    dry_run: bool = False,
    waive_public_release: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        profile = _validate_profile(profile)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if dry_run:
        payload = plan_payload(target, profile=profile)
        payload["dry_run"] = True
        payload["waive_public_release"] = waive_public_release
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        print(f"operator bootstrap dry-run: {target}")
        print(f"profile: {payload['profile']}")
        for row in payload["steps"]:
            print(f"[{row['action']}] {row['id']}: {row['path']}")
        return 0

    results: list[dict[str, Any]] = []
    for step in _steps(target, profile=profile):
        path = step["path"]
        if path.exists() and not force:
            results.append({"id": step["id"], "path": str(path), "status": "skipped", "reason": "already exists"})
            continue
        kwargs = dict(step["kwargs"])
        kwargs.update({"target": target, "force": force})
        output = StringIO()
        with redirect_stdout(output):
            rc = step["command"](**kwargs)
        results.append(
            {
                "id": step["id"],
                "path": str(path),
                "status": "written" if rc == 0 else "error",
                "return_code": rc,
                "output": output.getvalue().strip().splitlines(),
            }
        )
    post_actions = _post_init_actions(target, profile=profile, waive_public_release=waive_public_release)
    payload = {
        "target": str(target),
        "profile": profile,
        "results": results,
        "post_actions": post_actions,
        "written_count": sum(1 for row in results if row["status"] == "written"),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if _bootstrap_ok(results, post_actions) else 1
    print(f"operator bootstrap: {target}")
    print(f"profile: {profile}")
    for row in results:
        print(f"[{row['status']}] {row['id']}: {row['path']}")
    for row in post_actions:
        print(f"[{row['status']}] {row['id']}: {row.get('detail') or row.get('path') or ''}")
    return 0 if _bootstrap_ok(results, post_actions) else 1


def _bootstrap_ok(results: list[dict[str, Any]], post_actions: list[dict[str, Any]]) -> bool:
    return all(row.get("return_code", 0) == 0 for row in results if row["status"] != "skipped") and all(row.get("return_code", 0) == 0 for row in post_actions if row.get("status") != "skipped")


def _post_init_actions(target: Path, *, profile: str, waive_public_release: bool) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if profile == "internal-dogfood":
        output = StringIO()
        with redirect_stdout(output):
            rc = security_cmd.scan(target=target, output_dir=target / ".brigade" / "security" / "latest", json_output=False)
        actions.append(
            {
                "id": "security-scan",
                "status": "written" if rc == 0 else "error",
                "return_code": rc,
                "path": str(target / ".brigade" / "security" / "latest"),
                "output": output.getvalue().strip().splitlines(),
            }
        )
    if waive_public_release:
        actions.append(_waive_public_release_readiness(target))
    return actions


def _waive_public_release_readiness(target: Path) -> dict[str, Any]:
    payload = center_cmd._readiness_payload(target)
    finding = next((item for item in payload.get("findings", []) if item.get("name") == "missing_release_readiness"), None)
    if not isinstance(finding, dict):
        return {"id": "public-release-readiness-waiver", "status": "skipped", "reason": "missing_release_readiness not present"}
    output = StringIO()
    with redirect_stdout(output):
        rc = center_cmd.readiness_closeout(
            target=target,
            status="reviewed",
            reason="internal dogfood bootstrap: public release readiness is out of scope for local production use",
            waive_finding_ids=[str(finding["finding_id"])],
            json_output=False,
        )
    # readiness_closeout returns 1 when unrelated blockers remain, even though
    # the requested waiver was written. Keep bootstrap success tied to the write.
    return {
        "id": "public-release-readiness-waiver",
        "status": "written" if rc in {0, 1} else "error",
        "return_code": 0 if rc in {0, 1} else rc,
        "readiness_return_code": rc,
        "finding_id": finding.get("finding_id"),
        "output": output.getvalue().strip().splitlines(),
    }


def status_payload(target: Path, *, profile: str = "internal-dogfood") -> dict[str, Any]:
    target = target.expanduser().resolve()
    profile = _validate_profile(profile)
    config_rows = []
    for step in _steps(target, profile=profile):
        path = step["path"]
        config_rows.append(
            {
                "id": step["id"],
                "path": str(path),
                "exists": path.exists(),
                "gitignored": dogfood_cmd._check_git_ignored(target, path),
            }
        )
    codex_path = shutil.which("codex")
    brigade_path = shutil.which("brigade")
    daily_health = daily_cmd.health(target)
    security_health = security_cmd.health(target)
    readiness = center_cmd._readiness_payload(target)
    notification_health = notifications_cmd.health(target)
    dogfood_ready = dogfood_cmd.config_path(target).exists() and codex_path is not None
    issues = []
    if not dogfood_ready:
        issues.append({"status": "warn", "name": "dogfood_not_ready", "detail": "dogfood config or codex binary missing"})
    if security_health.get("issue_count"):
        issues.append({"status": "warn", "name": "security_health", "detail": str((security_health.get("top_issue") or {}).get("detail") or "security health issue")})
    if readiness.get("blocker_count"):
        issues.append({"status": "warn", "name": "operator_readiness_blocked", "detail": str((readiness.get("blockers") or [{}])[0].get("safe_summary") or "readiness blocker")})
    return {
        "target": str(target),
        "profile": profile,
        "brigade": {"version": __version__, "path": brigade_path},
        "machine": {
            "codex_path": codex_path,
            "agent_notify_installed": notification_health.get("installed"),
            "agent_notify_configured": notification_health.get("configured"),
            "notification_config_path": notification_health.get("config_path"),
        },
        "repo": {
            "configs": config_rows,
            "missing_config_count": sum(1 for row in config_rows if not row["exists"]),
            "not_gitignored_count": sum(1 for row in config_rows if row["exists"] and row["gitignored"] == "no"),
        },
        "dogfood": {"ready": dogfood_ready, "config_path": str(dogfood_cmd.config_path(target))},
        "daily": {
            "issue_count": daily_health.get("issue_count"),
            "top_issue": daily_health.get("top_issue"),
            "latest_plan": daily_health.get("latest_plan"),
            "latest_run": daily_health.get("latest_run"),
        },
        "security": {
            "issue_count": security_health.get("issue_count"),
            "top_issue": security_health.get("top_issue"),
            "evidence": security_health.get("evidence"),
        },
        "readiness": {
            "status": readiness.get("status"),
            "blocker_count": readiness.get("blocker_count"),
            "warning_count": readiness.get("warning_count"),
            "waived_count": readiness.get("waived_count"),
            "top_blocker": (readiness.get("blockers") or [None])[0] if readiness.get("blockers") else None,
        },
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "checks": issues,
    }


def status(*, target: Path, profile: str = "internal-dogfood", json_output: bool = False) -> int:
    try:
        payload = status_payload(target, profile=profile)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["issue_count"] == 0 else 1
    print(f"operator status: {payload['target']}")
    print(f"profile: {payload['profile']}")
    print(f"brigade: {payload['brigade']['version']} ({payload['brigade']['path'] or 'missing'})")
    print(f"codex: {payload['machine']['codex_path'] or 'missing'}")
    print(f"dogfood: {'ready' if payload['dogfood']['ready'] else 'not-ready'}")
    print(f"repo_configs_missing: {payload['repo']['missing_config_count']}")
    print(f"repo_configs_not_gitignored: {payload['repo']['not_gitignored_count']}")
    print(f"security_issues: {payload['security']['issue_count']}")
    print(f"daily_issues: {payload['daily']['issue_count']}")
    print(f"readiness: {payload['readiness']['status']} blockers={payload['readiness']['blocker_count']} warnings={payload['readiness']['warning_count']}")
    top = payload.get("top_issue")
    if isinstance(top, dict):
        print(f"top_issue: {top.get('name')} {top.get('detail')}")
    return 0 if payload["issue_count"] == 0 else 1


def doctor_payload(target: Path, *, profile: str = "internal-dogfood") -> dict[str, Any]:
    status = status_payload(target, profile=profile)
    target_path = Path(str(status["target"]))
    daily_status = daily_cmd.status_payload(target_path)
    tool_health = tools_cmd.health(target_path)
    blockers: list[dict[str, Any]] = []
    for item in status.get("checks") or []:
        if isinstance(item, dict):
            blockers.append(item)
    if int(tool_health.get("issue_count") or 0) > 0:
        top = tool_health.get("top_issue") if isinstance(tool_health.get("top_issue"), dict) else {}
        blockers.append(
            {
                "status": "warn",
                "name": "tool_projection_health",
                "detail": str(top.get("detail") or "portable tool catalog needs sync or review"),
                "suggested_next_command": "brigade operator sync-tools --target .",
            }
        )
    ready = not blockers
    if not ready:
        first = blockers[0]
        next_command = str(first.get("suggested_next_command") or "brigade operator status --profile internal-dogfood --target .")
    else:
        next_command = str(daily_status.get("next_recommended_command") or "brigade daily plan --target .")
    return {
        "target": status["target"],
        "profile": profile,
        "ready": ready,
        "blocking_issue_count": len(blockers),
        "blockers": blockers,
        "next_command": next_command,
        "operator_status": {
            "issue_count": status.get("issue_count"),
            "dogfood_ready": (status.get("dogfood") or {}).get("ready") if isinstance(status.get("dogfood"), dict) else None,
            "missing_config_count": (status.get("repo") or {}).get("missing_config_count") if isinstance(status.get("repo"), dict) else None,
            "not_gitignored_count": (status.get("repo") or {}).get("not_gitignored_count") if isinstance(status.get("repo"), dict) else None,
            "security_issue_count": (status.get("security") or {}).get("issue_count") if isinstance(status.get("security"), dict) else None,
            "daily_issue_count": (status.get("daily") or {}).get("issue_count") if isinstance(status.get("daily"), dict) else None,
        },
        "tool_health": {
            "issue_count": tool_health.get("issue_count"),
            "tool_count": tool_health.get("tool_count"),
            "top_issue": tool_health.get("top_issue"),
        },
        "daily": {
            "issue_count": (daily_status.get("daily_health") or {}).get("issue_count") if isinstance(daily_status.get("daily_health"), dict) else None,
            "selected_action": daily_status.get("selected_action"),
            "next_recommended_command": daily_status.get("next_recommended_command"),
        },
        "local_only_notes": [
            ".brigade/ stores local config, receipts, scans, reports, waivers, and run artifacts.",
            "Brigade does not run automatically, start daemons, install hooks, send notifications, publish, push, tag, or mutate remotes.",
        ],
        "tracked_vs_generated": [
            "Track reviewed cross-harness source docs under tools/.",
            "Generated harness projections under .claude/, .codex/, and .opencode/ are local ignored state.",
            "Run brigade operator sync-tools --target . after changing tracked tool sources.",
        ],
    }


def doctor(*, target: Path, profile: str = "internal-dogfood", json_output: bool = False) -> int:
    try:
        payload = doctor_payload(target, profile=profile)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["ready"] else 1
    print(f"operator doctor: {payload['target']}")
    print(f"profile: {payload['profile']}")
    print(f"ready: {'yes' if payload['ready'] else 'no'}")
    print(f"blocking_issues: {payload['blocking_issue_count']}")
    print(f"next: {payload['next_command']}")
    if payload["blockers"]:
        print("blockers:")
        for item in payload["blockers"]:
            print(f"- {item.get('name')}: {item.get('detail')}")
    print("local_only:")
    for item in payload["local_only_notes"]:
        print(f"- {item}")
    print("tracked_vs_generated:")
    for item in payload["tracked_vs_generated"]:
        print(f"- {item}")
    return 0 if payload["ready"] else 1


def sync_tools(*, target: Path, dry_run: bool = False, force: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    output = StringIO()
    with redirect_stdout(output):
        rc = tools_cmd.apply(target=target, all_tools=True, dry_run=dry_run, force=force, json_output=True)
    try:
        apply_payload = json.loads(output.getvalue() or "{}")
    except json.JSONDecodeError:
        apply_payload = {
            "valid": False,
            "errors": ["tools apply returned invalid JSON"],
            "output": output.getvalue().strip().splitlines(),
        }
        rc = 1
    tool_health = tools_cmd.health(target)
    ok = rc == 0 and (dry_run or int(tool_health.get("issue_count") or 0) == 0)
    payload = {
        "target": str(target),
        "dry_run": dry_run,
        "force": force,
        "apply": apply_payload,
        "tool_health": {
            "valid": tool_health.get("valid"),
            "tool_count": tool_health.get("tool_count"),
            "issue_count": tool_health.get("issue_count"),
            "top_issue": tool_health.get("top_issue"),
            "sync_plan": tool_health.get("sync_plan"),
        },
        "projection_paths": [
            item.get("projection_path")
            for item in (apply_payload.get("applied") or []) + (apply_payload.get("skipped") or [])
            if isinstance(item, dict) and item.get("projection_path")
        ],
        "status": "ok" if ok else "warn",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["status"] == "ok" else 1
    print(f"operator sync-tools: {target}")
    print(f"dry_run: {dry_run}")
    print(f"force: {force}")
    print(f"applied: {apply_payload.get('applied_count', 0)}")
    print(f"skipped: {apply_payload.get('skipped_count', 0)}")
    print(f"conflicts: {apply_payload.get('conflict_count', 0)}")
    print(f"tool_issues: {tool_health.get('issue_count')}")
    for item in apply_payload.get("applied") or []:
        if isinstance(item, dict):
            verb = "would_write" if dry_run else "wrote"
            print(f"- {verb}: {item.get('tool_id')} {item.get('harness')} {item.get('projection_path')}")
    for item in apply_payload.get("conflicts") or []:
        if isinstance(item, dict):
            print(f"- conflict: {item.get('tool_id')} {item.get('harness')} {item.get('detail')}")
    top = tool_health.get("top_issue")
    if isinstance(top, dict):
        print(f"top_issue: {top.get('tool_id')}/{top.get('issue_type')}: {top.get('detail')}")
    return 0 if payload["status"] == "ok" else 1
