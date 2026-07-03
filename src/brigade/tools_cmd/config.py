"""Configuration loaders for the tools command family."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import toml_compat as tomllib
from . import constants, paths


def _load_config(target: Path) -> tuple[list[dict[str, Any]], list[str]]:
    path = paths.config_path(target)
    if not path.is_file():
        return [], [f"tool catalog config missing: {path}"]
    if tomllib is None:
        return [], ["tool catalog requires Python tomllib support"]
    try:
        payload = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:  # type: ignore[union-attr]
        return [], [f"invalid tool catalog config: {exc}"]
    values = payload.get("tool")
    if values is None:
        # An empty catalog is valid: minimal installs start with no tools.
        return [], []
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
        if tool.get("family") and tool["family"] not in constants.FAMILIES:
            errors.append(f"{label}: family must be one of: {', '.join(constants.FAMILIES)}")
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
        for field in (
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
        ):
            value = raw_tool.get(field)
            if value is not None:
                if not isinstance(value, str):
                    errors.append(f"{label}: {field} must be a string")
                else:
                    tool[field] = value.strip()
        if tool.get("approval_mode") and tool["approval_mode"] not in constants.APPROVAL_MODES:
            errors.append(f"{label}: approval_mode must be one of: {', '.join(constants.APPROVAL_MODES)}")
        requires_runtime = raw_tool.get("requires_runtime", False)
        if not isinstance(requires_runtime, bool):
            errors.append(f"{label}: requires_runtime must be true or false")
            requires_runtime = False
        tool["requires_runtime"] = requires_runtime
        for field in ("permissions", "effects", "env_labels"):
            values = raw_tool.get(field, [])
            if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
                errors.append(f"{label}: {field} must be a list of strings")
                values = []
            tool[field] = [item.strip() for item in values if isinstance(item, str) and item.strip()]
        argument_template = raw_tool.get("argument_template", {})
        if argument_template is None:
            argument_template = {}
        if not isinstance(argument_template, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in argument_template.items()
        ):
            errors.append(f"{label}: argument_template must be a table of name = template")
            argument_template = {}
        tool["argument_template"] = {str(key): str(value) for key, value in argument_template.items()}
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
        if not isinstance(projections, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in projections.items()
        ):
            errors.append(f"{label}: projections must be a table of harness = path")
            projections = {}
        tool["projections"] = {str(key): str(value) for key, value in projections.items()}
        projection_fingerprints = raw_tool.get("projection_fingerprints", {})
        if projection_fingerprints is None:
            projection_fingerprints = {}
        if not isinstance(projection_fingerprints, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in projection_fingerprints.items()
        ):
            errors.append(f"{label}: projection_fingerprints must be a table of harness = fingerprint")
            projection_fingerprints = {}
        tool["projection_fingerprints"] = {str(key): str(value) for key, value in projection_fingerprints.items()}
        tools.append(tool)
    return tools, errors


def _load_runtime_config(target: Path) -> tuple[list[dict[str, Any]], list[str]]:
    path = paths.runtimes_config_path(target)
    if not path.is_file():
        return [], [f"tool runtime config missing: {path}"]
    if tomllib is None:
        return [], ["tool runtime config requires Python tomllib support"]
    try:
        payload = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:  # type: ignore[union-attr]
        return [], [f"invalid tool runtime config: {exc}"]
    values = payload.get("runtime")
    if not isinstance(values, list):
        return [], ["tool runtime config must contain [[runtime]] entries"]
    runtimes: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, raw_runtime in enumerate(values, start=1):
        label = f"runtime {index}"
        if not isinstance(raw_runtime, dict):
            errors.append(f"{label} must be a table")
            continue
        runtime: dict[str, Any] = {"raw": raw_runtime}
        for field in ("id", "name", "command"):
            value = raw_runtime.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{label}: {field} must be a non-empty string")
            else:
                runtime[field] = value.strip()
        runtime_id = runtime.get("id")
        if isinstance(runtime_id, str):
            if runtime_id in seen:
                errors.append(f"{label}: duplicate id {runtime_id}")
            seen.add(runtime_id)
        enabled = raw_runtime.get("enabled", True)
        if not isinstance(enabled, bool):
            errors.append(f"{label}: enabled must be true or false")
        else:
            runtime["enabled"] = enabled
        for field in ("cwd", "health_command", "health_path", "pid_path", "log_path"):
            value = raw_runtime.get(field)
            if value is not None:
                if not isinstance(value, str):
                    errors.append(f"{label}: {field} must be a string")
                else:
                    runtime[field] = value.strip()
        port = raw_runtime.get("port")
        if port is not None:
            if not isinstance(port, int) or isinstance(port, bool) or port <= 0 or port > 65535:
                errors.append(f"{label}: port must be an integer from 1 to 65535")
            else:
                runtime["port"] = port
        timeout = raw_runtime.get("timeout")
        if timeout is not None:
            if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
                errors.append(f"{label}: timeout must be a positive number")
            else:
                runtime["timeout"] = float(timeout)
        runtimes.append(runtime)
    return runtimes, errors


def _load_policy_config(target: Path) -> tuple[dict[str, Any] | None, list[str]]:
    path = paths.policy_path(target)
    if not path.is_file():
        return None, [f"tool execution policy missing: {path}"]
    if tomllib is None:
        return None, ["tool execution policy requires Python tomllib support"]
    try:
        raw_policy = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:  # type: ignore[union-attr]
        return None, [f"invalid tool execution policy: {exc}"]
    errors: list[str] = []
    policy: dict[str, Any] = {"raw": raw_policy}
    for field in (
        "allowed_families",
        "allowed_effects",
        "denied_effects",
        "required_approval_modes",
        "allowed_runtimes",
    ):
        values = raw_policy.get(field, [])
        if values is None:
            values = []
        if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
            errors.append(f"{field} must be a list of strings")
            values = []
        policy[field] = [item.strip() for item in values if isinstance(item, str) and item.strip()]
    invalid_families = [family for family in policy["allowed_families"] if family not in constants.FAMILIES]
    if invalid_families:
        errors.append(f"allowed_families has unknown values: {', '.join(invalid_families)}")
    invalid_modes = [mode for mode in policy["required_approval_modes"] if mode not in constants.APPROVAL_MODES]
    if invalid_modes:
        errors.append(f"required_approval_modes has unknown values: {', '.join(invalid_modes)}")
    max_timeout = raw_policy.get("max_timeout")
    if max_timeout is not None:
        if not isinstance(max_timeout, (int, float)) or isinstance(max_timeout, bool) or max_timeout <= 0:
            errors.append("max_timeout must be a positive number")
            max_timeout = None
        else:
            max_timeout = float(max_timeout)
    policy["max_timeout"] = max_timeout
    env_bindings = raw_policy.get("env_bindings", {})
    if env_bindings is None:
        env_bindings = {}
    if not isinstance(env_bindings, dict) or any(
        not isinstance(key, str) or not key.strip() or not isinstance(value, str) or not value.strip()
        for key, value in env_bindings.items()
    ):
        errors.append("env_bindings must be a table of label = environment variable")
        env_bindings = {}
    cleaned_bindings: dict[str, str] = {}
    for key, value in env_bindings.items():
        label = str(key).strip()
        env_name = str(value).strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", label):
            errors.append(f"env binding label is invalid: {label}")
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
            errors.append(f"env binding target is invalid for label: {label}")
            continue
        cleaned_bindings[label] = env_name
    policy["env_bindings"] = cleaned_bindings
    return policy, errors
