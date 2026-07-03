"""Catalog health aggregation for the tools command family."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import calls, checkpoints, config, constants, issues as issues_mod, paths, projections, runtimes, runs, safety


def _catalog_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    tools, errors = config._load_config(target)
    summaries: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for tool in tools:
        if not tool.get("enabled", True):
            continue
        summary, tool_issues = projections._inspect_tool(target, tool, now=now)
        summaries.append(summary)
        issues.extend(tool_issues)
    wants_runtime = paths.runtimes_config_path(target).is_file() or any(
        tool.get("runtime_id") or tool.get("requires_runtime") for tool in tools
    )
    runtime_health = (
        runtimes._runtime_payload(target, run_health=False)
        if wants_runtime
        else {
            "config_path": str(paths.runtimes_config_path(target)),
            "state_path": str(paths.runtime_state_path(target)),
            "counts": {},
            "runtime_count": 0,
            "issue_count": 0,
            "top_issue": None,
            "issues": [],
            "runtimes": [],
        }
    )
    issues.extend(runtime_health["issues"])
    issues.extend(runtimes._tool_runtime_issues(target, tools, runtime_health))
    policy_health = safety._policy_health(target, tools)
    issues.extend(policy_health["issues"])
    call_health = calls._call_health(target)
    issues.extend(call_health["issues"])
    run_health = runs._run_history_health(target)
    issues.extend(run_health["issues"])
    checkpoint_health = checkpoints._checkpoint_health(target)
    issues.extend(checkpoint_health["issues"])
    if errors:
        issues.insert(
            0, {"status": constants.WARN, "name": "tool_config", "issue_type": "config", "detail": "; ".join(errors)}
        )
    raw_issues = list(issues)
    parity = issues_mod._apply_parity_closeout(target, raw_issues)
    issues = parity["issues"]
    return {
        "target": str(target),
        "config_path": str(paths.config_path(target)),
        "valid": not errors,
        "errors": errors,
        "tools": summaries,
        "tool_count": len(summaries),
        "raw_issues": raw_issues,
        "raw_issue_count": len(raw_issues),
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "parity": {
            "latest_closeout": parity["latest_closeout"],
            "quieted_issue_count": len(parity["quieted_issues"]),
            "quieted_issues": parity["quieted_issues"],
            "changed_issue_count": len(parity["changed_issues"]),
            "changed_issues": parity["changed_issues"],
        },
        "call_queue": {
            "calls_path": call_health["calls_path"],
            "counts": call_health["counts"],
            "pending_count": call_health["pending_count"],
            "issue_count": call_health["issue_count"],
            "top_issue": call_health["top_issue"],
        },
        "run_history": {
            "runs_path": run_health["runs_path"],
            "counts": run_health["counts"],
            "run_count": run_health["run_count"],
            "issue_count": run_health["issue_count"],
            "top_issue": run_health["top_issue"],
            "latest": run_health["latest"],
        },
        "checkpoints": {
            "checkpoints_path": checkpoint_health["checkpoints_path"],
            "counts": checkpoint_health["counts"],
            "checkpoint_count": checkpoint_health["checkpoint_count"],
            "issue_count": checkpoint_health["issue_count"],
            "top_issue": checkpoint_health["top_issue"],
            "latest": checkpoint_health["latest"],
        },
        "runtimes": {
            "config_path": runtime_health["config_path"],
            "state_path": runtime_health["state_path"],
            "counts": runtime_health["counts"],
            "runtime_count": runtime_health["runtime_count"],
            "issue_count": runtime_health["issue_count"],
            "top_issue": runtime_health["top_issue"],
        },
        "policy": {
            "policy_path": policy_health["policy_path"],
            "enabled": policy_health["enabled"],
            "valid": policy_health["valid"],
            "issue_count": policy_health["issue_count"],
            "top_issue": policy_health["top_issue"],
        },
    }
