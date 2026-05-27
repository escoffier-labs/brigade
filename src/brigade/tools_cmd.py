"""Local portable tool and skill catalog inspection."""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback.
    tomllib = None  # type: ignore[assignment]

from . import dogfood_cmd
from .install import apply_gitignore
from .selection import Selection

OK = "ok"
WARN = "warn"
FAIL = "fail"
CONFIG_REL_PATH = ".brigade/tools.toml"
HEALTH_STALE_HOURS = 48
FAMILIES = ("skill", "slash-command", "superpower", "mcp", "openapi", "graphql", "script", "custom")
KNOWN_HARNESSES = ("claude", "codex", "opencode", "hermes", "openclaw", "mcp", "scripts")
UNSAFE_FIELD_PATTERN = re.compile(r"(password|secret|token|credential|api[_-]?key)", re.IGNORECASE)
HIGH_RISK_COMMAND_PATTERNS = (
    re.compile(r"\brm\s+-rf\b"),
    re.compile(r"\bcurl\b.+\|\s*(?:sh|bash)\b"),
    re.compile(r"\b(?:sh|bash)\s+-c\b"),
    re.compile(r"\bsudo\b"),
)
DEFAULT_TOOLS = (
    {
        "id": "simplify",
        "name": "Simplify",
        "family": "slash-command",
        "enabled": True,
        "description": "Portable simplify command placeholder.",
        "source_path": "tools/simplify.md",
        "supported_harnesses": ["claude", "codex"],
        "projections": {
            "claude": ".claude/commands/simplify.md",
            "codex": ".codex/skills/simplify/SKILL.md",
        },
    },
    {
        "id": "superpowers",
        "name": "Superpowers",
        "family": "superpower",
        "enabled": True,
        "description": "Portable superpowers placeholder.",
        "source_path": "tools/superpowers.md",
        "supported_harnesses": ["claude", "codex", "opencode"],
        "projections": {
            "claude": ".claude/commands/superpowers.md",
            "codex": ".codex/skills/superpowers/SKILL.md",
            "opencode": ".opencode/superpowers/superpowers.md",
        },
    },
)


def config_path(target: Path) -> Path:
    return target / CONFIG_REL_PATH


def _stable_hash(value: object) -> str:
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16]


def _file_hash(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()[:16]


def _short(text: str, limit: int = 96) -> str:
    rendered = " ".join(text.split())
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3].rstrip() + "..."


def _as_path(target: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value.strip()).expanduser()
    return path if path.is_absolute() else target / path


def _format_inline_list(values: list[str]) -> str:
    return "[" + ", ".join(dogfood_cmd._format_toml_value(value) for value in values) + "]"


def _format_inline_table(values: dict[str, str]) -> str:
    rendered = ", ".join(f"{key} = {dogfood_cmd._format_toml_value(value)}" for key, value in values.items())
    return "{ " + rendered + " }"


def _format_tools_toml(tools: tuple[dict[str, Any], ...] = DEFAULT_TOOLS) -> str:
    lines = [
        "# Local portable tool and skill catalog. Brigade inspects this file but does not sync projections.",
        "",
    ]
    for tool in tools:
        lines.append("[[tool]]")
        for key in ("id", "name", "family", "enabled", "description", "source_path"):
            lines.append(f"{key} = {dogfood_cmd._format_toml_value(tool[key])}")
        lines.append(f"supported_harnesses = {_format_inline_list(list(tool['supported_harnesses']))}")
        lines.append(f"projections = {_format_inline_table(dict(tool['projections']))}")
        lines.append("")
    return "\n".join(lines)


