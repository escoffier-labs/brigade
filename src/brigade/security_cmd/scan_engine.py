"""Read-only security scanner for agent workspaces."""
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


def _rule_id(category: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return f"{category}.{slug or 'finding'}"


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
    if parts and parts[0] in HARNESS_ROOTS:
        return True
    if len(parts) >= 4 and parts[0] == "src" and parts[1] == "brigade" and parts[2] == "templates":
        return True
    if parts and parts[0] == "templates":
        return True
    return False


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


def _scan_path_selected(rel_path: str, *, include_paths: tuple[str, ...], exclude_paths: tuple[str, ...]) -> bool:
    if include_paths and not _path_matches_any(rel_path, include_paths):
        return False
    return not (exclude_paths and _path_matches_any(rel_path, exclude_paths))


def _candidate_scan_roots(target: Path, include_paths: tuple[str, ...]) -> list[Path]:
    if not include_paths:
        return [target]
    roots: list[Path] = []
    seen: set[Path] = set()
    for pattern in include_paths:
        clean = pattern.strip().replace("\\", "/").strip("/")
        if not clean:
            continue
        root = (target / clean).resolve()
        try:
            root.relative_to(target)
        except ValueError:
            continue
        if root.exists() and root not in seen:
            roots.append(root)
            seen.add(root)
    return sorted(roots)


def _should_scan_file(path: Path) -> bool:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return False
    try:
        return path.stat().st_size <= 500_000
    except OSError:
        return False


def _iter_scan_files(
    target: Path, *, include_paths: tuple[str, ...] = (), exclude_paths: tuple[str, ...] = ()
) -> list[Path]:
    paths: list[Path] = []
    for root in _candidate_scan_roots(target, include_paths):
        if root.is_file():
            rel = str(root.relative_to(target))
            rel_parts = root.relative_to(target).parts
            if (
                not _scan_path_selected(rel, include_paths=include_paths, exclude_paths=exclude_paths)
                or any(part in SKIP_DIRS for part in rel_parts)
                or any(rel_parts[: len(prefix)] == prefix for prefix in SKIP_PREFIXES)
                or not _should_scan_file(root)
            ):
                continue
            paths.append(root)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            current = Path(dirpath)
            try:
                rel_current = current.relative_to(target)
            except ValueError:
                dirnames[:] = []
                continue
            rel_parts = rel_current.parts
            if rel_parts and (
                any(part in SKIP_DIRS for part in rel_parts)
                or any(rel_parts[: len(prefix)] == prefix for prefix in SKIP_PREFIXES)
                or not _scan_path_selected(str(rel_current), include_paths=include_paths, exclude_paths=exclude_paths)
            ):
                dirnames[:] = []
                continue
            kept_dirs: list[str] = []
            for dirname in sorted(dirnames):
                child = current / dirname
                try:
                    child_rel = child.relative_to(target)
                except ValueError:
                    continue
                child_parts = child_rel.parts
                if (
                    dirname in SKIP_DIRS
                    or any(child_parts[: len(prefix)] == prefix for prefix in SKIP_PREFIXES)
                    or not _scan_path_selected(str(child_rel), include_paths=include_paths, exclude_paths=exclude_paths)
                ):
                    continue
                kept_dirs.append(dirname)
            dirnames[:] = kept_dirs
            for filename in sorted(filenames):
                path = current / filename
                rel = str(path.relative_to(target))
                if _scan_path_selected(
                    rel,
                    include_paths=include_paths,
                    exclude_paths=exclude_paths,
                ) and _should_scan_file(path):
                    paths.append(path)
    paths.sort()
    unique_paths: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path in seen:
            continue
        unique_paths.append(path)
        seen.add(path)
    return unique_paths


def _surface_for(path: Path, target: Path) -> str:
    rel = path.relative_to(target)
    parts = rel.parts
    if _is_session_chat_path(path, target):
        return "session-chat"
    if "memory-handoffs" in parts:
        return "handoff-inbox"
    if "skills" in parts and path.name == "SKILL.md":
        return "skill"
    if "commands" in parts and path.suffix.lower() == ".md":
        return "slash-command"
    if ("agents" in parts or "subagents" in parts) and path.suffix.lower() == ".md":
        return "subagent"
    if any(part in {"wrappers", "tools", "tool-wrappers"} for part in parts) and path.suffix.lower() in {
        ".sh",
        ".py",
        ".js",
        ".ts",
        ".toml",
        ".json",
        ".md",
    }:
        return "tool-wrapper"
    if parts and parts[0] == ".brigade":
        if path.name == "tools.toml" or "tools" in parts:
            return "tool-wrapper"
        return "brigade"
    if parts and parts[0] == ".codex":
        if "skills" in parts:
            return "skill"
        return "codex"
    if parts and parts[0] == ".claude":
        if "commands" in parts:
            return "slash-command"
        if "agents" in parts or "subagents" in parts:
            return "subagent"
        return "claude"
    if "mcp" in path.name.lower():
        return "mcp"
    if parts and parts[0] in {"hooks", "scripts"}:
        if any(part in {"wrappers", "tools", "tool-wrappers"} for part in parts):
            return "tool-wrapper"
        return "automation"
    if path.name in {"AGENTS.md", "CLAUDE.md", "SAFETY_RULES.md", "INSTALL_FOR_AGENTS.md"}:
        return "agent-instructions"
    return "repo"


def _confidence_for(path: Path, target: Path) -> str:
    rel = path.relative_to(target)
    parts = rel.parts
    if _is_session_chat_path(path, target):
        return "runtime"
    if parts and parts[0] == "src" and "templates" in parts:
        return "template"
    if parts and parts[0] in {".brigade", ".claude", ".codex", "hooks", "scripts"}:
        return "runtime"
    if path.name in {"AGENTS.md", "CLAUDE.md", "SAFETY_RULES.md", "INSTALL_FOR_AGENTS.md"}:
        return "runtime"
    return "repo"


def _classification_for(path: Path, target: Path) -> FileClassification:
    return FileClassification(surface=_surface_for(path, target), confidence=_confidence_for(path, target))


def _is_session_chat_path(path: Path, target: Path) -> bool:
    try:
        rel = path.relative_to(target)
    except ValueError:
        rel = path
    parts = {part.lower() for part in rel.parts}
    if parts & SESSION_CHAT_PARTS:
        return True
    name = path.name.lower()
    return any(token in name for token in ("chat", "conversation", "session", "transcript"))


def _secret_response_options(path: Path, target: Path) -> list[str]:
    options = [
        "move_to_env: Store active app secrets in a gitignored .env file or environment variable, then commit only a placeholder.",
        "scrub_or_rotate: Remove the unredacted value from tracked files and rotate the credential if it was committed, shared, or exposed.",
        "keepass_review: Show the redacted finding to the operator so they can save the real value in KeePass before deciding what to remove.",
    ]
    if _is_session_chat_path(path, target):
        options.insert(
            1,
            "scrub_session_chat: Redact, archive, or delete the session/chat transcript before sharing or syncing the workspace.",
        )
    return options


def _fingerprint(*, category: str, title: str, rel_path: Path, line: int, evidence: str) -> str:
    stable = "\n".join([category, title, str(rel_path), str(line), _short(evidence, limit=96)])
    return hashlib.sha256(stable.encode()).hexdigest()[:16]


def _finding(
    findings: list[dict[str, Any]],
    *,
    target: Path,
    path: Path,
    line: int,
    severity: str,
    category: str,
    title: str,
    evidence: str,
    suggestion: str,
    response_options: list[str] | None = None,
    classification: FileClassification | None = None,
) -> None:
    rel = path.relative_to(target)
    file_classification = classification or _classification_for(path, target)
    safe_excerpt = _short(evidence)
    fingerprint = _fingerprint(category=category, title=title, rel_path=rel, line=line, evidence=safe_excerpt)
    finding_id = f"security-{fingerprint}"
    findings.append(
        {
            "id": finding_id,
            "fingerprint": fingerprint,
            "rule_id": _rule_id(category, title),
            "severity": severity,
            "category": category,
            "title": title,
            "path": str(rel),
            "line": line,
            "surface": file_classification.surface,
            "confidence": file_classification.confidence,
            "evidence": safe_excerpt,
            "safe_excerpt": safe_excerpt,
            "suggestion": suggestion,
            "remediation_hint": suggestion,
            "response_options": response_options or [],
        }
    )


def _is_security_scanner_literal(path: Path, line: str) -> bool:
    if not path.as_posix().endswith("src/brigade/security_cmd.py"):
        return False
    stripped = line.strip()
    scanner_tokens = (
        "danger-full-access",
        "sandbox_permissions",
        "require_escalated",
        "npx package",
        "PLAINTEXT_PASSWORD_RE",
        "Environment dump or exfiltration pattern",
        "Plaintext password",
        "Possible hardcoded credential",
        "Possible sensitive secret material",
        "Session chat contains exposed credential",
    )
    if any(token in stripped for token in scanner_tokens):
        return True
    if stripped.startswith("suggestion=") or stripped.startswith("title="):
        return True
    if stripped.startswith(("password_match =", "password_emitted =")):
        return True
    return stripped.startswith("if ") and (
        '"danger-full-access"' in stripped or '"sandbox_permissions"' in stripped or '"require_escalated"' in stripped
    )


def _scan_line(
    findings: list[dict[str, Any]],
    *,
    target: Path,
    path: Path,
    line_number: int,
    line: str,
    classification: FileClassification | None = None,
) -> None:
    file_classification = classification or _classification_for(path, target)
    if _is_security_scanner_literal(path, line):
        return
    secret_match = SECRET_VALUE_RE.search(line)
    password_match = PLAINTEXT_PASSWORD_RE.search(line)
    password_emitted = bool(password_match and not _is_placeholder(password_match.group(2)))
    if password_emitted:
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="high",
            category="secrets",
            title="Plaintext password",
            evidence=_redact_secret_evidence(line),
            suggestion="Move the password into local secret storage or a gitignored environment file, then scrub the raw value from shared files.",
            classification=file_classification,
            response_options=_secret_response_options(path, target),
        )
    if secret_match and not password_emitted and not _is_placeholder(secret_match.group(2)):
        session_chat = _is_session_chat_path(path, target)
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="high",
            category="secrets",
            title="Session chat contains exposed credential" if session_chat else "Possible hardcoded credential",
            evidence=_redact_secret_evidence(line),
            suggestion=(
                "Redact or archive the session transcript, rotate the credential if real, and move active use to .env, environment variables, or KeePass."
                if session_chat
                else "Move the value into local environment or secret storage and commit only a placeholder."
            ),
            response_options=_secret_response_options(path, target),
            classification=file_classification,
        )
    if _contains_private_key_material(line) or (
        ENV_ASSIGNMENT_RE.search(line) and not password_emitted and not _is_placeholder(line)
    ):
        session_chat = _is_session_chat_path(path, target)
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="high",
            category="secrets",
            title="Session chat contains exposed credential" if session_chat else "Possible sensitive secret material",
            evidence=_redact_secret_evidence(line),
            suggestion=(
                "Redact or archive the session transcript, rotate the credential if real, and move active use to .env, environment variables, or KeePass."
                if session_chat
                else "Remove secret material from the repo and rotate the credential if it was real."
            ),
            response_options=_secret_response_options(path, target),
            classification=file_classification,
        )
    if "danger-full-access" in line or "sandbox_permissions" in line and "require_escalated" in line:
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="medium",
            category="permissions",
            title="Broad agent execution permission",
            evidence=line,
            suggestion="Prefer read-only or workspace-scoped execution unless this is an explicitly trusted local path.",
            classification=file_classification,
        )
    if REMOTE_SHELL_RE.search(line):
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="high",
            category="automation",
            title="Remote script piped into shell",
            evidence=line,
            suggestion="Pin and verify downloaded scripts before execution, or replace with a checked-in script.",
            classification=file_classification,
        )
    if DESTRUCTIVE_RE.search(line):
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="medium",
            category="automation",
            title="Destructive command pattern",
            evidence=line,
            suggestion="Gate destructive commands behind explicit operator approval and document recovery steps.",
            classification=file_classification,
        )
    if ENV_DUMP_RE.search(line):
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="high",
            category="secrets",
            title="Environment dump or exfiltration pattern",
            evidence=_redact_secret_evidence(line),
            suggestion="Avoid dumping environment variables near file redirection or network commands.",
            classification=file_classification,
            response_options=_secret_response_options(path, target),
        )
    npx_match = UNPINNED_NPX_RE.search(line)
    if npx_match and "@" not in npx_match.group(1):
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="medium",
            category="supply-chain",
            title="Unpinned remote package execution",
            evidence=line,
            suggestion="Pin remote package versions or install through a reviewed lockfile.",
            classification=file_classification,
        )
    if "mcp" in path.name.lower() or '"mcpServers"' in line:
        if HTTP_MCP_RE.search(line):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="mcp",
                title="Remote MCP transport",
                evidence=line,
                suggestion="Prefer local MCP servers, pin remote hosts, and document authentication boundaries.",
                classification=file_classification,
            )
        if AUTO_APPROVE_RE.search(line):
            _finding(
                findings,
                target=target,
                path=path,
                line=line_number,
                severity="medium",
                category="mcp",
                title="MCP auto-approval pattern",
                evidence=line,
                suggestion="Avoid blanket auto-approval and require review for mutable or networked tools.",
                classification=file_classification,
            )
    if file_classification.surface in {
        "agent-instructions",
        "claude",
        "codex",
        "repo",
        "skill",
        "slash-command",
        "subagent",
        "tool-wrapper",
    } and PROMPT_INJECTION_RE.search(line):
        _finding(
            findings,
            target=target,
            path=path,
            line=line_number,
            severity="low",
            category="prompt-injection",
            title="Prompt-injection style instruction",
            evidence=line,
            suggestion="Keep hostile examples clearly labeled as examples and avoid executable language in trusted instructions.",
            classification=file_classification,
        )


