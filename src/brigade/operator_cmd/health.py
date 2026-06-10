from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import brigade.operator_cmd as _pkg

from .. import (
    __version__,
    center_cmd,
    daily_cmd,
    dogfood_cmd,
    handoff_cmd,
    localio,
    notifications_cmd,
    scrub,
    security_cmd,
    tools_cmd,
)
from .guide import _steps, _validate_profile


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
                "gitignored": localio.check_git_ignored(target, path),
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
        issues.append(
            {"status": "warn", "name": "dogfood_not_ready", "detail": "dogfood config or codex binary missing"}
        )
    security_top_issue = security_health.get("top_issue") if isinstance(security_health.get("top_issue"), dict) else {}
    security_missing_evidence = (
        security_top_issue.get("name") == "security_evidence"
        and str(security_top_issue.get("detail") or "") == "missing"
    )
    if security_health.get("issue_count") and (profile == "internal-dogfood" or not security_missing_evidence):
        issues.append(
            {
                "status": "warn",
                "name": "security_health",
                "detail": str((security_health.get("top_issue") or {}).get("detail") or "security health issue"),
            }
        )
    if profile == "internal-dogfood" and readiness.get("blocker_count"):
        issues.append(
            {
                "status": "warn",
                "name": "operator_readiness_blocked",
                "detail": str((readiness.get("blockers") or [{}])[0].get("safe_summary") or "readiness blocker"),
            }
        )
    content_guard_configured = bool(
        content_guard_health.get("available")
        or content_guard_health.get("hooks_path")
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
            # The pre-push hook ships inactive by design and activation is the
            # operator's call; a fresh local-operator setup should not be
            # blocked on it. The internal-dogfood profile keeps the strict bar.
            if name == "content_guard_hook_not_enabled" and profile == "local-operator":
                continue
            issues.append(
                {
                    "status": str(check.get("status") or "warn"),
                    "name": name,
                    "detail": str(check.get("detail") or "content guard needs attention"),
                    "suggested_next_command": (
                        content_guard_health.get("suggested_commands")
                        or ["brigade operator status --profile internal-dogfood --target ."]
                    )[0],
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
    hook_label = content_guard.get("pre_push_hook_mode") or (
        "enabled" if content_guard.get("pre_push_hook_enabled") else "not-enabled"
    )
    print(
        f"content_guard: {'installed' if content_guard.get('available') else 'missing'} hook={hook_label} policy={content_guard.get('policy')}"
    )
    for command in content_guard.get("suggested_commands") or []:
        print(f"content_guard_next: {command}")
    print(f"daily_issues: {payload['daily']['issue_count']}")
    print(
        f"readiness: {payload['readiness']['status']} blockers={payload['readiness']['blocker_count']} warnings={payload['readiness']['warning_count']}"
    )
    top = payload.get("top_issue")
    if isinstance(top, dict):
        print(f"top_issue: {top.get('name')} {top.get('detail')}")
    return 0 if payload["issue_count"] == 0 else 1


def doctor_payload(target: Path, *, profile: str = "internal-dogfood") -> dict[str, Any]:
    status = _pkg.status_payload(target, profile=profile)
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
        next_command = str(
            first.get("suggested_next_command") or "brigade operator status --profile internal-dogfood --target ."
        )
    else:
        next_command = _operator_doctor_next_command(profile, daily_status)
    return {
        "target": status["target"],
        "profile": profile,
        "ready": ready,
        "blocking_issue_count": len(blockers),
        "blockers": blockers,
        "next_command": next_command,
        "operator_status": {
            "issue_count": status.get("issue_count"),
            "dogfood_ready": (status.get("dogfood") or {}).get("ready")
            if isinstance(status.get("dogfood"), dict)
            else None,
            "missing_config_count": (status.get("repo") or {}).get("missing_config_count")
            if isinstance(status.get("repo"), dict)
            else None,
            "not_gitignored_count": (status.get("repo") or {}).get("not_gitignored_count")
            if isinstance(status.get("repo"), dict)
            else None,
            "security_issue_count": (status.get("security") or {}).get("issue_count")
            if isinstance(status.get("security"), dict)
            else None,
            "daily_issue_count": (status.get("daily") or {}).get("issue_count")
            if isinstance(status.get("daily"), dict)
            else None,
        },
        "content_guard": status.get("content_guard"),
        "tool_health": {
            "issue_count": tool_health.get("issue_count"),
            "tool_count": tool_health.get("tool_count"),
            "top_issue": tool_health.get("top_issue"),
        },
        "daily": {
            "issue_count": (daily_status.get("daily_health") or {}).get("issue_count")
            if isinstance(daily_status.get("daily_health"), dict)
            else None,
            "selected_action": daily_status.get("selected_action"),
            "next_recommended_command": daily_status.get("next_recommended_command"),
        },
        "local_only_notes": [
            ".brigade/ stores local config, receipts, scans, reports, waivers, and run artifacts.",
            "Brigade does not run automatically, start daemons, activate hooks, send notifications, publish, push, tag, or mutate remotes.",
        ],
        "tracked_vs_generated": [
            "Track reviewed cross-harness source docs under tools/.",
            "Generated harness projections and handoff inboxes under .claude/, .codex/, .opencode/, .antigravity/, .pi/, .cursor/, .aider/, .goose/, .continue/, .copilot/, .qwen/, .kimi/, .adal/, .openhands/, .hermes/, .openclaw/, .mcp/, and scripts/ are local ignored state.",
            "Run brigade operator sync-tools --target . after changing tracked tool sources.",
        ],
    }


def _operator_doctor_next_command(profile: str, daily_status: dict[str, Any]) -> str:
    command = str(daily_status.get("next_recommended_command") or "brigade daily plan --target .")
    selected = daily_status.get("selected_action") if isinstance(daily_status.get("selected_action"), dict) else {}
    if (
        profile == "local-operator"
        and selected.get("source_subsystem") == "center-readiness"
        and selected.get("action_type") == "import-readiness-issues"
        and selected.get("safe_summary") == "release readiness receipt is missing"
    ):
        return "brigade daily plan --target ."
    return command


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
        hook_label = content_guard.get("pre_push_hook_mode") or (
            "enabled" if content_guard.get("pre_push_hook_enabled") else "not-enabled"
        )
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
    from ..selection import KNOWN_HARNESSES, WRITER_INBOXES

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
            "next_command": "choose a writer harness: claude, codex, opencode, antigravity, pi, cursor, aider, goose, continue, copilot, qwen, kimi, adal, openhands, hermes",
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
            checks.append(
                {
                    "status": "fail",
                    "name": "handoff_source_coverage",
                    "detail": f"{inbox_rel} is not watched by .brigade/handoff-sources.json",
                }
            )

    if harness == "hermes":
        checks.extend(_hermes_adapter_checks(target, inbox_rel))

    gitignore_probe = inbox_path / ".brigade-ignore-probe"
    gitignored = localio.check_git_ignored(target, gitignore_probe)
    if gitignored == "no":
        checks.append(
            {"status": "fail", "name": "handoff_inbox_gitignored", "detail": f"{inbox_rel} is not ignored by git"}
        )
    elif gitignored in {"yes", "unknown"}:
        checks.append({"status": "ok", "name": "handoff_inbox_gitignored", "detail": f"gitignore status: {gitignored}"})
    else:
        checks.append(
            {"status": "warn", "name": "handoff_inbox_gitignored", "detail": f"gitignore status: {gitignored}"}
        )

    # The managed .gitignore un-ignores each inbox's TEMPLATE.md so the format
    # travels with the repo. Git cannot re-include a file whose parent dir is
    # excluded by another source (commonly a global gitignore with a bare
    # `.claude/` or `.codex/` entry), and that shadowing is otherwise silent.
    template_path = inbox_path / "TEMPLATE.md"
    if template_path.is_file():
        template_ignored = localio.check_git_ignored(target, template_path)
        if template_ignored == "yes":
            inbox_root = inbox_rel.split("/")[0]
            checks.append(
                {
                    "status": "warn",
                    "name": "handoff_template_shadowed",
                    "detail": (
                        f"{inbox_rel}/TEMPLATE.md is gitignored despite the managed un-ignore rule; "
                        f"an external ignore source (often a global gitignore entry like `{inbox_root}/`) "
                        "is shadowing it, so the template will not travel with the repo"
                    ),
                }
            )
        else:
            checks.append(
                {
                    "status": "ok",
                    "name": "handoff_template_shadowed",
                    "detail": f"{inbox_rel}/TEMPLATE.md is visible to git",
                }
            )

    lint_results = [result for result in health.lint if _path_under(result.path, inbox_path)]
    invalid = [result for result in lint_results if not result.valid]
    if invalid:
        checks.append(
            {
                "status": "fail",
                "name": "handoff_lint",
                "detail": f"{len(invalid)} invalid of {len(lint_results)} pending {harness} handoff(s)",
            }
        )
    elif lint_results:
        checks.append(
            {
                "status": "ok",
                "name": "handoff_lint",
                "detail": f"{len(lint_results)} pending {harness} handoff(s) lint clean",
            }
        )
    else:
        checks.append({"status": "ok", "name": "handoff_lint", "detail": f"no pending {harness} handoffs"})

    # Fails block readiness; warns are advisories (host conditions like a
    # global gitignore shadowing an inbox template) and stay visible without
    # flipping ready to no.
    issue_count = sum(1 for item in checks if item.get("status") == "fail")
    warning_count = sum(1 for item in checks if item.get("status") == "warn")
    hermes_adapter_issues = [
        item
        for item in checks
        if str(item.get("name", "")).startswith("hermes_adapter_") and item.get("status") in {"fail", "warn"}
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
        next_command = "brigade handoff list --target . --json"
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
        "warning_count": warning_count,
        "ready": issue_count == 0,
        "next_command": next_command,
        "local_only_notes": [
            "This check only verifies Brigade's repo-local handoff writer wiring.",
            "It does not start Hermes, call a live Hermes API, or ingest handoffs into canonical memory.",
        ],
    }


def _hermes_adapter_checks(target: Path, inbox_rel: str) -> list[dict[str, Any]]:
    from ..hermes_adapter import inspect_hermes_adapter

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
