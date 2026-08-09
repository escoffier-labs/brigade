"""Document-level security scan detectors for agent workspaces."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse

from .. import work_cmd
from ..selection import WRITER_INBOXES
from ..untrusted import PROMPT_INJECTION_RE, scan_untrusted
from .. import localio
from ..localio import read_json_dict as _read_json, utc_now_iso_z as _utc_iso, write_json as _write_json

from . import models as _family_base

globals().update({name: value for name, value in vars(_family_base).items() if not name.startswith("__")})


def _line_number_for(text: str, needle: str) -> int:
    if not needle:
        return 1
    for line_number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return line_number
    return 1


def _is_mcp_document(path: Path, text: str) -> bool:
    return "mcp" in path.name.lower() or '"mcpServers"' in text


def _server_timeout(server: dict[str, Any]) -> object:
    for key in ("timeout", "timeout_seconds", "timeoutSeconds", "startupTimeout", "startupTimeoutMs"):
        if key in server:
            return server[key]
    return None


def _scan_mcp_document(
    findings: list[dict[str, Any]], *, target: Path, path: Path, text: str, classification: FileClassification
) -> None:
    if not _is_mcp_document(path, text):
        return
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return
    if len(servers) > MCP_SERVER_COUNT_WARN:
        _finding(
            findings,
            target=target,
            path=path,
            line=_line_number_for(text, "mcpServers"),
            severity="low",
            category="mcp",
            title="Large MCP server set",
            evidence=f"mcpServers: {len(servers)} configured",
            suggestion="Review whether every MCP server is still needed and disable stale or duplicate servers.",
            classification=classification,
        )
    for server_name, raw_server in servers.items():
        if not isinstance(server_name, str) or not isinstance(raw_server, dict):
            continue
        server = raw_server
        line_number = _line_number_for(text, server_name)
        command = server.get("command")
        args = server.get("args", [])
        command_name = Path(command).name if isinstance(command, str) else None
        if command_name in MCP_HIGH_RISK_COMMANDS:
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="mcp",
                title="MCP high-risk local command",
                evidence=f"{server_name}: command={command}",
                suggestion="Prefer purpose-built MCP binaries with narrow capabilities over direct shell, container, or remote-copy commands.",
                classification=classification,
            )
        if isinstance(command, str) and command == "npx" and isinstance(args, list):
            package = _first_npx_package(args)
            if package and "@" not in package:
                _finding(
                    findings,
                    target=target,
                    path=path,
                    line=line_number,
                    severity="medium",
                    category="mcp",
                    title="MCP unpinned npx package",
                    evidence=f"{server_name}: npx {package}",
                    suggestion="Pin MCP package versions or install through a reviewed lockfile.",
                    classification=classification,
                )
        if isinstance(args, list):
            for arg in args:
                if not isinstance(arg, str):
                    continue
                if MCP_SHELL_META_RE.search(arg):
                    _finding(
                        findings,
                        target=target,
                        path=path,
                        line=line_number,
                        severity="high",
                        category="mcp",
                        title="MCP shell metacharacter in argument",
                        evidence=f"{server_name}: arg={arg}",
                        suggestion="Remove shell metacharacters from MCP args and pass structured arguments directly.",
                        classification=classification,
                    )
                if arg in MCP_BROAD_PATHS:
                    _finding(
                        findings,
                        target=target,
                        path=path,
                        line=line_number,
                        severity="medium",
                        category="mcp",
                        title="MCP broad filesystem argument",
                        evidence=f"{server_name}: arg={arg}",
                        suggestion="Scope MCP filesystem access to explicit project directories instead of home or filesystem roots.",
                        classification=classification,
                    )
                if MCP_SENSITIVE_ARG_RE.search(arg):
                    _finding(
                        findings,
                        target=target,
                        path=path,
                        line=line_number,
                        severity="medium",
                        category="mcp",
                        title="MCP sensitive file argument",
                        evidence=f"{server_name}: arg={arg}",
                        suggestion="Avoid passing broad sensitive file paths to MCP servers; scope access to explicit project files.",
                        classification=classification,
                    )
        env = server.get("env")
        if isinstance(env, dict):
            for key, value in env.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    continue
                if re.search(r"(?i)(TOKEN|SECRET|PASSWORD|API_KEY)", key) and not _is_placeholder(value):
                    _finding(
                        findings,
                        target=target,
                        path=path,
                        line=line_number,
                        severity="high",
                        category="mcp",
                        title="MCP hardcoded environment secret",
                        evidence=_redact_secret_evidence(f"{server_name}.env.{key}={value}"),
                        suggestion="Load MCP secrets from local environment or secret storage instead of checked-in config.",
                        classification=classification,
                    )
        url = server.get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="mcp",
                title="Remote MCP transport",
                evidence=f"{server_name}: url={url}",
                suggestion="Prefer local MCP servers, pin remote hosts, and document authentication boundaries.",
                classification=classification,
            )
        if _server_timeout(server) is None:
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="low",
                category="mcp",
                title="MCP server missing timeout",
                evidence=f"{server_name}: timeout unset",
                suggestion="Set an explicit MCP startup or request timeout so hung servers fail predictably.",
                classification=classification,
            )


def _is_harness_wiring_document(path: Path, target: Path) -> bool:
    rel = path.relative_to(target)
    parts = rel.parts
    if path.suffix.lower() != ".json":
        return False
    if parts and parts[0] == ".brigade":
        return path.name == "handoff-sources.json" or (len(parts) >= 2 and parts[1] in {"hermes", "openclaw"})
    return bool(
        parts
        and (
            parts[0] in HARNESS_ROOTS
            or parts[0] == "templates"
            or (len(parts) >= 4 and parts[:3] == ("src", "brigade", "templates"))
        )
    )


def _scan_harness_wiring_document(
    findings: list[dict[str, Any]], *, target: Path, path: Path, text: str, classification: FileClassification
) -> None:
    if not _is_harness_wiring_document(path, target):
        return
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return
    _scan_harness_value(
        findings,
        target=target,
        path=path,
        text=text,
        value=data,
        key_path=(),
        classification=classification,
    )


def _scan_harness_value(
    findings: list[dict[str, Any]],
    *,
    target: Path,
    path: Path,
    text: str,
    value: object,
    key_path: tuple[str, ...],
    classification: FileClassification,
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                continue
            if key.startswith("_"):
                continue
            _scan_harness_value(
                findings,
                target=target,
                path=path,
                text=text,
                value=child,
                key_path=key_path + (key,),
                classification=classification,
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_harness_value(
                findings,
                target=target,
                path=path,
                text=text,
                value=child,
                key_path=key_path + (str(index),),
                classification=classification,
            )
    elif isinstance(value, str):
        _scan_harness_string(
            findings,
            target=target,
            path=path,
            text=text,
            value=value,
            key_path=key_path,
            classification=classification,
        )


def _scan_harness_string(
    findings: list[dict[str, Any]],
    *,
    target: Path,
    path: Path,
    text: str,
    value: str,
    key_path: tuple[str, ...],
    classification: FileClassification,
) -> None:
    if _is_placeholder(value):
        return
    key = _harness_semantic_key(key_path)
    if key in HARNESS_PATH_KEYS:
        _scan_harness_path_value(
            findings,
            target=target,
            path=path,
            text=text,
            value=value,
            key_path=key_path,
            classification=classification,
        )
    if key in HARNESS_COMMAND_KEYS:
        _scan_harness_command_value(
            findings,
            target=target,
            path=path,
            text=text,
            value=value,
            key_path=key_path,
            classification=classification,
        )
    if key in HARNESS_URL_KEYS or value.startswith(("http://", "https://")):
        _scan_harness_url_value(
            findings,
            target=target,
            path=path,
            text=text,
            value=value,
            key_path=key_path,
            classification=classification,
        )


def _harness_semantic_key(key_path: tuple[str, ...]) -> str:
    for key in reversed(key_path):
        if key.isdigit():
            continue
        return key
    return ""


def _harness_evidence(key_path: tuple[str, ...], value: str) -> str:
    return f"{'.'.join(key_path)}: {value}"


def _harness_line_number(text: str, value: str, key_path: tuple[str, ...]) -> int:
    if value:
        line = _line_number_for(text, json.dumps(value))
        if line != 1:
            return line
        line = _line_number_for(text, value)
        if line != 1:
            return line
    key = _harness_semantic_key(key_path)
    return _line_number_for(text, json.dumps(key) if key else "")


def _scan_harness_path_value(
    findings: list[dict[str, Any]],
    *,
    target: Path,
    path: Path,
    text: str,
    value: str,
    key_path: tuple[str, ...],
    classification: FileClassification,
) -> None:
    if value.startswith(("http://", "https://")):
        return
    normalized = value.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    line_number = _harness_line_number(text, value, key_path)
    evidence = _harness_evidence(key_path, value)
    if value in {"~", "$HOME", "/", "/home", "/Users"}:
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="medium",
            category="permissions",
            title="Harness wiring uses broad filesystem path",
            evidence=evidence,
            suggestion="Scope agent and harness paths to explicit repo-local directories or reviewed config files.",
            classification=classification,
        )
    if Path(value).is_absolute() or TEMPLATE_PRIVATE_PATH_RE.search(value):
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="high",
            category="permissions",
            title="Harness wiring contains host-private absolute path",
            evidence=evidence,
            suggestion="Use repo-relative paths, placeholders, or environment variables instead of host-private absolute paths.",
            classification=classification,
        )
    if ".." in parts:
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="high",
            category="permissions",
            title="Harness wiring path escapes target",
            evidence=evidence,
            suggestion="Remove '..' path traversal from harness wiring and keep generated paths under the target workspace.",
            classification=classification,
        )


def _scan_harness_command_value(
    findings: list[dict[str, Any]],
    *,
    target: Path,
    path: Path,
    text: str,
    value: str,
    key_path: tuple[str, ...],
    classification: FileClassification,
) -> None:
    if REMOTE_SHELL_RE.search(value):
        _finding(
            findings,
            target=target,
            path=path,
            line=_harness_line_number(text, value, key_path),
            severity="high",
            category="automation",
            title="Harness wiring pipes remote content into shell",
            evidence=_harness_evidence(key_path, value),
            suggestion="Replace remote shell bootstrap commands with checked-in, pinned, and reviewed setup steps.",
            classification=classification,
        )
    elif MCP_SHELL_META_RE.search(value):
        _finding(
            findings,
            target=target,
            path=path,
            line=_harness_line_number(text, value, key_path),
            severity="medium",
            category="automation",
            title="Harness wiring command contains shell metacharacter",
            evidence=_harness_evidence(key_path, value),
            suggestion="Pass structured arguments through harness config instead of shell-expanded command strings.",
            classification=classification,
        )


def _scan_harness_url_value(
    findings: list[dict[str, Any]],
    *,
    target: Path,
    path: Path,
    text: str,
    value: str,
    key_path: tuple[str, ...],
    classification: FileClassification,
) -> None:
    if not value.startswith(("http://", "https://")):
        return
    if HARNESS_ALLOWED_URL_RE.match(value):
        return
    parsed = urlparse(value)
    category = "supply-chain"
    title = "Harness wiring references remote URL"
    severity = "medium"
    suggestion = "Keep remote harness endpoints explicit, reviewed, and documented; prefer local or placeholder URLs in public templates."
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "0.0.0.0"}:
        severity = "high"
        title = "Harness wiring references insecure remote URL"
        suggestion = "Use HTTPS for remote harness endpoints or replace the URL with a placeholder."
    if TEMPLATE_PRIVATE_URL_RE.search(value):
        title = "Harness wiring contains private-looking URL"
        suggestion = "Replace private hostnames with placeholders before committing harness wiring."
    _finding(
        findings,
        target=target,
        path=path,
        line=_harness_line_number(text, value, key_path),
        severity=severity,
        category=category,
        title=title,
        evidence=_harness_evidence(key_path, value),
        suggestion=suggestion,
        classification=classification,
    )


def _first_npx_package(args: list[object]) -> str | None:
    skip_next = False
    for arg in args:
        if not isinstance(arg, str):
            continue
        if skip_next:
            skip_next = False
            continue
        if arg in {"-y", "--yes", "--quiet"}:
            continue
        if arg in {"--package", "-p"}:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        return arg
    return None


def _scan_package_json(
    findings: list[dict[str, Any]], *, target: Path, path: Path, text: str, classification: FileClassification
) -> None:
    if path.name != "package.json":
        return
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return
    scripts = data.get("scripts", {})
    if not isinstance(scripts, dict):
        return
    for name, command in scripts.items():
        if not isinstance(name, str) or not isinstance(command, str):
            continue
        line_number = _line_number_for(text, f'"{name}"')
        evidence = f"scripts.{name}: {command}"
        if REMOTE_SHELL_RE.search(command):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="high",
                category="supply-chain",
                title="Package script pipes remote content into shell",
                evidence=evidence,
                suggestion="Replace curl-to-shell package scripts with checked-in, pinned, and reviewed installer steps.",
                classification=classification,
            )
        if DESTRUCTIVE_RE.search(command):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="supply-chain",
                title="Package script contains destructive command",
                evidence=evidence,
                suggestion="Gate destructive package scripts behind explicit operator approval and document recovery steps.",
                classification=classification,
            )
        npx_match = UNPINNED_NPX_RE.search(command)
        if npx_match and "@" not in npx_match.group(1):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="supply-chain",
                title="Package script uses unpinned npx",
                evidence=evidence,
                suggestion="Pin npx package versions or move execution behind a reviewed lockfile.",
                classification=classification,
            )
        if ENV_DUMP_RE.search(command):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="high",
                category="supply-chain",
                title="Package script may leak environment",
                evidence=evidence,
                suggestion="Avoid dumping environment variables in package scripts, especially near network or file redirection.",
                classification=classification,
            )


def _scan_github_actions(
    findings: list[dict[str, Any]], *, target: Path, path: Path, text: str, classification: FileClassification
) -> None:
    rel = path.relative_to(target)
    if len(rel.parts) < 3 or rel.parts[0] != ".github" or rel.parts[1] != "workflows":
        return
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("pull_request_target:") or stripped == "- pull_request_target":
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="high",
                category="supply-chain",
                title="GitHub Actions uses pull_request_target",
                evidence=stripped,
                suggestion="Avoid pull_request_target for untrusted code paths or isolate it from checkout and secret access.",
                classification=classification,
            )
        if stripped.startswith("permissions: write-all"):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="high",
                category="supply-chain",
                title="GitHub Actions grants write-all permissions",
                evidence=stripped,
                suggestion="Use least-privilege workflow permissions instead of write-all.",
                classification=classification,
            )
        action_match = UNPINNED_ACTION_RE.search(stripped)
        if action_match:
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="supply-chain",
                title="GitHub Action missing pinned ref",
                evidence=stripped,
                suggestion="Pin actions to an immutable commit SHA or a reviewed release ref.",
                classification=classification,
            )
        pinned_match = PINNED_ACTION_RE.search(stripped)
        if pinned_match:
            ref = pinned_match.group(2)
            if ref.lower() in GITHUB_ACTION_FLOATING_REFS or (
                not ref.startswith("v") and not re.fullmatch(r"[a-fA-F0-9]{40}", ref)
            ):
                _finding(
                    findings,
                    target=target,
                    path=path,
                    line=line_number,
                    severity="medium",
                    category="supply-chain",
                    title="GitHub Action uses floating ref",
                    evidence=stripped,
                    suggestion="Pin GitHub Actions to immutable commit SHAs for release-sensitive workflows.",
                    classification=classification,
                )


def _scan_python_project(
    findings: list[dict[str, Any]], *, target: Path, path: Path, text: str, classification: FileClassification
) -> None:
    if path.name not in {"pyproject.toml", "setup.cfg", "requirements.txt"}:
        return
    current_section = ""
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        table_match = TOML_TABLE_RE.match(stripped) if path.name == "pyproject.toml" else None
        if table_match:
            current_section = table_match.group(1).lower()
            continue
        if PYTHON_URL_DEP_RE.search(stripped) and _python_url_dependency_candidate(
            path.name, current_section, stripped
        ):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="supply-chain",
                title="Python dependency uses URL source",
                evidence=stripped,
                suggestion="Prefer pinned package versions or reviewed immutable commit URLs for Python dependencies.",
                classification=classification,
            )
        if "setup_requires" in stripped or "dependency_links" in stripped:
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="supply-chain",
                title="Python project uses legacy install hook",
                evidence=stripped,
                suggestion="Avoid legacy install-time dependency hooks and move dependencies into static project metadata.",
                classification=classification,
            )


def _python_url_dependency_candidate(file_name: str, section: str, stripped: str) -> bool:
    if file_name in {"requirements.txt", "setup.cfg"}:
        return True
    if file_name != "pyproject.toml":
        return False
    if section.endswith(".urls") or section == "project.urls":
        return False
    if "dependencies" in section or section in {"build-system", "project"}:
        return True
    return stripped.startswith(("dependencies", "requires"))
