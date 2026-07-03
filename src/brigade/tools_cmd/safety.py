"""Safety, policy, and contract helpers for the tools command family."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

from ..localio import stable_hash as _stable_hash
from ..render import emit
from . import config, constants, helpers, issues as issues_mod, paths


def _policy_decision(
    target: Path,
    plan: dict[str, Any],
    *,
    include_env_values: bool = False,
) -> dict[str, Any]:
    policy, errors = config._load_policy_config(target)
    if policy is None:
        return {
            "enabled": False,
            "policy_path": str(paths.policy_path(target)),
            "allowed": True,
            "blockers": [],
            "errors": errors,
            "env_labels_used": [],
            "env": {},
        }
    blockers: list[str] = list(errors)
    family = str(plan.get("family") or "")
    if policy["allowed_families"] and family not in policy["allowed_families"]:
        blockers.append(f"family is not allowed by policy: {family}")
    effects = [str(effect) for effect in (plan.get("effects") if isinstance(plan.get("effects"), list) else [])]
    for effect in effects:
        if effect in policy["denied_effects"]:
            blockers.append(f"effect is denied by policy: {effect}")
        if policy["allowed_effects"] and effect not in policy["allowed_effects"]:
            blockers.append(f"effect is not allowed by policy: {effect}")
    approval_mode = str(plan.get("approval_mode") or "never")
    if policy["required_approval_modes"] and approval_mode not in policy["required_approval_modes"]:
        blockers.append(f"approval mode is not allowed by policy: {approval_mode}")
    timeout = plan.get("timeout")
    if policy.get("max_timeout") is not None and isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
        if float(timeout) > float(policy["max_timeout"]):
            blockers.append(f"timeout exceeds policy max: {timeout} > {policy['max_timeout']}")
    runtime_id = plan.get("runtime_id")
    if (
        isinstance(runtime_id, str)
        and runtime_id.strip()
        and policy["allowed_runtimes"]
        and runtime_id not in policy["allowed_runtimes"]
    ):
        blockers.append(f"runtime is not allowed by policy: {runtime_id}")
    env_bindings = policy.get("env_bindings", {}) if isinstance(policy.get("env_bindings"), dict) else {}
    env_labels = [str(label) for label in (plan.get("env_labels") if isinstance(plan.get("env_labels"), list) else [])]
    env: dict[str, str] = {}
    env_labels_used: list[str] = []
    for label in env_labels:
        env_name = env_bindings.get(label)
        if not env_name:
            blockers.append(f"missing env binding for label: {label}")
            continue
        if env_name not in os.environ:
            blockers.append(f"missing process env for label: {label}")
            continue
        env_labels_used.append(label)
        if include_env_values:
            env[label] = os.environ[env_name]
    return {
        "enabled": True,
        "policy_path": str(paths.policy_path(target)),
        "allowed": not blockers,
        "blockers": blockers,
        "errors": errors,
        "allowed_families": policy["allowed_families"],
        "allowed_effects": policy["allowed_effects"],
        "denied_effects": policy["denied_effects"],
        "required_approval_modes": policy["required_approval_modes"],
        "max_timeout": policy.get("max_timeout"),
        "allowed_runtimes": policy["allowed_runtimes"],
        "env_labels_required": env_labels,
        "env_labels_used": env_labels_used,
        "env": env,
    }


def _policy_health(target: Path, tools: list[dict[str, Any]]) -> dict[str, Any]:
    policy, errors = config._load_policy_config(target)
    issues: list[dict[str, Any]] = []
    if policy is None:
        policy_relevant = [
            tool for tool in tools if tool.get("enabled", True) and tool.get("command") and _contract_defined(tool)
        ]
        if policy_relevant:
            issues.append(
                {
                    "status": constants.WARN,
                    "name": "tool_policy_missing",
                    "tool_id": "policy",
                    "family": "policy",
                    "issue_type": "policy_missing",
                    "detail": errors[0] if errors else f"tool execution policy missing: {paths.policy_path(target)}",
                }
            )
        return {
            "policy_path": str(paths.policy_path(target)),
            "enabled": False,
            "valid": False,
            "errors": errors,
            "issues": issues,
            "issue_count": len(issues),
            "top_issue": issues[0] if issues else None,
        }
    if errors:
        issues.append(
            {
                "status": constants.WARN,
                "name": "tool_policy_config",
                "tool_id": "policy",
                "family": "policy",
                "issue_type": "policy_config",
                "detail": "; ".join(errors),
            }
        )
    for tool in tools:
        if not tool.get("enabled", True):
            continue
        plan = {
            "family": tool.get("family"),
            "effects": tool.get("effects", []),
            "approval_mode": tool.get("approval_mode", "never"),
            "timeout": tool.get("timeout"),
            "runtime_id": tool.get("runtime_id"),
            "env_labels": tool.get("env_labels", []),
        }
        decision = _policy_decision(target, plan)
        for blocker in decision["blockers"]:
            issue_type = "policy_blocker"
            if "missing env binding" in blocker or "missing process env" in blocker:
                issue_type = "policy_missing_env"
            elif "effect is denied" in blocker:
                issue_type = "policy_denied_effect"
            elif "timeout exceeds" in blocker:
                issue_type = "policy_timeout"
            elif "runtime is not allowed" in blocker:
                issue_type = "policy_runtime"
            elif "approval mode" in blocker:
                issue_type = "policy_approval"
            issues.append(issues_mod._tool_issue(tool, issue_type, blocker))
    return {
        "policy_path": str(paths.policy_path(target)),
        "enabled": True,
        "valid": not errors,
        "errors": errors,
        "policy": {key: value for key, value in policy.items() if key != "raw"},
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }


def _unsafe_fields(value: object, prefix: str = "") -> list[str]:
    unsafe: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            rendered = str(key)
            path = f"{prefix}.{rendered}" if prefix else rendered
            if constants.UNSAFE_FIELD_PATTERN.search(rendered) and rendered != "auth_label":
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
    return any(pattern.search(command) for pattern in constants.HIGH_RISK_COMMAND_PATTERNS)


def _redact_value(key: str, value: object) -> object:
    if constants.UNSAFE_FIELD_PATTERN.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            str(nested_key): _redact_value(str(nested_key), nested_value) for nested_key, nested_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(key, item) for item in value]
    if isinstance(value, str):
        return _redact_text(value, None)
    return value


def _redact_payload(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _redact_value(str(key), nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value, None)
    return value


def _redact_text(value: object, limit: int | None = 500) -> str:
    text = "" if value is None else str(value)
    text = re.sub(
        r"(?i)\b([A-Za-z0-9_-]*(?:password|secret|token|credential|api[_-]?key)[A-Za-z0-9_-]*)\b\s*[:=]\s*[^\s\"']+",
        lambda match: f"{match.group(1)}=[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)(\"(?:password|secret|token|credential|api[_-]?key)\"\s*:\s*\")[^\"]+(\")",
        r"\1[redacted]\2",
        text,
    )
    if limit is None:
        return text
    return helpers._short(text, limit)


def _redact_known_values(value: object, secrets: list[str]) -> object:
    if isinstance(value, dict):
        return {str(key): _redact_known_values(nested, secrets) for key, nested in value.items()}
    if isinstance(value, list):
        return [_redact_known_values(item, secrets) for item in value]
    if isinstance(value, str):
        text = value
        for secret in secrets:
            if secret:
                text = text.replace(secret, "[redacted]")
        return text
    return value


def _schema_path(target: Path, tool: dict[str, Any], field: str) -> Path | None:
    return helpers._as_path(target, tool.get(field))


def _load_schema(target: Path, tool: dict[str, Any], field: str) -> tuple[object | None, str | None]:
    path = _schema_path(target, tool, field)
    if path is None:
        return None, None
    if not path.is_file():
        return None, f"missing schema: {path}"
    return helpers._read_json(path)


def _schema_shape_errors(schema: object, *, path: str = "$", root: bool = True) -> list[str]:
    if not isinstance(schema, dict):
        return [f"{path}: schema must be an object"]
    schema_type = schema.get("type")
    if root and schema_type != "object":
        return [f"{path}: root schema type must be object"]
    if schema_type is not None and schema_type not in constants.SCHEMA_TYPES:
        return [f"{path}: unsupported type {schema_type!r}"]
    if "enum" in schema and not isinstance(schema["enum"], list):
        return [f"{path}.enum: must be a list"]
    errors: list[str] = []
    if schema_type == "object" or "properties" in schema or "required" in schema:
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            errors.append(f"{path}.properties: must be an object")
        else:
            for key, nested in properties.items():
                if not isinstance(key, str):
                    errors.append(f"{path}.properties: keys must be strings")
                    continue
                errors.extend(_schema_shape_errors(nested, path=f"{path}.{key}", root=False))
        required = schema.get("required", [])
        if required is not None and (
            not isinstance(required, list) or any(not isinstance(item, str) for item in required)
        ):
            errors.append(f"{path}.required: must be a list of strings")
        additional = schema.get("additionalProperties", True)
        if not isinstance(additional, bool):
            errors.append(f"{path}.additionalProperties: only boolean values are supported")
    if schema_type == "array":
        items = schema.get("items")
        if items is None:
            errors.append(f"{path}.items: required for arrays")
        else:
            errors.extend(_schema_shape_errors(items, path=f"{path}[]", root=False))
    unsupported = sorted(
        set(schema) - {"type", "properties", "required", "additionalProperties", "items", "enum", "description"}
    )
    if unsupported:
        errors.append(f"{path}: unsupported schema keywords: {', '.join(unsupported)}")
    return errors


def _json_type_matches(value: object, schema_type: str) -> bool:
    if schema_type == "object":
        return isinstance(value, dict)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "null":
        return value is None
    return False


def _validate_json_value(value: object, schema: dict[str, Any], *, path: str = "$") -> list[str]:
    errors: list[str] = []
    schema_type = schema.get("type")
    if isinstance(schema_type, str) and not _json_type_matches(value, schema_type):
        errors.append(f"{path}: expected {schema_type}")
        return errors
    if "enum" in schema and isinstance(schema["enum"], list) and value not in schema["enum"]:
        errors.append(f"{path}: expected one of {', '.join(repr(item) for item in schema['enum'])}")
    if (schema_type == "object" or "properties" in schema or "required" in schema) and isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        for key in required:
            if key not in value:
                errors.append(f"{path}.{key}: required")
        additional = schema.get("additionalProperties", True)
        if additional is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}.{key}: additional property not allowed")
        for key, nested_schema in properties.items():
            if key in value and isinstance(nested_schema, dict):
                errors.extend(_validate_json_value(value[key], nested_schema, path=f"{path}.{key}"))
    if schema_type == "array" and isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                errors.extend(_validate_json_value(item, items, path=f"{path}[{index}]"))
    return errors


def _render_argument_template(template: str, args: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        value = args.get(key)
        if isinstance(value, (dict, list)):
            return json.dumps(value, sort_keys=True)
        if value is None:
            return ""
        return str(value)

    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, template)


def _contract_defined(tool: dict[str, Any]) -> bool:
    return any(
        tool.get(field)
        for field in (
            "input_schema_path",
            "output_schema_path",
            "examples_path",
            "permissions",
            "effects",
            "approval_mode",
            "env_labels",
            "argument_template",
            "runtime_id",
            "requires_runtime",
            "runtime_health_path",
            "mcp_server_id",
            "mcp_tool_name",
        )
    )


def _contract_summary(target: Path, tool: dict[str, Any]) -> dict[str, Any]:
    input_path = _schema_path(target, tool, "input_schema_path")
    output_path = _schema_path(target, tool, "output_schema_path")
    examples_path = helpers._as_path(target, tool.get("examples_path"))
    return {
        "tool_id": tool.get("id"),
        "name": tool.get("name"),
        "family": tool.get("family"),
        "description": tool.get("description", ""),
        "command": tool.get("command"),
        "timeout": tool.get("timeout"),
        "auth_label": tool.get("auth_label"),
        "cwd": tool.get("cwd"),
        "runtime_id": tool.get("runtime_id"),
        "requires_runtime": tool.get("requires_runtime", False),
        "runtime_health_path": tool.get("runtime_health_path"),
        "mcp_server_id": tool.get("mcp_server_id"),
        "mcp_tool_name": tool.get("mcp_tool_name"),
        "approval_mode": tool.get("approval_mode") or "never",
        "permissions": tool.get("permissions", []),
        "effects": tool.get("effects", []),
        "env_labels": tool.get("env_labels", []),
        "argument_template": tool.get("argument_template", {}),
        "input_schema_path": str(input_path) if input_path is not None else None,
        "output_schema_path": str(output_path) if output_path is not None else None,
        "examples_path": str(examples_path) if examples_path is not None else None,
        "has_contract": _contract_defined(tool),
    }


def _source_fingerprint(target: Path, tool: dict[str, Any]) -> str:
    source_path = helpers._as_path(target, tool.get("source_path"))
    if source_path is not None:
        source_hash = helpers._file_hash(source_path)
        if source_hash is not None:
            return source_hash
    return str(tool.get("fingerprint") or "")


def _contract_fingerprint(target: Path, tool: dict[str, Any]) -> str:
    paths: dict[str, str | None] = {}
    for field in ("input_schema_path", "output_schema_path", "examples_path"):
        path = helpers._as_path(target, tool.get(field))
        paths[field] = helpers._file_hash(path) if path is not None else None
    return _stable_hash(
        {
            "tool_id": tool.get("id"),
            "command": tool.get("command"),
            "timeout": tool.get("timeout"),
            "auth_label": tool.get("auth_label"),
            "cwd": tool.get("cwd"),
            "runtime_id": tool.get("runtime_id"),
            "requires_runtime": tool.get("requires_runtime", False),
            "runtime_health_path": tool.get("runtime_health_path"),
            "mcp_server_id": tool.get("mcp_server_id"),
            "mcp_tool_name": tool.get("mcp_tool_name"),
            "approval_mode": tool.get("approval_mode"),
            "permissions": tool.get("permissions", []),
            "effects": tool.get("effects", []),
            "env_labels": tool.get("env_labels", []),
            "argument_template": tool.get("argument_template", {}),
            "paths": paths,
        }
    )


def _contract_issues(target: Path, tool: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if tool.get("family") == "mcp":
        if not tool.get("runtime_id"):
            issues.append(issues_mod._tool_issue(tool, "missing_runtime", "runtime_id is required for MCP execution"))
        if not tool.get("mcp_tool_name"):
            issues.append(
                issues_mod._tool_issue(tool, "missing_mcp_tool_name", "mcp_tool_name is required for MCP execution")
            )
        if not tool.get("command"):
            issues.append(
                issues_mod._tool_issue(tool, "missing_command", "command is required for local MCP stdio execution")
            )
    if not _contract_defined(tool):
        if tool.get("command") or tool.get("family") in {"script", "custom", "mcp", "openapi", "graphql"}:
            issues.append(issues_mod._tool_issue(tool, "missing_contract", "tool has no call contract metadata"))
        return issues
    for field, issue_prefix in (("input_schema_path", "input_schema"), ("output_schema_path", "output_schema")):
        schema_path = _schema_path(target, tool, field)
        if schema_path is None:
            if field == "input_schema_path":
                issues.append(
                    issues_mod._tool_issue(
                        tool, "missing_input_schema", "input_schema_path is required for call planning"
                    )
                )
            continue
        if not schema_path.is_file():
            issues.append(issues_mod._tool_issue(tool, f"missing_{issue_prefix}", f"missing schema: {schema_path}"))
            continue
        schema, error = helpers._read_json(schema_path)
        if error is not None:
            issues.append(issues_mod._tool_issue(tool, f"invalid_{issue_prefix}", f"{schema_path}: {error}"))
            continue
        shape_errors = _schema_shape_errors(schema)
        if shape_errors:
            issues.append(
                issues_mod._tool_issue(tool, f"unsupported_{issue_prefix}", f"{schema_path}: {'; '.join(shape_errors)}")
            )
    examples_path = helpers._as_path(target, tool.get("examples_path"))
    if tool.get("examples_path") and (examples_path is None or not examples_path.is_file()):
        issues.append(issues_mod._tool_issue(tool, "missing_examples", f"missing examples: {examples_path}"))
    for label in tool.get("env_labels", []):
        if constants.UNSAFE_FIELD_PATTERN.search(label):
            issues.append(issues_mod._tool_issue(tool, "unsafe_env_labels", f"unsafe env label name: {label}"))
    for key, value in tool.get("argument_template", {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key)):
            issues.append(issues_mod._tool_issue(tool, "bad_argument_template", f"invalid template output key: {key}"))
        for variable in re.findall(r"\{([^{}]+)\}", str(value)):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
                issues.append(
                    issues_mod._tool_issue(tool, "bad_argument_template", f"invalid template variable: {variable}")
                )
    return issues


def policy_init(*, target: Path, force: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = paths.policy_path(target)
    if path.exists() and not force:
        print(f"error: tool execution policy already exists: {path}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(helpers._format_policy_toml())
    print(f"policy_config: {path}")
    print("next_command: brigade tools policy show")
    return 0


def policy_show(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    policy, errors = config._load_policy_config(target)
    payload = {
        "target": str(target),
        "policy_path": str(paths.policy_path(target)),
        "valid": policy is not None and not errors,
        "errors": errors,
        "policy": {key: value for key, value in (policy or {}).items() if key != "raw"} if policy else None,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"tools policy: {target}")
    print(f"policy_path: {payload['policy_path']}")
    if errors:
        for error in errors:
            print(f"error: {error}")
        return 1
    assert policy is not None
    print(f"allowed_families: {', '.join(policy['allowed_families'])}")
    print(f"allowed_effects: {', '.join(policy['allowed_effects'])}")
    print(f"denied_effects: {', '.join(policy['denied_effects'])}")
    print(f"required_approval_modes: {', '.join(policy['required_approval_modes'])}")
    print(f"max_timeout: {policy.get('max_timeout') or ''}")
    print(f"allowed_runtimes: {', '.join(policy['allowed_runtimes'])}")
    print(f"env_bindings: {len(policy['env_bindings'])}")
    return 0


def policy_doctor(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    tools, tool_errors = config._load_config(target)
    payload = _policy_health(target, tools)
    payload["target"] = str(target)
    payload["tool_errors"] = tool_errors
    text_lines = [f"tools policy doctor: {target}", f"policy_path: {payload['policy_path']}"]
    for error in payload.get("errors", []):
        text_lines.append(f"[warn] tool_policy: {error}")
    if payload["issues"]:
        for issue in payload["issues"]:
            text_lines.append(f"[{issue.get('status', constants.WARN)}] {issue.get('name')}: {issue.get('detail')}")
    else:
        text_lines.append("[ok] tool_policy: no issues")
    text_lines.append(f"policy_issues: {payload['issue_count']}")
    return emit(payload, json_output, text_lines, 0 if payload["enabled"] and payload["valid"] else 1)
