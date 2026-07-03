"""Projection and catalog inspection helpers for the tools command family."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, constants, helpers, issues as issues_mod, paths, safety


def _managed_header(metadata: dict[str, Any]) -> str:
    rendered = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    return f"<!-- {constants.PROJECTION_MARKER} {rendered} -->"


def _managed_yaml_comment(metadata: dict[str, Any]) -> str:
    rendered = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    return f"# {constants.PROJECTION_MARKER} {rendered}"


def _parse_projection_metadata(raw: str) -> dict[str, Any] | None:
    try:
        metadata = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(metadata, dict):
        return None
    return metadata


def _read_projection(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        text = path.read_text()
    except OSError:
        return None, None
    lines = text.splitlines(keepends=True)
    if not lines:
        return None, text
    first = lines[0].strip()
    prefix = f"<!-- {constants.PROJECTION_MARKER} "
    if not first.startswith(prefix) or not first.endswith(" -->"):
        if first != "---":
            return None, text
        yaml_prefix = f"# {constants.PROJECTION_MARKER} "
        for index, line in enumerate(lines[1:], start=1):
            stripped = line.strip()
            if stripped == "---":
                break
            if not stripped.startswith(yaml_prefix):
                continue
            metadata = _parse_projection_metadata(stripped[len(yaml_prefix) :])
            if metadata is None:
                return None, text
            body = "".join(lines[:index] + lines[index + 1 :])
            return metadata, body
        return None, text
    metadata = _parse_projection_metadata(first[len(prefix) : -len(" -->")])
    if metadata is None:
        return None, text
    return metadata, "".join(lines[1:])


def _relative_path(target: Path, path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return str(path.relative_to(target))
    except ValueError:
        return str(path)


def _render_projection_body(tool: dict[str, Any], harness: str, source_text: str, source_ref: str) -> str:
    family = str(tool.get("family") or "")
    tool_id = str(tool.get("id") or "")
    name = str(tool.get("name") or tool_id)
    description = str(tool.get("description") or "")
    if family in {"slash-command", "skill", "superpower"}:
        return source_text if source_text.endswith("\n") else source_text + "\n"
    if family == "script":
        lines = [
            f"# {name}",
            "",
            "Managed Brigade script projection.",
            "",
            f"- tool_id: `{tool_id}`",
            f"- harness: `{harness}`",
            f"- source: `{source_ref}`",
            f"- command: `{tool.get('command') or ''}`",
        ]
        if description:
            lines.extend(["", description])
        lines.extend(["", "## Source", "", "```text", source_text.rstrip(), "```", ""])
        return "\n".join(lines)
    if family == "mcp":
        lines = [
            f"# {name}",
            "",
            "Managed Brigade MCP projection stub.",
            "",
            f"- tool_id: `{tool_id}`",
            f"- harness: `{harness}`",
            f"- source: `{source_ref}`",
            "",
            "This projection documents the catalog entry only. Runtime MCP server configs are managed separately by `brigade mcp` (see `.brigade/mcp.json`); Brigade does not start MCP servers.",
        ]
        if description:
            lines.extend(["", description])
        return "\n".join(lines) + "\n"
    lines = [
        f"# {name}",
        "",
        "Managed Brigade tool projection.",
        "",
        f"- tool_id: `{tool_id}`",
        f"- family: `{family}`",
        f"- harness: `{harness}`",
        f"- source: `{source_ref}`",
    ]
    if description:
        lines.extend(["", description])
    if source_text.strip():
        lines.extend(["", "## Source", "", "```text", source_text.rstrip(), "```"])
    return "\n".join(lines) + "\n"


def _yaml_string(value: str) -> str:
    return json.dumps(" ".join(value.split()))


def _codex_skill_frontmatter(tool: dict[str, Any]) -> str:
    tool_id = str(tool.get("id") or "brigade-tool")
    description = str(tool.get("description") or f"Use this Brigade-managed skill for {tool_id}.")
    return "\n".join(
        [
            "---",
            f"name: {_yaml_string(tool_id)}",
            f"description: {_yaml_string(description)}",
            "---",
            "",
        ]
    )


def _is_codex_skill_projection(tool: dict[str, Any], harness: str, projection_path: Path | None) -> bool:
    if harness != "codex" or projection_path is None:
        return False
    if projection_path.name != "SKILL.md":
        return False
    family = str(tool.get("family") or "")
    return family in {"slash-command", "skill", "superpower"}


def _projection_managed_body(tool: dict[str, Any], harness: str, projection_path: Path | None, body: str) -> str:
    if _is_codex_skill_projection(tool, harness, projection_path) and not body.startswith("---\n"):
        return _codex_skill_frontmatter(tool) + body
    return body


def _render_managed_projection(
    metadata: dict[str, Any],
    tool: dict[str, Any],
    harness: str,
    projection_path: Path | None,
    managed_body: str,
) -> str:
    if _is_codex_skill_projection(tool, harness, projection_path) and managed_body.startswith("---\n"):
        lines = managed_body.splitlines(keepends=True)
        return lines[0] + _managed_yaml_comment(metadata) + "\n" + "".join(lines[1:])
    return _managed_header(metadata) + "\n" + managed_body


def _projection_item(
    target: Path,
    tool: dict[str, Any],
    harness: str,
    *,
    generated_at: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    generated_at = generated_at or datetime.now(timezone.utc)
    projections = tool.get("projections") if isinstance(tool.get("projections"), dict) else {}
    projection_value = projections.get(harness)
    source_path = helpers._as_path(target, tool.get("source_path"))
    projection_path = helpers._as_path(target, projection_value)
    item: dict[str, Any] = {
        "tool_id": tool.get("id"),
        "name": tool.get("name"),
        "family": tool.get("family"),
        "harness": harness,
        "source_path": str(source_path) if source_path is not None else None,
        "projection_path": str(projection_path) if projection_path is not None else None,
        "managed": False,
        "metadata": None,
    }
    if projection_path is None:
        item.update({"status": "missing", "action": "skip", "detail": f"missing projection target for {harness}"})
        return item
    if not helpers._path_within_target(target, projection_path):
        item.update(
            {"status": "invalid", "action": "skip", "detail": f"projection path escapes target: {projection_value}"}
        )
        return item
    if source_path is None or not source_path.is_file():
        item.update({"status": "missing_source", "action": "skip", "detail": f"missing source: {source_path}"})
        return item
    try:
        source_text = source_path.read_text()
    except OSError as exc:
        item.update({"status": "missing_source", "action": "skip", "detail": f"cannot read source: {exc}"})
        return item
    source_fingerprint = helpers._text_hash(source_text)
    body = _render_projection_body(tool, harness, source_text, _relative_path(target, source_path))
    managed_body = _projection_managed_body(tool, harness, projection_path, body)
    projection_fingerprint = helpers._text_hash(managed_body)
    item.update(
        {
            "source_fingerprint": source_fingerprint,
            "expected_fingerprint": projection_fingerprint,
            "expected_projection_fingerprint": projection_fingerprint,
        }
    )
    metadata = {
        "tool_id": tool.get("id"),
        "family": tool.get("family"),
        "harness": harness,
        "source_fingerprint": source_fingerprint,
        "projection_fingerprint": projection_fingerprint,
        "generated_at": generated_at.isoformat(),
    }
    rendered = _render_managed_projection(metadata, tool, harness, projection_path, managed_body)
    item["rendered"] = rendered
    if not projection_path.exists():
        item.update({"status": "missing", "action": "create", "detail": "projection will be created"})
        return item
    existing_metadata, existing_body = _read_projection(projection_path)
    if existing_metadata is None:
        item.update(
            {
                "status": "unmanaged",
                "action": "update" if force else "conflict",
                "detail": "existing projection is not managed by Brigade",
            }
        )
        return item
    item["managed"] = True
    item["metadata"] = existing_metadata
    existing_projection_fingerprint = str(existing_metadata.get("projection_fingerprint") or "")
    actual_projection_fingerprint = helpers._text_hash(existing_body or "")
    item["actual_projection_fingerprint"] = actual_projection_fingerprint
    if existing_projection_fingerprint != actual_projection_fingerprint:
        item.update(
            {
                "status": "conflicted",
                "action": "update" if force else "conflict",
                "detail": "managed projection has local edits",
            }
        )
        return item
    if (
        existing_metadata.get("source_fingerprint") == source_fingerprint
        and existing_projection_fingerprint == projection_fingerprint
    ):
        item.update({"status": "current", "action": "skip", "detail": "projection is current"})
        return item
    item.update({"status": "stale", "action": "update", "detail": "projection will be updated"})
    return item


def _projection_plan_payload(target: Path, tool_id: str | None = None, *, force: bool = False) -> dict[str, Any]:
    target = target.expanduser().resolve()
    tools, errors = config._load_config(target)
    selected: list[dict[str, Any]] = []
    for tool in tools:
        if not tool.get("enabled", True):
            continue
        if tool_id is None or tool.get("id") == tool_id:
            selected.append(tool)
    if tool_id is not None and not selected and not errors:
        errors.append(f"tool not found: {tool_id}")
    generated_at = datetime.now(timezone.utc)
    projections: list[dict[str, Any]] = []
    for tool in selected:
        for harness in tool.get("supported_harnesses", []):
            projections.append(_projection_item(target, tool, harness, generated_at=generated_at, force=force))
    counts: dict[str, int] = {}
    for item in projections:
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {
        "target": str(target),
        "config_path": str(paths.config_path(target)),
        "valid": not errors,
        "errors": errors,
        "tool_id": tool_id,
        "tools": [tool.get("id") for tool in selected],
        "projections": [{key: value for key, value in item.items() if key != "rendered"} for item in projections],
        "counts": counts,
    }


def _inspect_mcp_config(tool: dict[str, Any], path: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    payload, error = helpers._read_json(path)
    if error is not None:
        return None, [issues_mod._tool_issue(tool, "invalid_schema", f"{path}: {error}")]
    if not isinstance(payload, dict) or not isinstance(payload.get("mcpServers"), dict):
        return None, []
    servers = payload["mcpServers"]
    issues: list[dict[str, Any]] = []
    server_ids = sorted(str(key) for key in servers)
    for server_id, server in servers.items():
        if not isinstance(server, dict):
            issues.append(issues_mod._tool_issue(tool, "invalid_mcp_server", f"{server_id} must be an object"))
            continue
        command = server.get("command")
        if not isinstance(command, str) or not command.strip():
            issues.append(issues_mod._tool_issue(tool, "missing_command", f"MCP server {server_id} is missing command"))
        elif safety._high_risk_command(command):
            issues.append(
                issues_mod._tool_issue(tool, "high_risk_command", f"MCP server {server_id} command shape is high risk")
            )
        if "timeout" not in server and "timeout_seconds" not in server:
            issues.append(
                issues_mod._tool_issue(tool, "missing_timeout", f"MCP server {server_id} is missing timeout metadata")
            )
    return {"server_count": len(server_ids), "server_ids": server_ids}, issues


def _inspect_tool(
    target: Path, tool: dict[str, Any], now: datetime | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    now = now or datetime.now(timezone.utc)
    issues: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "id": tool.get("id"),
        "name": tool.get("name"),
        "family": tool.get("family"),
        "enabled": tool.get("enabled", True),
        "description": tool.get("description", ""),
        "supported_harnesses": tool.get("supported_harnesses", []),
        "projection_coverage": {},
        "schema_available": False,
        "manifest_available": False,
        "auth_label": tool.get("auth_label"),
        "tool_count": 1,
        "contract": safety._contract_summary(target, tool),
    }
    unsafe = safety._unsafe_fields(tool.get("raw", {}))
    if unsafe:
        issues.append(
            issues_mod._tool_issue(tool, "unsafe_auth_fields", f"unsafe field names: {', '.join(unsafe[:8])}")
        )
    issues.extend(safety._contract_issues(target, tool))
    source_path = helpers._as_path(target, tool.get("source_path"))
    if source_path is not None:
        summary["source_path"] = str(source_path)
        source_hash = helpers._file_hash(source_path)
        summary["source_fingerprint"] = source_hash or tool.get("fingerprint")
        if not source_path.is_file():
            issues.append(issues_mod._tool_issue(tool, "missing_source", f"missing source: {source_path}"))
    manifest_path = helpers._as_path(target, tool.get("manifest_path"))
    if manifest_path is not None:
        summary["manifest_path"] = str(manifest_path)
        summary["manifest_available"] = manifest_path.is_file()
        if not manifest_path.is_file():
            issues.append(issues_mod._tool_issue(tool, "missing_manifest", f"missing manifest: {manifest_path}"))
    schema_path = helpers._as_path(target, tool.get("schema_path"))
    if schema_path is not None:
        summary["schema_path"] = str(schema_path)
        if not schema_path.is_file():
            issues.append(issues_mod._tool_issue(tool, "missing_schema", f"missing schema: {schema_path}"))
        else:
            schema, error = helpers._read_json(schema_path)
            if error is not None:
                issues.append(issues_mod._tool_issue(tool, "invalid_schema", f"{schema_path}: {error}"))
            else:
                summary["schema_available"] = True
                if isinstance(schema, dict) and isinstance(schema.get("tools"), list):
                    summary["tool_count"] = len(schema["tools"])
    health_path = helpers._as_path(target, tool.get("health_path"))
    if health_path is not None:
        summary["health_path"] = str(health_path)
        if not health_path.is_file():
            issues.append(issues_mod._tool_issue(tool, "missing_health", f"missing health file: {health_path}"))
        else:
            age_hours = (now.timestamp() - health_path.stat().st_mtime) / 3600
            if age_hours > constants.HEALTH_STALE_HOURS:
                issues.append(issues_mod._tool_issue(tool, "stale_health", f"health file is {age_hours:.1f}h old"))
    command = tool.get("command")
    if tool.get("family") in {"script", "custom"} and not command:
        issues.append(issues_mod._tool_issue(tool, "missing_command", "command is required for script/custom tools"))
    if command:
        summary["command"] = command
        if not safety._command_resolves(command):
            issues.append(
                issues_mod._tool_issue(
                    tool, "missing_command", f"command is not resolvable: {helpers._short(str(command))}"
                )
            )
        if safety._high_risk_command(command):
            issues.append(issues_mod._tool_issue(tool, "high_risk_command", "command shape is high risk"))
    for harness in tool.get("supported_harnesses", []):
        projection_item = _projection_item(target, tool, harness)
        status = str(projection_item.get("status") or "missing")
        summary["projection_coverage"][harness] = status
        if projection_item.get("projection_path"):
            summary.setdefault("projection_paths", {})[harness] = projection_item["projection_path"]
        if status == "missing" and projection_item.get("action") == "skip":
            issues.append(
                issues_mod._tool_issue(tool, "parity_gap", f"missing projection for {harness}", harness=harness)
            )
            continue
        if status == "missing_source":
            continue
        if status in {"missing", "stale", "conflicted", "unmanaged"}:
            issues.append(issues_mod._projection_issue(tool, projection_item))
            continue
    if tool.get("family") == "mcp":
        mcp_path = manifest_path or schema_path or source_path
        if mcp_path is not None and mcp_path.is_file():
            mcp_summary, mcp_issues = _inspect_mcp_config(tool, mcp_path)
            if mcp_summary:
                summary["mcp"] = mcp_summary
                summary["tool_count"] = mcp_summary["server_count"]
            issues.extend(mcp_issues)
    return summary, issues


def _find_tool(target: Path, tool_id: str) -> tuple[dict[str, Any] | None, list[str]]:
    tools, errors = config._load_config(target)
    for tool in tools:
        if tool.get("enabled", True) and tool.get("id") == tool_id:
            return tool, errors
    if not errors:
        errors.append(f"tool not found: {tool_id}")
    return None, errors


def _contracts_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    tools, errors = config._load_config(target)
    contracts: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for tool in tools:
        if not tool.get("enabled", True):
            continue
        summary = safety._contract_summary(target, tool)
        tool_issues = safety._contract_issues(target, tool)
        summary["issue_count"] = len(tool_issues)
        summary["issues"] = tool_issues
        contracts.append(summary)
        issues.extend(tool_issues)
    return {
        "target": str(target),
        "config_path": str(paths.config_path(target)),
        "valid": not errors,
        "errors": errors,
        "contracts": contracts,
        "contract_count": len(contracts),
        "issue_count": len(issues),
        "issues": issues,
    }