def _path_matches_any(rel_path: str, patterns: tuple[str, ...]) -> bool:
    normalized = rel_path.replace("\\", "/")
    for pattern in patterns:
        clean = pattern.strip().replace("\\", "/").strip("/")
        if not clean:
            continue
        if normalized == clean or normalized.startswith(clean.rstrip("/") + "/"):
            return True
    return False


def _severity_selected(finding: dict[str, Any], threshold: str) -> bool:
    return SEVERITY_ORDER.get(str(finding.get("severity")), 0) >= SEVERITY_ORDER.get(threshold, 0)


def _filter_findings(
    findings: list[dict[str, Any]],
    *,
    enabled_checks: tuple[str, ...],
    include_paths: tuple[str, ...],
    exclude_paths: tuple[str, ...],
    severity_threshold: str,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    enabled = set(enabled_checks)
    for finding in findings:
        path = str(finding.get("path") or "")
        if enabled and finding.get("category") not in enabled:
            continue
        if include_paths and not _path_matches_any(path, include_paths):
            continue
        if exclude_paths and _path_matches_any(path, exclude_paths):
            continue
        if not _severity_selected(finding, severity_threshold):
            continue
        selected.append(finding)
    return selected


def _scan_handoff_inboxes(
    findings: list[dict[str, Any]],
    *,
    target: Path,
    include_paths: tuple[str, ...] = (),
    exclude_paths: tuple[str, ...] = (),
) -> list[str]:
    """Screen pending handoff notes for injection signals.

    Handoff inboxes are excluded from the line scanner via SKIP_PREFIXES so
    untrusted note content is not attributed to the repo author. This pass
    reports the same content through the untrusted-context lens instead:
    a pending note carrying injection-style instructions should be reviewed
    before any ingester reads it. `processed/` and TEMPLATE.md are skipped.
    """
    scanned: list[str] = []
    for inbox_rel in sorted(set(WRITER_INBOXES.values())):
        inbox = target / inbox_rel
        if not inbox.is_dir():
            continue
        for path in sorted(inbox.glob("*.md")):
            if path.name == "TEMPLATE.md":
                continue
            rel = str(path.relative_to(target))
            if not _scan_path_selected(rel, include_paths=include_paths, exclude_paths=exclude_paths):
                continue
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            scanned.append(rel)
            signal = scan_untrusted(text)
            if signal.flagged:
                classification = _classification_for(path, target)
                _finding(
                    findings,
                    target=target,
                    path=path,
                    line=1,
                    severity="medium",
                    category="handoff-injection",
                    title="Pending handoff carries prompt-injection signals",
                    evidence=signal.markers[0] if signal.markers else "injection signal",
                    suggestion="Review this handoff before ingest; do not let an ingester auto-promote it.",
                    classification=classification,
                )
    return scanned


def scan_target(
    target: Path,
    *,
    include_templates: bool = False,
    suppressions: tuple[str, ...] = (),
    enabled_checks: tuple[str, ...] = SECURITY_CHECKS,
    include_paths: tuple[str, ...] = (),
    exclude_paths: tuple[str, ...] = (),
    severity_threshold: str = "low",
) -> dict[str, Any]:
    target = target.expanduser().resolve()
    findings: list[dict[str, Any]] = []
    scanned_files: list[str] = []
    scanned_files.extend(
        _scan_handoff_inboxes(
            findings,
            target=target,
            include_paths=include_paths,
            exclude_paths=exclude_paths,
        )
    )
    for path in _iter_scan_files(target, include_paths=include_paths, exclude_paths=exclude_paths):
        classification = _classification_for(path, target)
        if not include_templates and classification.confidence == "template":
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        scanned_files.append(str(path.relative_to(target)))
        for line_number, line in enumerate(text.splitlines(), start=1):
            _scan_line(
                findings,
                target=target,
                path=path,
                line_number=line_number,
                line=line,
                classification=classification,
            )
        _scan_mcp_document(findings, target=target, path=path, text=text, classification=classification)
        _scan_harness_wiring_document(findings, target=target, path=path, text=text, classification=classification)
        _scan_package_json(findings, target=target, path=path, text=text, classification=classification)
        _scan_github_actions(findings, target=target, path=path, text=text, classification=classification)
        _scan_python_project(findings, target=target, path=path, text=text, classification=classification)
    findings = _filter_findings(
        findings,
        enabled_checks=enabled_checks,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        severity_threshold=severity_threshold,
    )
    suppressed = [finding for finding in findings if finding.get("fingerprint") in suppressions]
    findings = [finding for finding in findings if finding.get("fingerprint") not in suppressions]
    counts: dict[str, int] = {}
    for finding in findings:
        severity = str(finding["severity"])
        counts[severity] = counts.get(severity, 0) + 1
    return {
        "target": str(target),
        "scanned_files": scanned_files,
        "scanned_file_count": len(scanned_files),
        "finding_count": len(findings),
        "suppressed_count": len(suppressed),
        "severity_counts": dict(sorted(counts.items())),
        "findings": findings,
        "suppressed_findings": suppressed,
    }


def _should_fail(findings: list[dict[str, Any]], fail_on: str) -> bool:
    if fail_on == "none":
        return False
    threshold = SEVERITY_ORDER[fail_on]
    return any(SEVERITY_ORDER.get(str(item.get("severity")), 0) >= threshold for item in findings)


def _import_findings(
    target: Path,
    findings: list[dict[str, Any]],
    *,
    evidence_path: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    existing_fingerprints = {
        str(item.get("metadata", {}).get("fingerprint"))
        for item in work_cmd._pending_imports(target)
        if isinstance(item, dict)
        and item.get("source") == "security-scan"
        and isinstance(item.get("metadata"), dict)
        and item.get("metadata", {}).get("fingerprint")
    }
    records = []
    skipped: list[dict[str, Any]] = []
    for finding in findings:
        fingerprint = str(finding.get("fingerprint") or "")
        if fingerprint and fingerprint in existing_fingerprints:
            skipped.append(finding)
            continue
        path = finding.get("path")
        line = finding.get("line")
        title = finding.get("title")
        severity = finding.get("severity")
        category = finding.get("category")
        kind = "incident" if SEVERITY_ORDER.get(str(severity), 0) >= SEVERITY_ORDER["high"] else "finding"
        acceptance = [
            f"`brigade security findings` no longer reports {finding.get('id')}.",
            "The mitigation or suppression reason is documented without exposing secret values.",
        ]
        response_options = finding.get("response_options")
        if isinstance(response_options, list) and response_options:
            acceptance.append(
                "The chosen response path is recorded, such as .env storage, scrub/rotate, KeePass review, transcript redaction, or accepted local risk."
            )
        records.append(
            {
                "text": f"Review security finding [{severity}] {category} in {path}:{line}: {title}",
                "kind": kind,
                "source": "security-scan",
                "type": "security",
                "priority": "high" if kind == "incident" else "normal",
                "template": "security-follow-up",
                "acceptance": acceptance,
                "metadata": {
                    "finding_id": finding.get("id"),
                    "rule_id": finding.get("rule_id"),
                    "issue_type": category,
                    "fingerprint": finding.get("fingerprint"),
                    "source_item_key": f"security-scan:{finding.get('fingerprint')}",
                    "source_fingerprint": work_cmd._stable_hash(
                        {
                            "rule_id": finding.get("rule_id"),
                            "fingerprint": finding.get("fingerprint"),
                            "severity": severity,
                            "path": path,
                            "line": line,
                            "safe_excerpt": finding.get("safe_excerpt") or finding.get("evidence"),
                        }
                    ),
                    "severity": severity,
                    "category": category,
                    "safe_summary": f"[{severity}] {category}: {title} on {finding.get('surface') or 'repo'}",
                    "path": path,
                    "line": line,
                    "surface": finding.get("surface"),
                    "confidence": finding.get("confidence"),
                    "safe_detail": finding.get("safe_excerpt") or finding.get("evidence"),
                    "remediation_hint": finding.get("remediation_hint") or finding.get("suggestion"),
                    "response_options": response_options if isinstance(response_options, list) else [],
                    "local_evidence_path": str(evidence_path) if evidence_path is not None else None,
                },
            }
        )
        if fingerprint:
            existing_fingerprints.add(fingerprint)
    imported, duplicate_records, dismissed_records = work_cmd._append_import_records(target, records)
    skipped.extend(duplicate_records)
    skipped.extend(dismissed_records)
    return imported, skipped


def _render_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Brigade Security Report",
        "",
        f"- target: `{report['target']}`",
        f"- generated_at: `{report['generated_at']}`",
        f"- policy: `{report['policy']}`",
        f"- fail_on: `{report['fail_on']}`",
        f"- include_templates: `{report['include_templates']}`",
        f"- scanned_files: `{report['scanned_file_count']}`",
        f"- findings: `{report['finding_count']}`",
        f"- suppressed: `{report['suppressed_count']}`",
        "",
        "## Severity Counts",
        "",
    ]
    if report["severity_counts"]:
        for severity, count in report["severity_counts"].items():
            lines.append(f"- {severity}: {count}")
    else:
        lines.append("- none: 0")
    lines.extend(["", "## Findings", ""])
    if not report["findings"]:
        lines.append("No unsuppressed findings.")
    for finding in report["findings"]:
        finding_lines = [
            f"### {finding['id']} - {finding['title']}",
            "",
            f"- fingerprint: `{finding['fingerprint']}`",
            f"- severity: `{finding['severity']}`",
            f"- category: `{finding['category']}`",
            f"- path: `{finding['path']}:{finding['line']}`",
            f"- surface: `{finding['surface']}`",
            f"- confidence: `{finding['confidence']}`",
            f"- evidence: `{finding['evidence']}`",
            f"- suggestion: {finding['suggestion']}",
        ]
        response_options = finding.get("response_options") or []
        if response_options:
            finding_lines.append("- response_options:")
            finding_lines.extend(f"  - {item}" for item in response_options)
        finding_lines.append("")
        lines.extend(finding_lines)
    return "\n".join(lines).rstrip() + "\n"


def _sarif_level(severity: object) -> str:
    rendered = str(severity or "").lower()
    if rendered in {"critical", "high"}:
        return "error"
    if rendered == "medium":
        return "warning"
    return "note"


def _sarif_report(report: dict[str, Any]) -> dict[str, Any]:
    rules: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    for finding in report.get("findings", []):
        if not isinstance(finding, dict):
            continue
        rule_id = str(finding.get("rule_id") or finding.get("category") or "brigade.security")
        if rule_id not in rules:
            rules[rule_id] = {
                "id": rule_id,
                "name": str(finding.get("title") or rule_id),
                "shortDescription": {"text": str(finding.get("title") or rule_id)},
                "fullDescription": {
                    "text": str(
                        finding.get("remediation_hint") or finding.get("suggestion") or finding.get("title") or rule_id
                    )
                },
                "properties": {
                    "category": finding.get("category"),
                    "severity": finding.get("severity"),
                },
            }
        region: dict[str, Any] = {}
        try:
            line = int(finding.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        if line > 0:
            region["startLine"] = line
        results.append(
            {
                "ruleId": rule_id,
                "level": _sarif_level(finding.get("severity")),
                "message": {
                    "text": str(
                        finding.get("title") or finding.get("safe_excerpt") or finding.get("evidence") or rule_id
                    )
                },
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": str(finding.get("path") or "")},
                            **({"region": region} if region else {}),
                        }
                    }
                ],
                "partialFingerprints": {
                    "primaryLocationLineHash": str(finding.get("fingerprint") or finding.get("id") or "")
                },
                "properties": {
                    "id": finding.get("id"),
                    "fingerprint": finding.get("fingerprint"),
                    "severity": finding.get("severity"),
                    "category": finding.get("category"),
                    "confidence": finding.get("confidence"),
                    "surface": finding.get("surface"),
                    "safe_excerpt": finding.get("safe_excerpt") or finding.get("evidence"),
                    "remediation_hint": finding.get("remediation_hint") or finding.get("suggestion"),
                    "response_options": finding.get("response_options") or [],
                },
            }
        )
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "Brigade Security",
                        "informationUri": "https://github.com/escoffier-labs/brigade",
                        "rules": list(rules.values()),
                    }
                },
                "results": results,
                "properties": {
                    "target": report.get("target"),
                    "generated_at": report.get("generated_at"),
                    "policy": report.get("policy"),
                    "finding_count": report.get("finding_count"),
                },
            }
        ],
    }