def _load_config(target: Path) -> tuple[list[dict[str, Any]], list[str]]:
    path = config_path(target)
    if not path.is_file():
        return [], [f"tool catalog config missing: {path}"]
    if tomllib is None:
        return [], ["tool catalog requires Python tomllib support"]
    try:
        payload = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:  # type: ignore[union-attr]
        return [], [f"invalid tool catalog config: {exc}"]
    values = payload.get("tool")
    if not isinstance(values, list):
        return [], ["tool catalog must contain [[tool]] entries"]
    tools: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, raw_tool in enumerate(values, start=1):
        label = f"tool {index}"
        if not isinstance(raw_tool, dict):
            errors.append(f"{label} must be a table")
            continue
        tool: dict[str, Any] = {"raw": raw_tool}
        for field in ("id", "name", "family"):
            value = raw_tool.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{label}: {field} must be a non-empty string")
            else:
                tool[field] = value.strip()
        if tool.get("family") and tool["family"] not in FAMILIES:
            errors.append(f"{label}: family must be one of: {', '.join(FAMILIES)}")
        tool_id = tool.get("id")
        if isinstance(tool_id, str):
            if tool_id in seen:
                errors.append(f"{label}: duplicate id {tool_id}")
            seen.add(tool_id)
        enabled = raw_tool.get("enabled", True)
        if not isinstance(enabled, bool):
            errors.append(f"{label}: enabled must be true or false")
        else:
            tool["enabled"] = enabled
        for field in ("description", "source_path", "manifest_path", "schema_path", "command", "auth_label", "health_path", "fingerprint"):
            value = raw_tool.get(field)
            if value is not None:
                if not isinstance(value, str):
                    errors.append(f"{label}: {field} must be a string")
                else:
                    tool[field] = value.strip()
        timeout = raw_tool.get("timeout")
        if timeout is not None:
            if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
                errors.append(f"{label}: timeout must be a positive number")
            else:
                tool["timeout"] = float(timeout)
        harnesses = raw_tool.get("supported_harnesses", [])
        if not isinstance(harnesses, list) or any(not isinstance(item, str) or not item.strip() for item in harnesses):
            errors.append(f"{label}: supported_harnesses must be a list of strings")
            harnesses = []
        tool["supported_harnesses"] = [item.strip() for item in harnesses if isinstance(item, str) and item.strip()]
        projections = raw_tool.get("projections", {})
        if not isinstance(projections, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in projections.items()):
            errors.append(f"{label}: projections must be a table of harness = path")
            projections = {}
        tool["projections"] = {str(key): str(value) for key, value in projections.items()}
        projection_fingerprints = raw_tool.get("projection_fingerprints", {})
        if projection_fingerprints is None:
            projection_fingerprints = {}
        if not isinstance(projection_fingerprints, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in projection_fingerprints.items()):
            errors.append(f"{label}: projection_fingerprints must be a table of harness = fingerprint")
            projection_fingerprints = {}
        tool["projection_fingerprints"] = {str(key): str(value) for key, value in projection_fingerprints.items()}
        tools.append(tool)
    return tools, errors


def _unsafe_fields(value: object, prefix: str = "") -> list[str]:
    unsafe: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            rendered = str(key)
            path = f"{prefix}.{rendered}" if prefix else rendered
            if UNSAFE_FIELD_PATTERN.search(rendered) and rendered != "auth_label":
                unsafe.append(path)
                continue
            unsafe.extend(_unsafe_fields(nested, path))
    elif isinstance(value, list):
        for index, nested in enumerate(value, start=1):
            unsafe.extend(_unsafe_fields(nested, f"{prefix}[{index}]"))
    return unsafe


def _command_parts(command: object) -> list[str]:
    if not isinstance(command, str) or not command.strip():
        return []
    try:
        return shlex.split(command)
    except ValueError:
        return []


def _command_resolves(command: object) -> bool:
    parts = _command_parts(command)
    if not parts:
        return False
    executable = parts[0]
    if executable == "brigade":
        return True
    if "/" in executable:
        return Path(executable).expanduser().exists()
    return shutil.which(executable) is not None


def _high_risk_command(command: object) -> bool:
    if not isinstance(command, str):
        return False
    return any(pattern.search(command) for pattern in HIGH_RISK_COMMAND_PATTERNS)


def _read_json(path: Path) -> tuple[object | None, str | None]:
    try:
        payload = json.loads(path.read_text())
    except OSError as exc:
        return None, str(exc)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc.msg}"
    return payload, None


