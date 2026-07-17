"""Fleet harness wiring and observed-use reporting."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .. import __version__, cursor_user_cmd, localio
from ..claude_hooks import classify as claude_classify
from ..claude_hooks import runtime as claude_runtime
from ..config import load_config
from ..receipts_cmd import _receipt_hash
from ..selection import WRITER_INBOXES
from . import constants, fleet

SUPPORTED_HARNESSES = ("claude", "cursor")
ADOPTION_STATES = (
    "unwired",
    "partial",
    "advisory-only",
    "enforced-idle",
    "active",
    "bypassed",
    "stale",
)
ALERT_STATES = ("bypassed", "stale", "unwired")


def _next_command(state: str, harness: str) -> str:
    if state == "unwired":
        return f"brigade operator quickstart --target <repo> --harnesses {harness} --dry-run"
    if harness == "cursor" and state in {"partial", "stale"}:
        return "brigade harness install cursor --scope user --dry-run"
    if state in {"partial", "stale", "advisory-only"}:
        return "brigade doctor --target <repo> --json"
    if state == "bypassed":
        return "brigade work status --target <repo> --json"
    return f"brigade repos adoption --target . --harness {harness} --days 7 --json"


def _unwired_row(entry: constants.RepoEntry, harness: str, detail: str) -> dict[str, Any]:
    return {
        "repo_id": entry.repo_id,
        "repo_label": entry.label,
        "harness": harness,
        "row_key": f"{entry.repo_id}:{harness}",
        "state": "unwired",
        "wiring": {
            "configured": False,
            "guidance": "missing",
            "skill": "missing",
            "hooks": "missing",
            "mcp": "missing",
            "version": "missing",
        },
        "use": _empty_use(),
        "issues": [detail],
        "next_command": _next_command("unwired", harness),
    }


def _empty_use() -> dict[str, Any]:
    return {
        "active_session_count": 0,
        "compliant_session_count": 0,
        "bypassed_session_count": 0,
        "captured_verification_count": 0,
        "failed_or_rejected_captured_count": 0,
        "sessions": [],
    }


def _receipt_paths(target: Path, *, window_start: datetime) -> list[tuple[Path, dict[str, Any]]]:
    root = target / ".brigade" / "work" / "verify-runs"
    if not root.is_dir():
        return []
    receipts: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(root.glob("*/receipt.json")):
        payload = localio.read_json_dict(path)
        if not payload:
            continue
        started = localio.parse_iso_datetime(payload.get("started_at"))
        if started is None or started < window_start:
            continue
        if payload.get("status") not in {"completed", "failed", "rejected"}:
            continue
        receipts.append((path, payload))
    return receipts


def _captured_receipt_paths(target: Path) -> set[str]:
    records = localio.read_jsonl_dicts(target / "memory" / "outcome" / "records.jsonl")
    captured: set[str] = set()
    for record in records:
        if record.get("source") != "verify":
            continue
        value = record.get("evidence_ref")
        if not isinstance(value, str) or not value:
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = target / path
        captured.add(str(path.resolve()))
    return captured


def _miseledger_hashes(target: Path) -> set[str]:
    payload = localio.read_json_dict(target / ".brigade" / "work" / "miseledger-export-cursor.json") or {}
    hashes = payload.get("raw_hashes")
    if not isinstance(hashes, list):
        return set()
    return {item for item in hashes if isinstance(item, str) and item}


def _handoff_since(target: Path, harness: str, started: datetime) -> bool:
    inbox_rel = WRITER_INBOXES.get(harness)
    if inbox_rel is None:
        return False
    inbox = target / inbox_rel
    if not inbox.is_dir():
        return False
    for path in inbox.glob("*.md"):
        if path.name == "TEMPLATE.md":
            continue
        try:
            if path.stat().st_mtime >= started.timestamp():
                return True
        except OSError:
            continue
    return False


def _graph_ok(receipt: dict[str, Any]) -> bool:
    delta = receipt.get("code_graph_delta")
    return bool(isinstance(delta, dict) and delta.get("status") == "ok" and delta.get("ok") is not False)


def _claude_use(target: Path, *, window_start: datetime) -> dict[str, Any]:
    receipts = _receipt_paths(target, window_start=window_start)
    captured_paths = _captured_receipt_paths(target)
    exported_hashes = _miseledger_hashes(target)
    target_label = str(target.resolve())
    sessions: list[dict[str, Any]] = []
    captured_count = 0
    captured_failed_count = 0
    for state in claude_runtime.iter_session_states(
        target,
        modified_since=window_start,
        limit=claude_runtime.MAX_RECENT_SESSION_STATES,
    ):
        if state.get("target") != target_label or state.get("write_observed") is not True:
            continue
        session_id = state.get("session_id")
        started = localio.parse_iso_datetime(state.get("started_at"))
        last_write = localio.parse_iso_datetime(state.get("last_write_at"))
        last_verification_write = localio.parse_iso_datetime(state.get("last_verification_write_at"))
        if not isinstance(session_id, str) or not session_id or started is None or started < window_start:
            continue
        threshold = last_verification_write or last_write or started
        fingerprint = state.get("session_fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            fingerprint = claude_runtime._session_fingerprint(session_id)
        matching: list[tuple[Path, dict[str, Any]]] = []
        for path, receipt in receipts:
            harness_session = receipt.get("harness_session")
            receipt_started = localio.parse_iso_datetime(receipt.get("started_at"))
            if (
                isinstance(harness_session, dict)
                and harness_session.get("harness") == "claude"
                and harness_session.get("fingerprint") == fingerprint
                and receipt_started is not None
                and receipt_started >= threshold
            ):
                matching.append((path, receipt))

        briefed = state.get("briefed") is True
        handoff = _handoff_since(target, "claude", started)
        evidence_rows: list[dict[str, bool]] = []
        for path, receipt in matching:
            normalized_path = str(path.resolve())
            captured = normalized_path in captured_paths
            if captured:
                captured_count += 1
                if receipt.get("status") in {"failed", "rejected"}:
                    captured_failed_count += 1
            try:
                digest, _ = _receipt_hash(receipt, path)
            except OSError:
                digest = ""
            evidence_rows.append(
                {
                    "captured": captured,
                    "graphtrail": _graph_ok(receipt),
                    "miseledger": bool(digest and digest in exported_hashes),
                }
            )
        receipt_compliant = any(all(evidence.values()) for evidence in evidence_rows)
        missing: list[str] = []
        if not briefed:
            missing.append("brief")
        if not matching:
            missing.append("verification")
        elif not any(item["captured"] for item in evidence_rows):
            missing.append("outcome-capture")
        if matching and not any(item["graphtrail"] for item in evidence_rows):
            missing.append("graphtrail")
        if matching and not any(item["miseledger"] for item in evidence_rows):
            missing.append("miseledger")
        if not handoff:
            missing.append("handoff")
        if not receipt_compliant and not missing:
            missing.append("complete-verification-evidence")
        compliant = briefed and handoff and receipt_compliant
        sessions.append(
            {
                "status": "compliant" if compliant else "bypassed",
                "briefed": briefed,
                "verification_receipt_count": len(matching),
                "handoff": handoff,
                "missing": missing,
            }
        )
    compliant_count = sum(1 for item in sessions if item["status"] == "compliant")
    bypassed_count = len(sessions) - compliant_count
    return {
        "active_session_count": len(sessions),
        "compliant_session_count": compliant_count,
        "bypassed_session_count": bypassed_count,
        "captured_verification_count": captured_count,
        "failed_or_rejected_captured_count": captured_failed_count,
        "sessions": sessions,
    }


def _claude_row(entry: constants.RepoEntry, *, window_start: datetime) -> dict[str, Any]:
    target = entry.path
    classification = claude_classify.classify(target)
    if classification is None:
        return _unwired_row(entry, "claude", "Claude is not selected in Brigade config")
    issues = [fleet._safe_text(item, target, entry.repo_id, entry.label) for item in classification.get("issues") or []]
    raw_hook_status = classification.get("hooks")
    hook_status: dict[str, Any] = raw_hook_status if isinstance(raw_hook_status, dict) else {}
    wiring = {
        "configured": True,
        "guidance": "current" if classification.get("instruction_import") else "missing",
        "skill": (
            "current"
            if classification.get("skill_current")
            else "stale"
            if classification.get("skill_present")
            else "missing"
        ),
        "hooks": ("current" if hook_status.get("current") else "stale" if hook_status.get("installed") else "missing"),
        "mcp": "not-required",
        "version": "current" if hook_status.get("current") else "unknown",
    }
    use = _claude_use(target, window_start=window_start)
    classified_state = str(classification.get("state") or "partial")
    if any(value == "stale" for value in wiring.values()):
        state = "stale"
    elif classified_state == "advisory-only":
        state = "advisory-only"
    elif classified_state == "partial":
        state = "partial"
    elif use["active_session_count"] == 0:
        state = "enforced-idle"
    elif use["bypassed_session_count"] > 0:
        state = "bypassed"
    else:
        state = "active"
    return {
        "repo_id": entry.repo_id,
        "repo_label": entry.label,
        "harness": "claude",
        "row_key": f"{entry.repo_id}:claude",
        "state": state,
        "wiring": wiring,
        "use": use,
        "issues": issues,
        "next_command": _next_command(state, "claude"),
    }


def _cursor_wiring(target: Path) -> tuple[dict[str, Any], list[str], bool, bool]:
    root = cursor_user_cmd._cursor_root()
    desired = cursor_user_cmd._desired_files(root)
    missing = False
    stale = False
    issues: list[str] = []
    for path, (expected, executable, surface) in desired.items():
        if not path.exists():
            missing = True
            issues.append(f"missing {surface}")
            continue
        current = cursor_user_cmd._file_matches(path, expected)
        if executable:
            try:
                current = current and bool(path.stat().st_mode & 0o111)
            except OSError:
                current = False
        if not current:
            stale = True
            issues.append(f"stale {surface}")

    hooks_doc, _ = cursor_user_cmd._read_json_object(root / "hooks.json")
    hook_entries: object = None
    if hooks_doc is not None and isinstance(hooks_doc.get("hooks"), dict):
        hook_entries = hooks_doc["hooks"].get("sessionStart")
    hook_current = isinstance(hook_entries, list) and cursor_user_cmd._hook_entry(root) in hook_entries
    if not hook_current:
        missing = missing or not (root / "hooks.json").exists()
        stale = stale or (root / "hooks.json").exists()
        issues.append("missing or stale session hook registration")

    mcp_doc, _ = cursor_user_cmd._read_json_object(root / "mcp.json")
    live_servers = mcp_doc.get("mcpServers") if mcp_doc is not None else None
    expected_servers = cursor_user_cmd._mcp_servers()
    mcp_current = isinstance(live_servers, dict) and all(
        live_servers.get(name) == expected_servers[name] for name in cursor_user_cmd.MANAGED_MCP_NAMES
    )
    if not mcp_current:
        missing_names = [
            name
            for name in cursor_user_cmd.MANAGED_MCP_NAMES
            if not isinstance(live_servers, dict) or name not in live_servers
        ]
        missing = missing or bool(missing_names)
        stale = stale or bool(isinstance(live_servers, dict) and any(name in live_servers for name in expected_servers))
        issues.append("missing or stale MCP projections")

    state = localio.read_json_dict(root / "brigade" / "install-state.json") or {}
    version_current = state.get("package_version") == __version__
    if not version_current:
        if state:
            stale = True
            issues.append("stale Brigade package version")
        else:
            missing = True
            issues.append("missing Brigade ownership state")

    try:
        guidance_text = (target / "AGENTS.md").read_text().casefold()
        guidance_current = "brigade-wired" in guidance_text and "brigade-work" in guidance_text
    except OSError:
        guidance_current = False
    if not guidance_current:
        missing = True
        issues.append("missing loaded Brigade guidance")

    skill_dir = root / "skills" / "brigade-work"
    skill_current = all(
        cursor_user_cmd._file_matches(path, expected)
        for path, (expected, _, _) in desired.items()
        if path.parent == skill_dir
    )
    wiring = {
        "configured": True,
        "guidance": "current" if guidance_current else "missing",
        "skill": "current" if skill_current else "stale" if skill_dir.exists() else "missing",
        "hooks": "advisory" if hook_current else "stale" if (root / "hooks.json").exists() else "missing",
        "mcp": "current" if mcp_current else "stale" if (root / "mcp.json").exists() else "missing",
        "version": "current" if version_current else "stale" if state else "missing",
    }
    return wiring, sorted(set(issues)), missing, stale


def _cursor_row(entry: constants.RepoEntry) -> dict[str, Any]:
    wiring, issues, missing, stale = _cursor_wiring(entry.path)
    state = "stale" if stale else "partial" if missing else "advisory-only"
    return {
        "repo_id": entry.repo_id,
        "repo_label": entry.label,
        "harness": "cursor",
        "row_key": f"{entry.repo_id}:cursor",
        "state": state,
        "wiring": wiring,
        "use": _empty_use(),
        "issues": issues,
        "next_command": _next_command(state, "cursor"),
    }


def _requested_harnesses(harnesses: list[str] | None) -> tuple[list[str] | None, str | None]:
    requested = list(dict.fromkeys(harnesses or SUPPORTED_HARNESSES))
    unsupported = [item for item in requested if item not in SUPPORTED_HARNESSES]
    if unsupported:
        return None, f"supported harness values: {', '.join(SUPPORTED_HARNESSES)}"
    return requested, None


def adoption_payload(
    *, target: Path, harnesses: list[str] | None = None, days: int = 7
) -> tuple[dict[str, Any] | None, str | None]:
    if days <= 0:
        return None, "--days must be a positive integer"
    requested, error = _requested_harnesses(harnesses)
    if requested is None:
        return None, error
    target = target.expanduser().resolve()
    entries, config_errors, config_loaded = fleet._load_config(target)
    now = localio.utc_now()
    window_start = now - timedelta(days=days)
    rows: list[dict[str, Any]] = []
    for entry in entries:
        if not entry.enabled:
            continue
        try:
            config = load_config(entry.path)
            selected = list(config.selection.harnesses) if config is not None else []
        except (OSError, ValueError, json.JSONDecodeError):
            selected = []
        row_harnesses = [item for item in requested if item in selected]
        if not selected:
            row_harnesses = requested
        for harness in row_harnesses:
            if not selected:
                rows.append(_unwired_row(entry, harness, "no usable Brigade harness configuration"))
            elif not entry.path.is_dir():
                rows.append(_unwired_row(entry, harness, "repository is not reachable"))
            elif harness == "claude":
                rows.append(_claude_row(entry, window_start=window_start))
            else:
                rows.append(_cursor_row(entry))

    active_repo_ids = {row["repo_id"] for row in rows if int(row["use"].get("active_session_count") or 0) > 0}
    wired_repo_ids = {row["repo_id"] for row in rows if row["state"] != "unwired"}
    denominators = {
        "active_sessions": sum(int(row["use"].get("active_session_count") or 0) for row in rows),
        "active_repositories": len(active_repo_ids),
        "wired_repositories": len(wired_repo_ids),
        "compliant_sessions": sum(int(row["use"].get("compliant_session_count") or 0) for row in rows),
        "bypassed_sessions": sum(int(row["use"].get("bypassed_session_count") or 0) for row in rows),
    }
    alerts = [
        {"row_key": row["row_key"], "state": row["state"], "next_command": row["next_command"]}
        for row in rows
        if row["state"] in ALERT_STATES
    ]
    monitor_rows = [{"row_key": row["row_key"], "state": row["state"]} for row in rows]
    payload = {
        "schema": {"name": "brigade.repo-fleet-adoption", "version": 1},
        "target_label": "repo-fleet",
        "generated_at": now.isoformat(),
        "window": {"days": days, "started_at": window_start.isoformat()},
        "config_loaded": config_loaded,
        "config_errors": [fleet._safe_text(item, target, "repo-fleet", "repo fleet") for item in config_errors],
        "harnesses": requested,
        "rows": rows,
        "denominators": denominators,
        "monitor": {
            "alert_states": list(ALERT_STATES),
            "alerts": alerts,
            "fingerprint": localio.stable_hash(monitor_rows),
        },
    }
    return payload, None


def adoption_report(
    *, target: Path, harnesses: list[str] | None = None, days: int = 7, json_output: bool = False
) -> int:
    payload, error = adoption_payload(target=target, harnesses=harnesses, days=days)
    if payload is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet adoption: {payload['target_label']} window={days}d")
    for row in payload["rows"]:
        print(
            f"- {row['repo_id']} {row['harness']} state={row['state']} "
            f"sessions={row['use']['active_session_count']} next={row['next_command']}"
        )
    counts = payload["denominators"]
    print(
        "denominators: "
        f"active_sessions={counts['active_sessions']} active_repositories={counts['active_repositories']} "
        f"wired_repositories={counts['wired_repositories']} compliant_sessions={counts['compliant_sessions']} "
        f"bypassed_sessions={counts['bypassed_sessions']}"
    )
    return 0


def adoption_repair(
    *,
    target: Path,
    harnesses: list[str] | None = None,
    days: int = 7,
    state: str | None = None,
    json_output: bool = False,
) -> int:
    if state is not None and state not in ADOPTION_STATES:
        print(f"error: --state must be one of: {', '.join(ADOPTION_STATES)}", file=sys.stderr)
        return 2
    payload, error = adoption_payload(target=target, harnesses=harnesses, days=days)
    if payload is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    actions = [
        {
            "repo_id": row["repo_id"],
            "harness": row["harness"],
            "state": row["state"],
            "command": row["next_command"],
        }
        for row in payload["rows"]
        if row["state"] != "active" and (state is None or row["state"] == state)
    ]
    result = {
        "schema": {"name": "brigade.repo-fleet-adoption-repair", "version": 1},
        "target_label": "repo-fleet",
        "dry_run": True,
        "would_write": False,
        "would_run_commands": False,
        "state_filter": state,
        "window_days": days,
        "actions": actions,
    }
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print("repo fleet adoption repair: dry-run")
    for action in actions:
        print(f"- {action['repo_id']} {action['harness']} [{action['state']}] {action['command']}")
    if not actions:
        print("actions: none")
    return 0


__all__ = (
    "ADOPTION_STATES",
    "SUPPORTED_HARNESSES",
    "adoption_payload",
    "adoption_repair",
    "adoption_report",
)