def write_evidence_bundle(report: dict[str, Any], output_dir: Path) -> Path:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = dict(report)
    report["artifacts"] = str(output_dir)
    (output_dir / "security-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    (output_dir / "security-report.md").write_text(_render_markdown_report(report))
    (output_dir / "security-report.sarif").write_text(
        json.dumps(_sarif_report(report), indent=2, sort_keys=True) + "\n"
    )
    return output_dir


def _config_payload(target: Path) -> dict[str, Any]:
    config = load_config(target)
    path = config_path(target)
    if config is None:
        return {"config_path": str(path), "configured": False, "config": None}
    return {
        "config_path": str(path),
        "configured": True,
        "config": {
            "policy": config.policy,
            "scan_profile": config.scan_profile,
            "fail_on": config.fail_on or POLICIES[config.policy]["fail_on"],
            "include_templates": config.include_templates
            if config.include_templates is not None
            else POLICIES[config.policy]["include_templates"],
            "enabled_checks": list(config.enabled_checks),
            "include_paths": list(config.include_paths),
            "exclude_paths": list(config.exclude_paths),
            "severity_threshold": config.severity_threshold,
            "output_path": config.output_path,
            "suppressions": list(config.suppressions),
            "suppression_reasons": config.suppression_reasons,
            "enrichment": {
                "provider": config.enrichment.provider,
                "misp_url_configured": bool(config.enrichment.misp_url),
                "misp_api_key_env": config.enrichment.misp_api_key_env,
                "timeout_seconds": config.enrichment.timeout_seconds,
                "cache_path": config.enrichment.cache_path,
            },
        },
    }