def _tool_issue(tool: dict[str, Any], issue_type: str, detail: str, *, harness: str | None = None, target: str | None = None) -> dict[str, Any]:
    return {
        "status": WARN,
        "name": f"tool_{issue_type}",
        "tool_id": tool.get("id"),
        "family": tool.get("family"),
        "issue_type": issue_type,
        "harness": harness,
        "projection_target": target,
        "description": tool.get("description"),
        "detail": detail,
    }


def _inspect_mcp_config(tool: dict[str, Any], path: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    payload, error = _read_json(path)
    if error is not None:
        return None, [_tool_issue(tool, "invalid_schema", f"{path}: {error}")]
    if not isinstance(payload, dict) or not isinstance(payload.get("mcpServers"), dict):
        return None, []
    servers = payload["mcpServers"]
    issues: list[dict[str, Any]] = []
    server_ids = sorted(str(key) for key in servers)
    for server_id, server in servers.items():
        if not isinstance(server, dict):
            issues.append(_tool_issue(tool, "invalid_mcp_server", f"{server_id} must be an object"))
            continue
        command = server.get("command")
        if not isinstance(command, str) or not command.strip():
            issues.append(_tool_issue(tool, "missing_command", f"MCP server {server_id} is missing command"))
        elif _high_risk_command(command):
            issues.append(_tool_issue(tool, "high_risk_command", f"MCP server {server_id} command shape is high risk"))
        if "timeout" not in server and "timeout_seconds" not in server:
            issues.append(_tool_issue(tool, "missing_timeout", f"MCP server {server_id} is missing timeout metadata"))
    return {"server_count": len(server_ids), "server_ids": server_ids}, issues


def _inspect_tool(target: Path, tool: dict[str, Any], now: datetime | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
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
    }
    unsafe = _unsafe_fields(tool.get("raw", {}))
    if unsafe:
        issues.append(_tool_issue(tool, "unsafe_auth_fields", f"unsafe field names: {', '.join(unsafe[:8])}"))
    source_path = _as_path(target, tool.get("source_path"))
    if source_path is not None:
        summary["source_path"] = str(source_path)
        source_hash = _file_hash(source_path)
        summary["source_fingerprint"] = source_hash or tool.get("fingerprint")
        if not source_path.is_file():
            issues.append(_tool_issue(tool, "missing_source", f"missing source: {source_path}"))
    manifest_path = _as_path(target, tool.get("manifest_path"))
    if manifest_path is not None:
        summary["manifest_path"] = str(manifest_path)
        summary["manifest_available"] = manifest_path.is_file()
        if not manifest_path.is_file():
            issues.append(_tool_issue(tool, "missing_manifest", f"missing manifest: {manifest_path}"))
    schema_path = _as_path(target, tool.get("schema_path"))
    if schema_path is not None:
        summary["schema_path"] = str(schema_path)
        if not schema_path.is_file():
            issues.append(_tool_issue(tool, "missing_schema", f"missing schema: {schema_path}"))
        else:
            schema, error = _read_json(schema_path)
            if error is not None:
                issues.append(_tool_issue(tool, "invalid_schema", f"{schema_path}: {error}"))
            else:
                summary["schema_available"] = True
                if isinstance(schema, dict) and isinstance(schema.get("tools"), list):
                    summary["tool_count"] = len(schema["tools"])
    health_path = _as_path(target, tool.get("health_path"))
    if health_path is not None:
        summary["health_path"] = str(health_path)
        if not health_path.is_file():
            issues.append(_tool_issue(tool, "missing_health", f"missing health file: {health_path}"))
        else:
            age_hours = (now.timestamp() - health_path.stat().st_mtime) / 3600
            if age_hours > HEALTH_STALE_HOURS:
                issues.append(_tool_issue(tool, "stale_health", f"health file is {age_hours:.1f}h old"))
    command = tool.get("command")
    if tool.get("family") in {"script", "custom"} and not command:
        issues.append(_tool_issue(tool, "missing_command", "command is required for script/custom tools"))
    if command:
        summary["command"] = command
        if not _command_resolves(command):
            issues.append(_tool_issue(tool, "missing_command", f"command is not resolvable: {_short(str(command))}"))
        if _high_risk_command(command):
            issues.append(_tool_issue(tool, "high_risk_command", "command shape is high risk"))
    projections = tool.get("projections") if isinstance(tool.get("projections"), dict) else {}
    projection_fingerprints = (
        tool.get("projection_fingerprints") if isinstance(tool.get("projection_fingerprints"), dict) else {}
    )
    for harness in tool.get("supported_harnesses", []):
        projection = projections.get(harness)
        if not projection:
            summary["projection_coverage"][harness] = "missing"
            issues.append(_tool_issue(tool, "parity_gap", f"missing projection for {harness}", harness=harness))
            continue
        projection_path = _as_path(target, projection)
        assert projection_path is not None
        summary["projection_coverage"][harness] = "present" if projection_path.is_file() else "missing"
        if not projection_path.is_file():
            issues.append(_tool_issue(tool, "missing_projection", f"missing projection for {harness}: {projection_path}", harness=harness, target=str(projection_path)))
            continue
        expected = projection_fingerprints.get(harness)
        actual = _file_hash(projection_path)
        if expected and actual and expected != actual:
            summary["projection_coverage"][harness] = "stale"
            issues.append(_tool_issue(tool, "stale_projection", f"stale projection for {harness}: {projection_path}", harness=harness, target=str(projection_path)))
    if tool.get("family") == "mcp":
        mcp_path = manifest_path or schema_path or source_path
        if mcp_path is not None and mcp_path.is_file():
            mcp_summary, mcp_issues = _inspect_mcp_config(tool, mcp_path)
            if mcp_summary:
                summary["mcp"] = mcp_summary
                summary["tool_count"] = mcp_summary["server_count"]
            issues.extend(mcp_issues)
    return summary, issues


def _catalog_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    tools, errors = _load_config(target)
    summaries: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for tool in tools:
        if not tool.get("enabled", True):
            continue
        summary, tool_issues = _inspect_tool(target, tool, now=now)
        summaries.append(summary)
        issues.extend(tool_issues)
    if errors:
        issues.insert(0, {"status": WARN, "name": "tool_config", "issue_type": "config", "detail": "; ".join(errors)})
    return {
        "target": str(target),
        "config_path": str(config_path(target)),
        "valid": not errors,
        "errors": errors,
        "tools": summaries,
        "tool_count": len(summaries),
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }


def health(target: Path) -> dict[str, Any]:
    payload = _catalog_payload(target)
    return {
        "config_path": payload["config_path"],
        "valid": payload["valid"],
        "tool_count": payload["tool_count"],
        "issue_count": payload["issue_count"],
        "top_issue": payload["top_issue"],
        "issues": payload["issues"],
    }


def _issue_records(target: Path) -> list[dict[str, Any]]:
    payload = _catalog_payload(target)
    records: list[dict[str, Any]] = []
    for issue in payload["issues"]:
        issue_type = str(issue.get("issue_type") or issue.get("name") or "tool_issue")
        tool_id = str(issue.get("tool_id") or "catalog")
        detail = str(issue.get("detail") or "")
        metadata = {
            "tool_id": tool_id,
            "tool_family": issue.get("family"),
            "tool_issue_type": issue_type,
            "tool_harness": issue.get("harness"),
            "projection_target": issue.get("projection_target"),
            "tool_issue_detail": detail,
            "source_item_key": f"tool-catalog:{tool_id}:{issue_type}:{issue.get('harness') or ''}",
            "source_fingerprint": _stable_hash(
                {
                    "tool_id": tool_id,
                    "issue_type": issue_type,
                    "detail": detail,
                    "harness": issue.get("harness"),
                    "projection_target": issue.get("projection_target"),
                }
            ),
        }
        records.append(
            {
                "text": f"Repair tool catalog issue {tool_id}/{issue_type}: {detail}",
                "kind": "task",
                "source": "tool-catalog",
                "type": "workflow",
                "priority": "normal",
                "template": "bugfix",
                "acceptance": [f"`brigade tools doctor` no longer reports {tool_id}/{issue_type}."],
                "metadata": metadata,
            }
        )
    return records


def init(*, target: Path, force: bool = False, update_gitignore: bool = True) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = config_path(target)
    if path.exists() and not force:
        print(f"error: tool catalog config already exists: {path}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_format_tools_toml())
    print(f"tools_config: {path}")
    print(f"tools: {len(DEFAULT_TOOLS)}")
    if update_gitignore:
        result = apply_gitignore(target, Selection("repo", ["codex"], "codex"))
        print(f"gitignore: {result}")
    else:
        print("gitignore: skipped")
    print("next_command: brigade tools list")
    return 0


def list_tools(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _catalog_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"tools: {target}")
    print(f"config_path: {payload['config_path']}")
    if payload["errors"]:
        for error in payload["errors"]:
            print(f"error: {error}")
        return 1
    if not payload["tools"]:
        print("tools: none")
        return 0
    for tool in payload["tools"]:
        print(
            f"- {tool.get('id')} [{tool.get('family')}] "
            f"harnesses={','.join(tool.get('supported_harnesses', []))} "
            f"tools={tool.get('tool_count')}"
        )
        if tool.get("description"):
            print(f"  {_short(str(tool['description']))}")
    return 0


def show(*, target: Path, tool_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _catalog_payload(target)
    tool = None
    for item in payload["tools"]:
        if item.get("id") == tool_id:
            tool = item
            break
    if json_output:
        print(json.dumps({"target": str(target), "config_path": payload["config_path"], "tool": tool}, indent=2, sort_keys=True))
        return 0 if tool is not None else 1
    if tool is None:
        print(f"error: tool not found: {tool_id}", file=sys.stderr)
        return 1
    print(f"tool: {tool.get('id')}")
    print(f"name: {tool.get('name')}")
    print(f"family: {tool.get('family')}")
    print(f"description: {tool.get('description')}")
    print(f"supported_harnesses: {', '.join(tool.get('supported_harnesses', []))}")
    print(f"tool_count: {tool.get('tool_count')}")
    print(f"schema_available: {tool.get('schema_available')}")
    print(f"auth_label: {tool.get('auth_label') or ''}")
    print("projections:")
    for harness, status in sorted(tool.get("projection_coverage", {}).items()):
        print(f"  {harness}: {status}")
    if tool.get("mcp"):
        print(f"mcp_servers: {tool['mcp'].get('server_count')}")
    return 0


def search(*, target: Path, query: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    needle = query.casefold().strip()
    payload = _catalog_payload(target)
    matches = [
        tool
        for tool in payload["tools"]
        if needle
        and needle
        in " ".join(str(tool.get(key, "")) for key in ("id", "name", "family", "description")).casefold()
    ]
    result = {"target": str(target), "query": query, "matches": matches, "match_count": len(matches)}
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(f"tool search: {query}")
    print(f"matches: {len(matches)}")
    for tool in matches:
        print(f"- {tool.get('id')} [{tool.get('family')}] {_short(str(tool.get('description', '')))}")
    return 0


def doctor(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _catalog_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"tools doctor: {target}")
    print(f"config_path: {payload['config_path']}")
    if payload["errors"]:
        for error in payload["errors"]:
            print(f"[warn] tool_config: {error}")
    else:
        print(f"[ok] tool_config: {payload['config_path']}")
    if payload["issues"]:
        for issue in payload["issues"]:
            print(f"[{issue.get('status', WARN)}] {issue.get('name')}: {issue.get('detail')}")
    else:
        print("[ok] tool_catalog: no issues")
    print(f"tool_issues: {payload['issue_count']}")
    return 0 if payload["valid"] else 1


def import_issues(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    records = _issue_records(target)
    from . import work_cmd

    imported, skipped, skipped_dismissed = work_cmd._append_import_records(target, records)
    payload = {
        "target": str(target),
        "imports_path": str(work_cmd._imports_path(target)),
        "issues": len(records),
        "created": len(imported),
        "skipped": len(skipped),
        "dismissed": len(skipped_dismissed),
        "imports": imported,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"tool issue imports: {target}")
    print(f"imports_path: {payload['imports_path']}")
    print(f"issues: {len(records)}")
    print(f"created: {len(imported)}")
    print(f"skipped: {len(skipped)}")
    print(f"dismissed: {len(skipped_dismissed)}")
    for item in imported:
        print(f"- {item.get('id')} [{item.get('kind')}] {_short(str(item.get('text', '')))}")
    return 0
