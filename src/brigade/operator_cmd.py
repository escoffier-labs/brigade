"""Safe local operator bootstrap commands."""
from __future__ import annotations

import json
import shutil
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

from . import __version__, center_cmd, chat_cmd, daily_cmd, dogfood_cmd, handoff_cmd, memory_cmd, notifications_cmd, repos_cmd, scrub, security_cmd, skills_cmd, tools_cmd, work_cmd
from .install import install_selection
from .selection import KNOWN_HARNESSES, WRITER_INBOXES, Selection, resolve_owner

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
    content_guard_health = scrub.hook_status(target)
    dogfood_ready = dogfood_cmd.config_path(target).exists() and codex_path is not None
    issues = []
    if profile == "internal-dogfood" and not dogfood_ready:
        issues.append({"status": "warn", "name": "dogfood_not_ready", "detail": "dogfood config or codex binary missing"})
    security_top_issue = security_health.get("top_issue") if isinstance(security_health.get("top_issue"), dict) else {}
    security_missing_evidence = security_top_issue.get("name") == "security_evidence" and str(security_top_issue.get("detail") or "") == "missing"
    if security_health.get("issue_count") and (profile == "internal-dogfood" or not security_missing_evidence):
        issues.append({"status": "warn", "name": "security_health", "detail": str((security_health.get("top_issue") or {}).get("detail") or "security health issue")})
    if profile == "internal-dogfood" and readiness.get("blocker_count"):
        issues.append({"status": "warn", "name": "operator_readiness_blocked", "detail": str((readiness.get("blockers") or [{}])[0].get("safe_summary") or "readiness blocker")})
    content_guard_configured = bool(
        content_guard_health.get("available")
        or content_guard_health.get("hooks_path")
        or content_guard_health.get("pre_push_hook_exists")
        or content_guard_health.get("configured_pre_push_hook_exists")
        or content_guard_health.get("git_pre_push_hook_exists")
    )
    for check in content_guard_health.get("checks") or []:
        if isinstance(check, dict) and check.get("status") != "ok":
            name = str(check.get("name") or "content_guard")
            if name == "content_guard_missing" and not content_guard_configured:
                continue
            if name == "content_guard_hook_not_enabled" and not content_guard_configured:
                continue
            issues.append(
                {
                    "status": str(check.get("status") or "warn"),
                    "name": name,
                    "detail": str(check.get("detail") or "content guard needs attention"),
                    "suggested_next_command": (content_guard_health.get("suggested_commands") or ["brigade operator status --profile internal-dogfood --target ."])[0],
                }
            )
    return {
        "target": str(target),
        "profile": profile,
        "brigade": {"version": __version__, "path": brigade_path},
        "machine": {
            "codex_path": codex_path,
            "agent_notify_installed": notification_health.get("installed"),
            "agent_notify_configured": notification_health.get("configured"),
            "notification_config_path": notification_health.get("config_path"),
            "content_guard_installed": content_guard_health.get("available"),
            "content_guard_dir": content_guard_health.get("scanner_dir"),
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
        "content_guard": content_guard_health,
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
    content_guard = payload.get("content_guard") if isinstance(payload.get("content_guard"), dict) else {}
    hook_label = content_guard.get("pre_push_hook_mode") or ("enabled" if content_guard.get("pre_push_hook_enabled") else "not-enabled")
    print(f"content_guard: {'installed' if content_guard.get('available') else 'missing'} hook={hook_label} policy={content_guard.get('policy')}")
    for command in content_guard.get("suggested_commands") or []:
        print(f"content_guard_next: {command}")
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
        "content_guard": status.get("content_guard"),
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
            "Generated harness projections and handoff inboxes under .claude/, .codex/, .opencode/, .hermes/, .openclaw/, .mcp/, and scripts/ are local ignored state.",
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
    content_guard = payload.get("content_guard") if isinstance(payload.get("content_guard"), dict) else {}
    if content_guard:
        hook_label = content_guard.get("pre_push_hook_mode") or ("enabled" if content_guard.get("pre_push_hook_enabled") else "not-enabled")
        print(
            "content_guard: "
            f"{'installed' if content_guard.get('available') else 'missing'} "
            f"hook={hook_label} "
            f"policy={content_guard.get('policy')}"
        )
        for command in content_guard.get("suggested_commands") or []:
            print(f"content_guard_next: {command}")
    print("local_only:")
    for item in payload["local_only_notes"]:
        print(f"- {item}")
    print("tracked_vs_generated:")
    for item in payload["tracked_vs_generated"]:
        print(f"- {item}")
    return 0 if payload["ready"] else 1


def verify_harness_payload(target: Path, *, harness: str) -> dict[str, Any]:
    from .selection import KNOWN_HARNESSES, WRITER_INBOXES

    target = target.expanduser().resolve()
    checks: list[dict[str, Any]] = []
    if harness not in KNOWN_HARNESSES:
        raise ValueError(f"harness must be one of: {', '.join(KNOWN_HARNESSES)}")
    inbox_rel = WRITER_INBOXES.get(harness)
    if inbox_rel is None:
        checks.append(
            {
                "status": "fail",
                "name": "handoff_writer",
                "detail": f"{harness} is not configured as a handoff writer harness",
            }
        )
        return {
            "target": str(target),
            "harness": harness,
            "supported": False,
            "handoff_inbox": None,
            "checks": checks,
            "issue_count": 1,
            "ready": False,
            "next_command": "choose a writer harness: claude, codex, opencode, hermes",
        }

    health = handoff_cmd.inspect(target)
    inbox_health = next((item for item in health.inboxes if item.inbox == inbox_rel), None)
    inbox_path = target / inbox_rel
    if inbox_health is None:
        checks.append({"status": "fail", "name": "handoff_inbox_known", "detail": f"{inbox_rel} was not inspected"})
    elif not inbox_health.exists:
        checks.append({"status": "fail", "name": "handoff_inbox_exists", "detail": f"{inbox_path} does not exist"})
    else:
        checks.append(
            {
                "status": "ok",
                "name": "handoff_inbox_exists",
                "detail": f"{inbox_path} exists with {inbox_health.pending} pending handoff(s)",
            }
        )
        if inbox_health.watched:
            checks.append({"status": "ok", "name": "handoff_source_coverage", "detail": f"{inbox_rel} is watched"})
        else:
            checks.append({"status": "fail", "name": "handoff_source_coverage", "detail": f"{inbox_rel} is not watched by .brigade/handoff-sources.json"})

    if harness == "hermes":
        checks.extend(_hermes_adapter_checks(target, inbox_rel))

    gitignore_probe = inbox_path / ".brigade-ignore-probe"
    gitignored = dogfood_cmd._check_git_ignored(target, gitignore_probe)
    if gitignored == "no":
        checks.append({"status": "fail", "name": "handoff_inbox_gitignored", "detail": f"{inbox_rel} is not ignored by git"})
    elif gitignored in {"yes", "unknown"}:
        checks.append({"status": "ok", "name": "handoff_inbox_gitignored", "detail": f"gitignore status: {gitignored}"})
    else:
        checks.append({"status": "warn", "name": "handoff_inbox_gitignored", "detail": f"gitignore status: {gitignored}"})

    lint_results = [
        result
        for result in health.lint
        if _path_under(result.path, inbox_path)
    ]
    invalid = [result for result in lint_results if not result.valid]
    if invalid:
        checks.append({"status": "fail", "name": "handoff_lint", "detail": f"{len(invalid)} invalid of {len(lint_results)} pending {harness} handoff(s)"})
    elif lint_results:
        checks.append({"status": "ok", "name": "handoff_lint", "detail": f"{len(lint_results)} pending {harness} handoff(s) lint clean"})
    else:
        checks.append({"status": "ok", "name": "handoff_lint", "detail": f"no pending {harness} handoffs"})

    issue_count = sum(1 for item in checks if item.get("status") in {"fail", "warn"})
    hermes_adapter_issues = [
        item
        for item in checks
        if str(item.get("name", "")).startswith("hermes_adapter_")
        and item.get("status") in {"fail", "warn"}
    ]
    if issue_count:
        if harness == "hermes" and hermes_adapter_issues and not (inbox_health and inbox_health.exists):
            next_command = "brigade init --target . --depth workspace --harnesses hermes"
        elif harness == "hermes" and hermes_adapter_issues:
            next_command = "brigade hermes-fragments --out .brigade/hermes"
        elif inbox_health is None or not inbox_path.exists():
            next_command = f"brigade handoff draft --inbox {harness} --target . --title <title> --summary <summary> --content <content>"
        elif not (inbox_health and inbox_health.watched):
            next_command = "brigade handoff sources init --target . --force"
        elif invalid:
            next_command = f"brigade handoff lint {inbox_rel} --target ."
        else:
            next_command = "brigade handoff doctor --target ."
    else:
        next_command = f"brigade handoff list --target . --json"
    return {
        "target": str(target),
        "harness": harness,
        "supported": True,
        "handoff_inbox": {
            "path": str(inbox_path),
            "relative_path": inbox_rel,
            "exists": bool(inbox_health and inbox_health.exists),
            "watched": bool(inbox_health and inbox_health.watched),
            "pending": int(inbox_health.pending) if inbox_health else 0,
            "processed": int(inbox_health.processed) if inbox_health else 0,
            "gitignored": gitignored,
        },
        "checks": checks,
        "issue_count": issue_count,
        "ready": issue_count == 0,
        "next_command": next_command,
        "local_only_notes": [
            "This check only verifies Brigade's repo-local handoff writer wiring.",
            "It does not start Hermes, call a live Hermes API, or ingest handoffs into canonical memory.",
        ],
    }


def _hermes_adapter_checks(target: Path, inbox_rel: str) -> list[dict[str, Any]]:
    from .hermes_adapter import inspect_hermes_adapter

    return [_operator_hermes_result(item) for item in inspect_hermes_adapter(target, inbox_rel)]


def _operator_hermes_result(item: dict[str, Any]) -> dict[str, Any]:
    result_id = item.get("id")
    if result_id == "fragment":
        name = f"hermes_adapter_{item.get('fragment')}"
    else:
        name = {
            "workspace_handoff_inbox": "hermes_adapter_workspace_handoff_inbox",
            "workspace_json": "hermes_adapter_workspace_json",
            "memory_handoff_inbox": "hermes_adapter_memory_handoff_inbox",
            "processed_handoff_inbox": "hermes_adapter_processed_handoff_inbox",
            "memory_handoff_json": "hermes_adapter_memory_handoff_json",
        }.get(str(result_id), f"hermes_adapter_{result_id}")
    return {"status": item.get("status", "warn"), "name": name, "detail": str(item.get("detail", ""))}


def _path_under(path: Path, root: Path) -> bool:
    try:
        path.expanduser().resolve().relative_to(root.expanduser().resolve())
    except ValueError:
        return False
    return True


def verify_harness(*, target: Path, harness: str, json_output: bool = False) -> int:
    try:
        payload = verify_harness_payload(target, harness=harness)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["ready"] else 1
    print(f"operator verify-harness: {payload['target']}")
    print(f"harness: {payload['harness']}")
    print(f"ready: {'yes' if payload['ready'] else 'no'}")
    handoff_inbox = payload.get("handoff_inbox") if isinstance(payload.get("handoff_inbox"), dict) else None
    if handoff_inbox:
        print(f"handoff_inbox: {handoff_inbox.get('relative_path')}")
    for item in payload["checks"]:
        print(f"[{item.get('status')}] {item.get('name')}: {item.get('detail')}")
    print(f"next: {payload['next_command']}")
    return 0 if payload["ready"] else 1


def sync_tools(*, target: Path, dry_run: bool = False, force: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    defaults_output = StringIO()
    with redirect_stdout(defaults_output):
        defaults_rc = tools_cmd.defaults(
            target=target,
            dry_run=dry_run,
            force=force,
            update_gitignore=True,
            json_output=True,
        )
    try:
        defaults_payload = json.loads(defaults_output.getvalue() or "{}")
    except json.JSONDecodeError:
        defaults_payload = {
            "valid": False,
            "errors": ["tools defaults returned invalid JSON"],
            "output": defaults_output.getvalue().strip().splitlines(),
        }
        defaults_rc = 1
    if defaults_rc != 0:
        payload = {
            "target": str(target),
            "dry_run": dry_run,
            "force": force,
            "defaults": defaults_payload,
            "apply": {"applied_count": 0, "skipped_count": 0, "conflict_count": 0},
            "tool_health": {"valid": False, "tool_count": None, "issue_count": None, "top_issue": None, "sync_plan": None},
            "projection_paths": [],
            "status": "warn",
        }
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 1
        print(f"operator sync-tools: {target}")
        print("defaults: failed")
        for error in defaults_payload.get("errors") or []:
            print(f"error: {error}")
        for conflict in defaults_payload.get("conflicts") or []:
            if isinstance(conflict, dict):
                print(f"- conflict: {conflict.get('tool_id')} {conflict.get('detail')}")
        return 1
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
        "defaults": defaults_payload,
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
    print(f"defaults_added: {len(defaults_payload.get('added') or [])}")
    print(f"defaults_updated: {len(defaults_payload.get('updated') or [])}")
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


def _capture_json_call(func, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    output = StringIO()
    with redirect_stdout(output):
        rc = func(**kwargs, json_output=True)
    try:
        payload = json.loads(output.getvalue() or "{}")
    except json.JSONDecodeError:
        payload = {
            "valid": False,
            "errors": [f"{getattr(func, '__name__', 'command')} returned invalid JSON"],
            "output": output.getvalue().strip().splitlines(),
        }
        rc = 1
    return rc, payload


def _capture_text_call(func, **kwargs: Any) -> tuple[int, list[str]]:
    output = StringIO()
    with redirect_stdout(output):
        rc = func(**kwargs)
    return rc, output.getvalue().strip().splitlines()


def _parse_harnesses(value: str | None) -> list[str]:
    if value is None or not value.strip():
        return ["codex"]
    if value.strip() == "none":
        return []
    harnesses = [item.strip() for item in value.split(",") if item.strip()]
    unknown = [item for item in harnesses if item not in KNOWN_HARNESSES]
    if unknown:
        raise ValueError(f"unknown harness: {', '.join(unknown)}")
    return list(dict.fromkeys(harnesses))


def quickstart(
    *,
    target: Path,
    depth: str = "repo",
    harnesses: str | None = "codex",
    owner: str | None = None,
    tool_pack: Path | None = None,
    skill_pack: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if depth not in {"repo", "workspace"}:
        print("error: --depth must be repo or workspace", file=sys.stderr)
        return 2
    try:
        selected_harnesses = _parse_harnesses(harnesses)
        memory_owner = resolve_owner(selected_harnesses, override=owner)
        selection = Selection(depth=depth, harnesses=selected_harnesses, owner=memory_owner, includes=[])
        selection.validate()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    steps: list[dict[str, Any]] = []
    install_rc, install_output = _capture_text_call(
        install_selection,
        target=target,
        selection=selection,
        force=force,
        dry_run=dry_run,
        allow_home=False,
    )
    steps.append({"id": "brigade-init", "status": "ok" if install_rc == 0 else "error", "return_code": install_rc, "output": install_output})
    if install_rc != 0:
        payload = {
            "target": str(target),
            "depth": depth,
            "harnesses": selected_harnesses,
            "owner": memory_owner,
            "dry_run": dry_run,
            "force": force,
            "steps": steps,
            "status": "blocked",
            "next_commands": [f"brigade init --target {target} --depth {depth} --harnesses {','.join(selected_harnesses) or 'none'} --force"],
            "local_only_notes": _quickstart_local_notes(),
        }
        payload["issue_report"] = _quickstart_issue_report(payload)
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 1
        _print_quickstart(payload)
        return 1

    init_rc, init_payload = _capture_json_call(init, target=target, profile="local-operator", force=force, dry_run=dry_run)
    init_status = "planned" if dry_run and init_rc == 0 else "ok" if init_rc == 0 else "error"
    steps.append({"id": "operator-init", "status": init_status, "return_code": init_rc, "payload": init_payload})

    portable_rc, portable_payload = _capture_json_call(
        bootstrap_portable,
        target=target,
        tool_pack=tool_pack,
        skill_pack=skill_pack,
        dry_run=dry_run,
        force=force,
    )
    portable_status = "planned" if dry_run and portable_rc == 0 else "ok" if portable_rc == 0 else "error"
    steps.append({"id": "portable-bootstrap", "status": portable_status, "return_code": portable_rc, "payload": portable_payload})

    if dry_run:
        for harness in selected_harnesses:
            if harness in WRITER_INBOXES:
                steps.append({"id": f"verify-{harness}", "status": "planned", "return_code": 0, "next_command": f"brigade operator verify-harness --harness {harness} --target {target}"})
    else:
        for harness in selected_harnesses:
            if harness not in WRITER_INBOXES:
                steps.append({"id": f"verify-{harness}", "status": "skipped", "reason": "no Brigade handoff writer inbox for this harness"})
                continue
            verify_rc, verify_payload = _capture_json_call(verify_harness, target=target, harness=harness)
            steps.append({"id": f"verify-{harness}", "status": "ok" if verify_rc == 0 else "warn", "return_code": verify_rc, "payload": verify_payload})

    ok = all(step.get("return_code", 0) == 0 for step in steps if step.get("status") not in {"skipped", "planned"})
    if dry_run:
        ok = install_rc == 0 and init_rc == 0 and portable_rc == 0
    payload = {
        "target": str(target),
        "depth": depth,
        "harnesses": selected_harnesses,
        "owner": memory_owner,
        "dry_run": dry_run,
        "force": force,
        "steps": steps,
        "status": "ok" if ok else "warn",
        "next_commands": _quickstart_next_commands(selected_harnesses, dry_run=dry_run),
        "local_only_notes": _quickstart_local_notes(),
    }
    payload["issue_report"] = _quickstart_issue_report(payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if ok else 1
    _print_quickstart(payload)
    return 0 if ok else 1


def _quickstart_next_commands(harnesses: list[str], *, dry_run: bool) -> list[str]:
    if dry_run:
        return ["rerun without --dry-run after reviewing planned writes"]
    commands = [
        "brigade operator doctor --target . --profile local-operator",
        "brigade tools list --target .",
        "brigade skills doctor --target .",
        "brigade security scan --target . --output-dir .brigade/security/latest",
    ]
    commands.extend(f"brigade operator verify-harness --target . --harness {harness}" for harness in harnesses if harness in WRITER_INBOXES)
    return commands


def _quickstart_local_notes() -> list[str]:
    return [
        ".brigade/ stores local config, receipts, scans, reports, waivers, and run artifacts.",
        "Generated harness projections and handoff inboxes are local ignored state.",
        "Brigade does not start daemons, install hooks, publish, push, tag, or mutate remotes from quickstart.",
    ]


def _quickstart_issue_report(payload: dict[str, Any]) -> dict[str, Any]:
    steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
    step_summaries = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        item = {
            "id": step.get("id"),
            "status": step.get("status"),
            "return_code": step.get("return_code"),
        }
        payload_obj = step.get("payload")
        if isinstance(payload_obj, dict):
            item["payload_status"] = payload_obj.get("status") or payload_obj.get("ready")
            top_issue = payload_obj.get("top_issue")
            if isinstance(top_issue, dict):
                item["top_issue"] = {
                    "name": top_issue.get("name"),
                    "detail": top_issue.get("detail"),
                }
        step_summaries.append(item)
    return {
        "brigade_version": __version__,
        "status": payload.get("status"),
        "depth": payload.get("depth"),
        "harnesses": payload.get("harnesses"),
        "owner": payload.get("owner"),
        "dry_run": payload.get("dry_run"),
        "force": payload.get("force"),
        "steps": step_summaries,
        "next_commands": payload.get("next_commands") or [],
        "github_issue_url": "https://github.com/escoffier-labs/brigade/issues/new/choose",
        "privacy_note": "Review before sharing. Do not paste tokens, private hostnames, or unredacted absolute paths.",
    }


def _print_quickstart(payload: dict[str, Any]) -> None:
    print(f"operator quickstart: {payload['target']}")
    print(f"depth: {payload['depth']}")
    print(f"harnesses: {','.join(payload['harnesses']) or 'none'}")
    print(f"owner: {payload['owner']}")
    print(f"dry_run: {payload['dry_run']}")
    for step in payload["steps"]:
        print(f"[{step.get('status')}] {step.get('id')}")
    print(f"status: {payload['status']}")
    print("next:")
    for command in payload["next_commands"]:
        print(f"- {command}")
    print("issues: https://github.com/escoffier-labs/brigade/issues/new/choose")


def bootstrap_portable(
    *,
    target: Path,
    tool_pack: Path | None = None,
    skill_pack: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    steps: list[dict[str, Any]] = []
    if tool_pack is not None:
        if dry_run:
            steps.append({"id": "tools-pack-import", "status": "skipped", "reason": "dry-run", "pack": str(tool_pack)})
        else:
            rc, payload = _capture_json_call(tools_cmd.pack_import, target=target, pack=tool_pack, force=force)
            steps.append({"id": "tools-pack-import", "status": "ok" if rc == 0 else "error", "return_code": rc, "pack": str(tool_pack), "payload": payload})
    if skill_pack is not None:
        if dry_run:
            steps.append({"id": "skills-pack-import", "status": "skipped", "reason": "dry-run", "pack": str(skill_pack)})
        else:
            rc, payload = _capture_json_call(skills_cmd.pack_import, target=target, pack=skill_pack, force=force)
            steps.append({"id": "skills-pack-import", "status": "ok" if rc == 0 else "error", "return_code": rc, "pack": str(skill_pack), "payload": payload})

    sync_rc, sync_payload = _capture_json_call(sync_tools, target=target, dry_run=dry_run, force=force)
    sync_status = "ok" if sync_rc == 0 else "error"
    if dry_run and isinstance(sync_payload.get("defaults"), dict) and sync_payload["defaults"].get("valid"):
        sync_rc = 0
        sync_status = "planned"
    steps.append({"id": "operator-sync-tools", "status": sync_status, "return_code": sync_rc, "payload": sync_payload})
    if not dry_run:
        tools_rc, tools_payload = _capture_json_call(tools_cmd.doctor, target=target)
        steps.append({"id": "tools-doctor", "status": "ok" if tools_rc == 0 else "error", "return_code": tools_rc, "payload": tools_payload})
        skills_rc, skills_payload = _capture_json_call(skills_cmd.doctor, target=target)
        steps.append({"id": "skills-doctor", "status": "ok" if skills_rc == 0 else "error", "return_code": skills_rc, "payload": skills_payload})

    ok = all(step.get("return_code", 0) == 0 for step in steps if step.get("status") != "skipped")
    payload = {
        "target": str(target),
        "dry_run": dry_run,
        "force": force,
        "tool_pack": str(tool_pack) if tool_pack is not None else None,
        "skill_pack": str(skill_pack) if skill_pack is not None else None,
        "steps": steps,
        "status": "ok" if ok else "warn",
        "next_commands": [
            "brigade tools list --target .",
            "brigade skills doctor --target .",
            "brigade security scan --target . --output-dir .brigade/security/latest",
        ],
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if ok else 1
    print(f"operator bootstrap-portable: {target}")
    print(f"dry_run: {dry_run}")
    print(f"force: {force}")
    for step in steps:
        print(f"[{step['status']}] {step['id']}")
    print(f"status: {payload['status']}")
    if ok:
        print("next:")
        for command in payload["next_commands"]:
            print(f"- {command}")
    return 0 if ok else 1
