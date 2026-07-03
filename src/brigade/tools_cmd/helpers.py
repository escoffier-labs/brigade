"""Shared helpers for the tools command family."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import localio, toml_compat as tomllib
from . import constants


def _stable_hash(value: object) -> str:
    return localio.stable_hash(value)


def _now() -> datetime:
    return localio.utc_now()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    localio.write_json(path, payload)


def _parse_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    rendered = value.strip()
    if rendered.endswith("Z"):
        rendered = rendered[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _file_hash(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()[:16]


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


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


def _path_within_target(target: Path, path: Path) -> bool:
    try:
        resolved = path.resolve()
        target_resolved = target.resolve()
    except OSError:
        return False
    return resolved != target_resolved and resolved.is_relative_to(target_resolved)


def _format_inline_list(values: list[str]) -> str:
    return "[" + ", ".join(tomllib.format_toml_value(value) for value in values) + "]"


def _format_inline_table(values: dict[str, str]) -> str:
    rendered = ", ".join(f"{key} = {tomllib.format_toml_value(value)}" for key, value in values.items())
    return "{ " + rendered + " }"


def _format_toml_key(key: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
        return key
    return json.dumps(key)


def _format_toml_object(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:g}"
    if isinstance(value, list):
        return "[" + ", ".join(_format_toml_object(item) for item in value) + "]"
    if isinstance(value, dict):
        rendered = ", ".join(
            f"{_format_toml_key(str(key))} = {_format_toml_object(item)}" for key, item in value.items()
        )
        return "{ " + rendered + " }"
    return json.dumps(str(value))


def _format_tool_entries(entries: list[dict[str, Any]]) -> str:
    lines = [
        "# Local portable tool and skill catalog. Brigade inspects this file but does not sync projections.",
        "",
    ]
    preferred = [
        "id",
        "name",
        "family",
        "enabled",
        "description",
        "source_path",
        "manifest_path",
        "schema_path",
        "command",
        "auth_label",
        "health_path",
        "fingerprint",
        "input_schema_path",
        "output_schema_path",
        "examples_path",
        "approval_mode",
        "cwd",
        "runtime_id",
        "runtime_health_path",
        "mcp_server_id",
        "mcp_tool_name",
        "requires_runtime",
        "timeout",
        "permissions",
        "effects",
        "env_labels",
        "supported_harnesses",
        "projections",
        "projection_fingerprints",
        "argument_template",
    ]
    for entry in entries:
        lines.append("[[tool]]")
        keys = [key for key in preferred if key in entry]
        keys.extend(sorted(key for key in entry if key not in set(preferred)))
        for key in keys:
            if key == "raw":
                continue
            lines.append(f"{_format_toml_key(key)} = {_format_toml_object(entry[key])}")
        lines.append("")
    return "\n".join(lines)


def _format_tools_toml(tools: tuple[dict[str, Any], ...] | None = None) -> str:
    if tools is None:
        tools = constants.DEFAULT_TOOLS
    return _format_tool_entries([dict(tool) for tool in tools])


def _default_tool_source_text(tool_id: str) -> str | None:
    source_path = Path(__file__).resolve().parents[3] / "tools" / f"{tool_id}.md"
    if source_path.is_file():
        return source_path.read_text(errors="replace")
    return constants.DEFAULT_TOOL_SOURCE_TEXTS.get(tool_id)


def _ensure_default_tool_sources(target: Path, *, dry_run: bool = False) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for tool in constants.DEFAULT_TOOLS:
        tool_id = str(tool.get("id") or "")
        source_path = tool.get("source_path")
        if not tool_id or not isinstance(source_path, str) or not source_path.strip():
            continue
        rel = Path(source_path)
        if rel.is_absolute() or ".." in rel.parts:
            results.append(
                {
                    "tool_id": tool_id,
                    "source_path": source_path,
                    "action": "skip",
                    "detail": "source path is not repo-relative",
                }
            )
            continue
        dest = target / rel
        if dest.exists():
            results.append(
                {
                    "tool_id": tool_id,
                    "source_path": source_path,
                    "path": str(dest),
                    "action": "skip",
                    "detail": "source exists",
                }
            )
            continue
        text = _default_tool_source_text(tool_id)
        if text is None:
            results.append(
                {
                    "tool_id": tool_id,
                    "source_path": source_path,
                    "path": str(dest),
                    "action": "missing",
                    "detail": "built-in source text unavailable",
                }
            )
            continue
        results.append(
            {
                "tool_id": tool_id,
                "source_path": source_path,
                "path": str(dest),
                "action": "create" if not dry_run else "would_create",
            }
        )
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text if text.endswith("\n") else text + "\n")
    return results


def _format_runtimes_toml(runtimes: tuple[dict[str, Any], ...] | None = None) -> str:
    if runtimes is None:
        runtimes = constants.DEFAULT_RUNTIMES
    lines = [
        "# Local portable tool runtimes. Brigade starts and stops only explicit local runtimes.",
        "",
    ]
    for runtime in runtimes:
        lines.append("[[runtime]]")
        for key in (
            "id",
            "name",
            "enabled",
            "command",
            "cwd",
            "port",
            "health_command",
            "health_path",
            "pid_path",
            "log_path",
            "timeout",
        ):
            lines.append(f"{key} = {tomllib.format_toml_value(runtime[key])}")
        lines.append("")
    return "\n".join(lines)


def _format_policy_toml(policy: dict[str, Any] | None = None) -> str:
    if policy is None:
        policy = constants.DEFAULT_POLICY
    lines = [
        "# Host-local portable tool execution policy. Keep secrets in the process environment, not here.",
        f"allowed_families = {_format_inline_list(list(policy['allowed_families']))}",
        f"allowed_effects = {_format_inline_list(list(policy['allowed_effects']))}",
        f"denied_effects = {_format_inline_list(list(policy['denied_effects']))}",
        f"required_approval_modes = {_format_inline_list(list(policy['required_approval_modes']))}",
        f"max_timeout = {tomllib.format_toml_value(policy['max_timeout'])}",
        f"allowed_runtimes = {_format_inline_list(list(policy['allowed_runtimes']))}",
        f"env_bindings = {_format_inline_table(dict(policy['env_bindings']))}",
        "",
    ]
    return "\n".join(lines)


def _read_json(path: Path) -> tuple[object | None, str | None]:
    try:
        payload = json.loads(path.read_text())
    except OSError as exc:
        return None, str(exc)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc.msg}"
    return payload, None
