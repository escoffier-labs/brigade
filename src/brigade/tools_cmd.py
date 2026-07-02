"""Local portable tool and skill catalog inspection."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import signal
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import toml_compat as tomllib
from .config import load_config as load_brigade_config
from .install import apply_gitignore
from .selection import Selection
from .localio import stable_hash as _stable_hash, utc_now as _now, write_json as _write_json

OK = "ok"
WARN = "warn"
FAIL = "fail"
CONFIG_REL_PATH = ".brigade/tools.toml"
CALLS_REL_PATH = ".brigade/tools/calls.jsonl"
RUNS_REL_PATH = ".brigade/tools/runs"
CHECKPOINTS_REL_PATH = ".brigade/tools/checkpoints"
RUNTIMES_REL_PATH = ".brigade/tools/runtimes.toml"
RUNTIME_STATE_REL_PATH = ".brigade/tools/runtime"
POLICY_REL_PATH = ".brigade/tools/policy.toml"
PARITY_CLOSEOUTS_REL_PATH = ".brigade/tools/parity-closeouts"
HEALTH_STALE_HOURS = 48
CALL_STALE_HOURS = 72
CALL_RUNNING_STALE_HOURS = 2
PROJECTION_MARKER = "brigade-tool-projection:"
FAMILIES = ("skill", "slash-command", "superpower", "mcp", "openapi", "graphql", "script", "custom")
KNOWN_HARNESSES = (
    "claude",
    "codex",
    "opencode",
    "antigravity",
    "pi",
    "cursor",
    "aider",
    "goose",
    "continue",
    "copilot",
    "qwen",
    "kimi",
    "adal",
    "openhands",
    "grok",
    "amp",
    "crush",
    "hermes",
    "openclaw",
    "mcp",
    "scripts",
)
PARITY_ISSUE_TYPES = {
    "parity_gap",
    "missing_projection",
    "stale_projection",
    "conflicted_projection",
    "unmanaged_projection",
}
APPROVAL_MODES = ("never", "on-request", "always")
SCHEMA_TYPES = ("object", "array", "string", "number", "integer", "boolean", "null")
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
        "supported_harnesses": [
            "claude",
            "codex",
            "opencode",
            "antigravity",
            "pi",
            "cursor",
            "aider",
            "goose",
            "continue",
            "copilot",
            "qwen",
            "kimi",
            "adal",
            "openhands",
            "grok",
            "amp",
            "crush",
            "hermes",
            "openclaw",
            "mcp",
            "scripts",
        ],
        "projections": {
            "claude": ".claude/commands/simplify.md",
            "codex": ".codex/skills/simplify/SKILL.md",
            "opencode": ".opencode/commands/simplify.md",
            "antigravity": ".antigravity/commands/simplify.md",
            "pi": ".pi/commands/simplify.md",
            "cursor": ".cursor/rules/simplify.md",
            "aider": ".aider/commands/simplify.md",
            "goose": ".goose/commands/simplify.md",
            "continue": ".continue/rules/simplify.md",
            "copilot": ".copilot/instructions/simplify.md",
            "qwen": ".qwen/commands/simplify.md",
            "kimi": ".kimi/commands/simplify.md",
            "adal": ".adal/commands/simplify.md",
            "openhands": ".openhands/instructions/simplify.md",
            "grok": ".grok/instructions/simplify.md",
            "amp": ".amp/instructions/simplify.md",
            "crush": ".crush/instructions/simplify.md",
            "hermes": ".hermes/commands/simplify.md",
            "openclaw": ".openclaw/commands/simplify.md",
            "mcp": ".mcp/simplify.md",
            "scripts": "scripts/simplify.md",
        },
    },
    {
        "id": "superpowers",
        "name": "Superpowers",
        "family": "superpower",
        "enabled": True,
        "description": "Portable superpowers placeholder.",
        "source_path": "tools/superpowers.md",
        "supported_harnesses": [
            "claude",
            "codex",
            "opencode",
            "antigravity",
            "pi",
            "cursor",
            "aider",
            "goose",
            "continue",
            "copilot",
            "qwen",
            "kimi",
            "adal",
            "openhands",
            "grok",
            "amp",
            "crush",
            "hermes",
            "openclaw",
            "mcp",
            "scripts",
        ],
        "projections": {
            "claude": ".claude/commands/superpowers.md",
            "codex": ".codex/skills/superpowers/SKILL.md",
            "opencode": ".opencode/superpowers/superpowers.md",
            "antigravity": ".antigravity/superpowers/superpowers.md",
            "pi": ".pi/superpowers/superpowers.md",
            "cursor": ".cursor/rules/superpowers.md",
            "aider": ".aider/commands/superpowers.md",
            "goose": ".goose/commands/superpowers.md",
            "continue": ".continue/rules/superpowers.md",
            "copilot": ".copilot/instructions/superpowers.md",
            "qwen": ".qwen/commands/superpowers.md",
            "kimi": ".kimi/commands/superpowers.md",
            "adal": ".adal/commands/superpowers.md",
            "openhands": ".openhands/instructions/superpowers.md",
            "grok": ".grok/instructions/superpowers.md",
            "amp": ".amp/instructions/superpowers.md",
            "crush": ".crush/instructions/superpowers.md",
            "hermes": ".hermes/superpowers/superpowers.md",
            "openclaw": ".openclaw/superpowers/superpowers.md",
            "mcp": ".mcp/superpowers.md",
            "scripts": "scripts/superpowers.md",
        },
    },
    {
        "id": "frontend",
        "name": "Frontend",
        "family": "skill",
        "enabled": True,
        "description": "Frontend implementation and visual quality workflow.",
        "source_path": "tools/frontend.md",
        "supported_harnesses": [
            "claude",
            "codex",
            "opencode",
            "antigravity",
            "pi",
            "cursor",
            "aider",
            "goose",
            "continue",
            "copilot",
            "qwen",
            "kimi",
            "adal",
            "openhands",
            "grok",
            "amp",
            "crush",
            "hermes",
            "openclaw",
            "mcp",
            "scripts",
        ],
        "projections": {
            "claude": ".claude/commands/frontend.md",
            "codex": ".codex/skills/frontend/SKILL.md",
            "opencode": ".opencode/commands/frontend.md",
            "antigravity": ".antigravity/commands/frontend.md",
            "pi": ".pi/commands/frontend.md",
            "cursor": ".cursor/rules/frontend.md",
            "aider": ".aider/commands/frontend.md",
            "goose": ".goose/commands/frontend.md",
            "continue": ".continue/rules/frontend.md",
            "copilot": ".copilot/instructions/frontend.md",
            "qwen": ".qwen/commands/frontend.md",
            "kimi": ".kimi/commands/frontend.md",
            "adal": ".adal/commands/frontend.md",
            "openhands": ".openhands/instructions/frontend.md",
            "grok": ".grok/instructions/frontend.md",
            "amp": ".amp/instructions/frontend.md",
            "crush": ".crush/instructions/frontend.md",
            "hermes": ".hermes/commands/frontend.md",
            "openclaw": ".openclaw/commands/frontend.md",
            "mcp": ".mcp/frontend.md",
            "scripts": "scripts/frontend.md",
        },
    },
    {
        "id": "antislop",
        "name": "Anti-Slop",
        "family": "skill",
        "enabled": True,
        "description": "Quality review workflow for removing vague or unfinished work.",
        "source_path": "tools/antislop.md",
        "supported_harnesses": [
            "claude",
            "codex",
            "opencode",
            "antigravity",
            "pi",
            "cursor",
            "aider",
            "goose",
            "continue",
            "copilot",
            "qwen",
            "kimi",
            "adal",
            "openhands",
            "grok",
            "amp",
            "crush",
            "hermes",
            "openclaw",
            "mcp",
            "scripts",
        ],
        "projections": {
            "claude": ".claude/commands/antislop.md",
            "codex": ".codex/skills/antislop/SKILL.md",
            "opencode": ".opencode/commands/antislop.md",
            "antigravity": ".antigravity/commands/antislop.md",
            "pi": ".pi/commands/antislop.md",
            "cursor": ".cursor/rules/antislop.md",
            "aider": ".aider/commands/antislop.md",
            "goose": ".goose/commands/antislop.md",
            "continue": ".continue/rules/antislop.md",
            "copilot": ".copilot/instructions/antislop.md",
            "qwen": ".qwen/commands/antislop.md",
            "kimi": ".kimi/commands/antislop.md",
            "adal": ".adal/commands/antislop.md",
            "openhands": ".openhands/instructions/antislop.md",
            "grok": ".grok/instructions/antislop.md",
            "amp": ".amp/instructions/antislop.md",
            "crush": ".crush/instructions/antislop.md",
            "hermes": ".hermes/commands/antislop.md",
            "openclaw": ".openclaw/commands/antislop.md",
            "mcp": ".mcp/antislop.md",
            "scripts": "scripts/antislop.md",
        },
    },
)
DEFAULT_TOOL_SOURCE_TEXTS = {
    "simplify": """# Simplify

Use this cross-harness tool source when a repo needs a concise explanation, cleanup plan, or smaller version of a complex artifact.

## Intent

Turn dense local context into a clear, actionable summary without changing files, running commands, or hiding important uncertainty.

## Use When

- A plan, issue, design note, or handoff is too broad to act on.
- A user asks for a shorter version of existing local material.
- A harness needs the same simplification behavior projected into its native command or skill format.

## Procedure

1. Identify the source material and the target audience.
2. Preserve concrete facts, commands, file paths, blockers, and decisions.
3. Remove repetition, speculation, and incidental implementation detail.
4. Keep unresolved questions explicit.
5. End with the next concrete action when one is known.

## Boundaries

- Do not invent missing facts.
- Do not remove safety, privacy, approval, or verification requirements.
- Do not edit source files unless the user explicitly asks.
- Do not run commands only to simplify text.

## Output Shape

Prefer:

- one short answer-first paragraph
- a short list of material facts or decisions
- a final next action when useful
""",
    "superpowers": """# Superpowers

Use this cross-harness tool source to expose repo-reviewed workflows, skills, and repeatable operating patterns to any agent harness.

## Intent

Make higher-level capabilities discoverable across Claude, Codex, OpenCode, OpenClaw, Hermes, scripts, MCP adapters, and future harnesses without treating any one harness as canonical.

## Use When

- A repo has a reviewed workflow that should be reused by more than one harness.
- A command, skill, checklist, or operating pattern needs a single source document before projection.
- Agents need to know which local capability to use before starting work.

## Procedure

1. Start from the tracked source document in `tools/`.
2. Project only reviewed content into harness-specific locations.
3. Keep generated projections local unless the repo intentionally tracks them.
4. Re-run the relevant Brigade health check after changing source or projections.
5. Write a handoff when the available cross-harness capability changes in a durable way.

## Boundaries

- Do not auto-install harness plugins or skills.
- Do not overwrite harness-specific files without an explicit command.
- Do not store secrets, private host paths, tokens, or user-specific credentials in shared source docs.
- Do not use projections to bypass repo guidance, approvals, or safety rules.

## Useful Commands

```bash
brigade tools doctor --target .
brigade tools list --target .
brigade tools plan --target .
```

Use `brigade tools apply` only when the operator explicitly wants reviewed projections written.
""",
    "frontend": """# Frontend

Use this cross-harness tool source when implementing, reviewing, or polishing a user-facing frontend.

## Intent

Build interfaces that are usable, coherent, responsive, and visually appropriate for the product context instead of generic demo screens.

## Use When

- A task changes a web, mobile, dashboard, game, or interactive UI.
- A feature needs layout, components, interaction states, or responsive behavior.
- A frontend should be checked for visual quality before handoff.

## Procedure

1. Read the existing app structure, design system, routes, and component patterns before inventing new UI.
2. Identify the primary user workflow and make the first screen useful for that workflow.
3. Use established local components, icons, spacing, colors, and state patterns where they exist.
4. Build complete controls and states: loading, empty, error, active, disabled, hover, focus, and mobile layouts when relevant.
5. Keep text fitted to its containers and check for overlap at common desktop and mobile sizes.
6. Verify with the smallest meaningful browser or screenshot check when the app can run locally.

## Boundaries

- Do not create a landing page when the user asked for an app, tool, game, or workflow screen.
- Do not add decorative gradients, blobs, cards, or large hero sections unless they serve the product.
- Do not introduce new UI dependencies unless the task explicitly requires them or the repo already uses them.
- Do not leave placeholder controls, fake actions, or unreachable states when the user expects a working experience.

## Output Shape

Prefer:

- the implemented UI change
- the verification command or browser check
- any remaining responsive or asset caveat
""",
    "antislop": """# Anti-Slop

Use this cross-harness tool source to remove vague, low-quality, performative, or unfinished work before handoff.

## Intent

Force the work to become specific, testable, and useful. Replace generic filler with concrete behavior, evidence, and clear next actions.

## Use When

- An answer, plan, UI, document, or code change feels generic or padded.
- A task claims success without enough verification.
- A workflow has placeholders, fake completeness, weak assumptions, or unexplained tradeoffs.
- A user asks for sharper, less sloppy, or more production-ready work.

## Procedure

1. Name the actual goal in one sentence.
2. Remove decorative text, vague praise, generic best practices, and unsupported certainty.
3. Check whether every claim is backed by a file, command, source, screenshot, test, or explicit assumption.
4. Replace placeholders with working behavior, or label the blocker and the exact missing input.
5. Prefer root-cause fixes over cosmetic patches.
6. Run the smallest meaningful verification step before saying the work is done.
7. Report only the changes, evidence, caveats, and next action that matter.

## Boundaries

- Do not use anti-slop as an excuse to broaden scope.
- Do not rewrite user intent into a different task.
- Do not hide uncertainty, skipped verification, or known gaps.
- Do not make prose terse at the cost of losing required technical detail.

## Output Shape

Prefer:

- concise answer first
- concrete evidence or verification
- explicit caveats only when they affect the user
""",
}
DEFAULT_RUNTIMES = (
    {
        "id": "local-helper",
        "name": "Local Helper",
        "enabled": True,
        "command": "python3 -m http.server 8765",
        "cwd": ".",
        "port": 8765,
        "health_command": "python3 --version",
        "health_path": ".brigade/tools/runtime/local-helper.json",
        "pid_path": ".brigade/tools/runtime/local-helper.pid",
        "log_path": ".brigade/tools/runtime/local-helper.log",
        "timeout": 10,
    },
)
DEFAULT_POLICY = {
    "allowed_families": ["script"],
    "allowed_effects": ["local-read", "local-write"],
    "denied_effects": ["remote-mutation", "secret-read"],
    "required_approval_modes": ["on-request", "always"],
    "max_timeout": 60,
    "allowed_runtimes": ["local-helper"],
    "env_bindings": {"SAFE_ENV": "SAFE_ENV"},
}


def config_path(target: Path) -> Path:
    return target / CONFIG_REL_PATH


def calls_path(target: Path) -> Path:
    return target / CALLS_REL_PATH


def runs_path(target: Path) -> Path:
    return target / RUNS_REL_PATH


def checkpoints_path(target: Path) -> Path:
    return target / CHECKPOINTS_REL_PATH


def runtimes_config_path(target: Path) -> Path:
    return target / RUNTIMES_REL_PATH


def runtime_state_path(target: Path) -> Path:
    return target / RUNTIME_STATE_REL_PATH


def policy_path(target: Path) -> Path:
    return target / POLICY_REL_PATH


def parity_closeouts_path(target: Path) -> Path:
    return target / PARITY_CLOSEOUTS_REL_PATH


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


def _format_tools_toml(tools: tuple[dict[str, Any], ...] = DEFAULT_TOOLS) -> str:
    return _format_tool_entries([dict(tool) for tool in tools])


def _default_tool_source_text(tool_id: str) -> str | None:
    source_path = Path(__file__).resolve().parents[2] / "tools" / f"{tool_id}.md"
    if source_path.is_file():
        return source_path.read_text(errors="replace")
    return DEFAULT_TOOL_SOURCE_TEXTS.get(tool_id)


def _ensure_default_tool_sources(target: Path, *, dry_run: bool = False) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for tool in DEFAULT_TOOLS:
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


def _format_runtimes_toml(runtimes: tuple[dict[str, Any], ...] = DEFAULT_RUNTIMES) -> str:
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


def _format_policy_toml(policy: dict[str, Any] = DEFAULT_POLICY) -> str:
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
        if tool.get("approval_mode") and tool["approval_mode"] not in APPROVAL_MODES:
            errors.append(f"{label}: approval_mode must be one of: {', '.join(APPROVAL_MODES)}")
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
    path = runtimes_config_path(target)
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
    path = policy_path(target)
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
    invalid_families = [family for family in policy["allowed_families"] if family not in FAMILIES]
    if invalid_families:
        errors.append(f"allowed_families has unknown values: {', '.join(invalid_families)}")
    invalid_modes = [mode for mode in policy["required_approval_modes"] if mode not in APPROVAL_MODES]
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


def _find_runtime(target: Path, runtime_id: str) -> tuple[dict[str, Any] | None, list[str]]:
    runtimes, errors = _load_runtime_config(target)
    for runtime in runtimes:
        if runtime.get("enabled", True) and runtime.get("id") == runtime_id:
            return runtime, errors
    if not errors:
        errors.append(f"runtime not found: {runtime_id}")
    return None, errors


def _runtime_file(target: Path, runtime: dict[str, Any], field: str, default_suffix: str) -> Path:
    runtime_id = str(runtime.get("id") or "runtime")
    configured = runtime.get(field)
    if isinstance(configured, str) and configured.strip():
        return _as_path(target, configured) or (runtime_state_path(target) / f"{runtime_id}{default_suffix}")
    return runtime_state_path(target) / f"{runtime_id}{default_suffix}"


def _runtime_pid_path(target: Path, runtime: dict[str, Any]) -> Path:
    return _runtime_file(target, runtime, "pid_path", ".pid")


def _runtime_metadata_path(target: Path, runtime: dict[str, Any]) -> Path:
    return runtime_state_path(target) / f"{runtime.get('id')}.json"


def _runtime_health_path(target: Path, runtime: dict[str, Any]) -> Path | None:
    value = runtime.get("health_path")
    return _as_path(target, value) if value else None


def _runtime_log_paths(target: Path, runtime: dict[str, Any]) -> tuple[Path, Path]:
    runtime_id = str(runtime.get("id") or "runtime")
    configured = runtime.get("log_path")
    base = _as_path(target, configured) if configured else runtime_state_path(target) / f"{runtime_id}.log"
    assert base is not None
    return base.with_suffix(base.suffix + ".stdout"), base.with_suffix(base.suffix + ".stderr")


def _read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _process_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.is_file():
        try:
            parts = stat_path.read_text().split()
        except OSError:
            parts = []
        if len(parts) > 2 and parts[2] == "Z":
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_runtime_metadata(target: Path, runtime: dict[str, Any]) -> dict[str, Any] | None:
    path = _runtime_metadata_path(target, runtime)
    payload, error = _read_json(path) if path.is_file() else (None, None)
    if error is not None or not isinstance(payload, dict):
        return None
    return payload


def _write_runtime_metadata(target: Path, runtime: dict[str, Any], metadata: dict[str, Any]) -> None:
    path = _runtime_metadata_path(target, runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")


def _port_in_use(port: object) -> bool:
    if not isinstance(port, int):
        return False
    loopback = ".".join(("127", "0", "0", "1"))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.1)
        return sock.connect_ex((loopback, port)) == 0


def _runtime_cwd(target: Path, runtime: dict[str, Any]) -> Path:
    cwd = _as_path(target, runtime.get("cwd"))
    return cwd or target


def _runtime_status_item(target: Path, runtime: dict[str, Any], *, run_health: bool = True) -> dict[str, Any]:
    pid_path = _runtime_pid_path(target, runtime)
    metadata_path = _runtime_metadata_path(target, runtime)
    stdout_path, stderr_path = _runtime_log_paths(target, runtime)
    pid = _read_pid(pid_path)
    alive = _process_alive(pid)
    stale_pid = pid is not None and not alive
    metadata = _read_runtime_metadata(target, runtime)
    managed = bool(metadata and metadata.get("runtime_id") == runtime.get("id") and metadata.get("pid") == pid)
    cwd = _runtime_cwd(target, runtime)
    issues: list[dict[str, Any]] = []
    if _high_risk_command(runtime.get("command")):
        issues.append(
            _tool_issue(
                {"id": runtime.get("id"), "family": "runtime"},
                "runtime_high_risk_command",
                "runtime command shape is high risk",
            )
        )
    if not _command_parts(runtime.get("command")):
        issues.append(
            _tool_issue(
                {"id": runtime.get("id"), "family": "runtime"},
                "runtime_bad_command",
                "runtime command could not be parsed",
            )
        )
    if not cwd.is_dir():
        issues.append(
            _tool_issue(
                {"id": runtime.get("id"), "family": "runtime"}, "runtime_missing_cwd", f"runtime cwd missing: {cwd}"
            )
        )
    if stale_pid:
        issues.append(
            _tool_issue(
                {"id": runtime.get("id"), "family": "runtime"}, "runtime_stale_pid", f"stale pid file: {pid_path}"
            )
        )
    if isinstance(runtime.get("port"), int) and _port_in_use(runtime["port"]) and not alive:
        issues.append(
            _tool_issue(
                {"id": runtime.get("id"), "family": "runtime"},
                "runtime_port_conflict",
                f"port is already in use: {runtime['port']}",
            )
        )
    health_path = _runtime_health_path(target, runtime)
    health_ok = True
    health_detail = "not configured"
    if alive and health_path is not None:
        if health_path.exists():
            health_detail = f"health path present: {health_path}"
        else:
            health_ok = False
            health_detail = f"health path missing: {health_path}"
            issues.append(
                _tool_issue({"id": runtime.get("id"), "family": "runtime"}, "runtime_health_failed", health_detail)
            )
    health_command = runtime.get("health_command")
    if alive and run_health and isinstance(health_command, str) and health_command.strip():
        parts = _command_parts(health_command)
        if not parts:
            health_ok = False
            health_detail = "health command could not be parsed"
            issues.append(
                _tool_issue({"id": runtime.get("id"), "family": "runtime"}, "runtime_health_failed", health_detail)
            )
        elif _high_risk_command(health_command):
            health_ok = False
            health_detail = "health command shape is high risk"
            issues.append(
                _tool_issue({"id": runtime.get("id"), "family": "runtime"}, "runtime_health_failed", health_detail)
            )
        else:
            try:
                completed = subprocess.run(
                    parts,
                    cwd=cwd if cwd.is_dir() else target,
                    text=True,
                    capture_output=True,
                    timeout=float(runtime.get("timeout") or 5),
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                health_ok = False
                health_detail = f"health command failed: {_short(str(exc))}"
                issues.append(
                    _tool_issue({"id": runtime.get("id"), "family": "runtime"}, "runtime_health_failed", health_detail)
                )
            else:
                health_ok = completed.returncode == 0
                health_detail = f"health command exit_code={completed.returncode}"
                if completed.returncode != 0:
                    issues.append(
                        _tool_issue(
                            {"id": runtime.get("id"), "family": "runtime"}, "runtime_health_failed", health_detail
                        )
                    )
    state = "running" if alive else ("stale" if stale_pid else "stopped")
    return {
        "id": runtime.get("id"),
        "name": runtime.get("name"),
        "enabled": runtime.get("enabled", True),
        "command": runtime.get("command"),
        "cwd": str(cwd),
        "port": runtime.get("port"),
        "pid": pid,
        "state": state,
        "running": alive,
        "managed": managed,
        "stale_pid": stale_pid,
        "pid_path": str(pid_path),
        "metadata_path": str(metadata_path),
        "stdout_log_path": str(stdout_path),
        "stderr_log_path": str(stderr_path),
        "health_path": str(health_path) if health_path is not None else None,
        "health_ok": health_ok if alive else None,
        "health_detail": health_detail,
        "metadata": metadata,
        "issues": issues,
        "issue_count": len(issues),
    }


def _runtime_payload(target: Path, runtime_id: str | None = None, *, run_health: bool = True) -> dict[str, Any]:
    target = target.expanduser().resolve()
    runtimes, errors = _load_runtime_config(target)
    if runtime_id is not None:
        runtimes = [runtime for runtime in runtimes if runtime.get("id") == runtime_id]
        if not runtimes and not errors:
            errors.append(f"runtime not found: {runtime_id}")
    statuses = [
        _runtime_status_item(target, runtime, run_health=run_health)
        for runtime in runtimes
        if runtime.get("enabled", True)
    ]
    issues = [issue for item in statuses for issue in item.get("issues", [])]
    if errors:
        issues.insert(
            0, {"status": WARN, "name": "runtime_config", "issue_type": "runtime_config", "detail": "; ".join(errors)}
        )
    counts: dict[str, int] = {}
    for item in statuses:
        state = str(item.get("state") or "unknown")
        counts[state] = counts.get(state, 0) + 1
    return {
        "target": str(target),
        "config_path": str(runtimes_config_path(target)),
        "state_path": str(runtime_state_path(target)),
        "valid": not errors,
        "errors": errors,
        "runtimes": statuses,
        "runtime_count": len(statuses),
        "counts": counts,
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }


def _tool_runtime_issues(
    target: Path, tools: list[dict[str, Any]], runtime_payload: dict[str, Any]
) -> list[dict[str, Any]]:
    runtimes_by_id = {str(item.get("id")): item for item in runtime_payload.get("runtimes", []) if item.get("id")}
    issues: list[dict[str, Any]] = []
    for tool in tools:
        if not tool.get("enabled", True):
            continue
        runtime_id = tool.get("runtime_id")
        requires_runtime = bool(tool.get("requires_runtime"))
        if requires_runtime and (not isinstance(runtime_id, str) or not runtime_id.strip()):
            issues.append(_tool_issue(tool, "runtime_missing", "tool requires a runtime but runtime_id is missing"))
            continue
        if not isinstance(runtime_id, str) or not runtime_id.strip():
            continue
        runtime = runtimes_by_id.get(runtime_id)
        if runtime is None:
            issues.append(_tool_issue(tool, "runtime_missing", f"tool runtime is not configured: {runtime_id}"))
            continue
        if requires_runtime and not runtime.get("running"):
            issues.append(_tool_issue(tool, "runtime_stopped", f"required runtime is not running: {runtime_id}"))
        if requires_runtime and runtime.get("health_ok") is False:
            issues.append(_tool_issue(tool, "runtime_unhealthy", f"required runtime is unhealthy: {runtime_id}"))
    return issues


def _policy_decision(
    target: Path,
    plan: dict[str, Any],
    *,
    include_env_values: bool = False,
) -> dict[str, Any]:
    policy, errors = _load_policy_config(target)
    if policy is None:
        return {
            "enabled": False,
            "policy_path": str(policy_path(target)),
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
        "policy_path": str(policy_path(target)),
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
    policy, errors = _load_policy_config(target)
    issues: list[dict[str, Any]] = []
    if policy is None:
        policy_relevant = [
            tool for tool in tools if tool.get("enabled", True) and tool.get("command") and _contract_defined(tool)
        ]
        if policy_relevant:
            issues.append(
                {
                    "status": WARN,
                    "name": "tool_policy_missing",
                    "tool_id": "policy",
                    "family": "policy",
                    "issue_type": "policy_missing",
                    "detail": errors[0] if errors else f"tool execution policy missing: {policy_path(target)}",
                }
            )
        return {
            "policy_path": str(policy_path(target)),
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
                "status": WARN,
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
            issues.append(_tool_issue(tool, issue_type, blocker))
    return {
        "policy_path": str(policy_path(target)),
        "enabled": True,
        "valid": not errors,
        "errors": errors,
        "policy": {key: value for key, value in policy.items() if key != "raw"},
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }


def _start_runtime_payload(target: Path, runtime_id: str) -> tuple[dict[str, Any], int]:
    target = target.expanduser().resolve()
    runtime, errors = _find_runtime(target, runtime_id)
    if runtime is None:
        return {"target": str(target), "error": "; ".join(errors)}, 1
    status = _runtime_status_item(target, runtime, run_health=False)
    blockers: list[str] = []
    if status.get("running"):
        return {
            "target": str(target),
            "runtime": status,
            "started": 0,
            "skipped": 1,
            "reason": "runtime already running",
        }, 0
    command = str(runtime.get("command") or "")
    parts = _command_parts(command)
    cwd = _runtime_cwd(target, runtime)
    if _high_risk_command(command):
        blockers.append("runtime command shape is high risk")
    if not parts:
        blockers.append("runtime command could not be parsed")
    if not cwd.is_dir():
        blockers.append(f"runtime cwd missing: {cwd}")
    if status.get("stale_pid"):
        blockers.append(f"stale pid file: {status.get('pid_path')}")
    if isinstance(runtime.get("port"), int) and _port_in_use(runtime["port"]):
        blockers.append(f"port is already in use: {runtime['port']}")
    if blockers:
        return {
            "target": str(target),
            "runtime": status,
            "started": 0,
            "blockers": blockers,
            "error": "runtime is not startable",
        }, 1
    pid_path = _runtime_pid_path(target, runtime)
    metadata_path = _runtime_metadata_path(target, runtime)
    stdout_path, stderr_path = _runtime_log_paths(target, runtime)
    for path in (pid_path, metadata_path, stdout_path, stderr_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    stdout_file = stdout_path.open("a")
    stderr_file = stderr_path.open("a")
    try:
        process = subprocess.Popen(
            parts,
            cwd=cwd,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
            start_new_session=True,
        )
    finally:
        stdout_file.close()
        stderr_file.close()
    started_at = _now().isoformat()
    pid_path.write_text(f"{process.pid}\n")
    metadata = {
        "runtime_id": runtime.get("id"),
        "pid": process.pid,
        "command": command,
        "cwd": str(cwd),
        "started_at": started_at,
        "pid_path": str(pid_path),
        "stdout_log_path": str(stdout_path),
        "stderr_log_path": str(stderr_path),
    }
    _write_runtime_metadata(target, runtime, metadata)
    health_path = _runtime_health_path(target, runtime)
    if health_path is not None:
        health_path.parent.mkdir(parents=True, exist_ok=True)
        health_path.write_text(
            json.dumps({"runtime_id": runtime.get("id"), "pid": process.pid, "started_at": started_at}, sort_keys=True)
            + "\n"
        )
    status = _runtime_status_item(target, runtime)
    return {
        "target": str(target),
        "runtime": status,
        "started": 1,
        "skipped": 0,
        "pid": process.pid,
        "pid_path": str(pid_path),
        "metadata_path": str(metadata_path),
    }, 0


def _stop_runtime_payload(target: Path, runtime_id: str) -> tuple[dict[str, Any], int]:
    target = target.expanduser().resolve()
    runtime, errors = _find_runtime(target, runtime_id)
    if runtime is None:
        return {"target": str(target), "error": "; ".join(errors)}, 1
    pid_path = _runtime_pid_path(target, runtime)
    pid = _read_pid(pid_path)
    metadata = _read_runtime_metadata(target, runtime)
    if pid is None:
        return {
            "target": str(target),
            "runtime": _runtime_status_item(target, runtime),
            "stopped": 0,
            "reason": "runtime is not running",
        }, 0
    if (
        not metadata
        or metadata.get("runtime_id") != runtime.get("id")
        or metadata.get("pid") != pid
        or metadata.get("command") != runtime.get("command")
    ):
        return {
            "target": str(target),
            "runtime": _runtime_status_item(target, runtime),
            "stopped": 0,
            "error": "refusing to stop unmanaged runtime process",
        }, 1
    if not _process_alive(pid):
        pid_path.unlink(missing_ok=True)
        metadata["stopped_at"] = _now().isoformat()
        metadata["stop_reason"] = "stale pid"
        _write_runtime_metadata(target, runtime, metadata)
        return {
            "target": str(target),
            "runtime": _runtime_status_item(target, runtime),
            "stopped": 0,
            "reason": "stale pid removed",
        }, 0
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return {
            "target": str(target),
            "runtime": _runtime_status_item(target, runtime),
            "stopped": 0,
            "error": str(exc),
        }, 1
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            break
        time.sleep(0.05)
    if _process_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    pid_path.unlink(missing_ok=True)
    metadata["stopped_at"] = _now().isoformat()
    _write_runtime_metadata(target, runtime, metadata)
    return {"target": str(target), "runtime": _runtime_status_item(target, runtime), "stopped": 1, "pid": pid}, 0


def _restart_runtime_payload(target: Path, runtime_id: str) -> tuple[dict[str, Any], int]:
    stop_payload, stop_rc = _stop_runtime_payload(target, runtime_id)
    if stop_rc != 0:
        return {
            "target": str(target.expanduser().resolve()),
            "stop": stop_payload,
            "error": stop_payload.get("error"),
        }, stop_rc
    start_payload, start_rc = _start_runtime_payload(target, runtime_id)
    return {
        "target": str(target.expanduser().resolve()),
        "stop": stop_payload,
        "start": start_payload,
        "runtime": start_payload.get("runtime"),
    }, start_rc


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


def _redact_value(key: str, value: object) -> object:
    if UNSAFE_FIELD_PATTERN.search(key):
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
    return _short(text, limit)


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
    return _as_path(target, tool.get(field))


def _load_schema(target: Path, tool: dict[str, Any], field: str) -> tuple[object | None, str | None]:
    path = _schema_path(target, tool, field)
    if path is None:
        return None, None
    if not path.is_file():
        return None, f"missing schema: {path}"
    return _read_json(path)


def _schema_shape_errors(schema: object, *, path: str = "$", root: bool = True) -> list[str]:
    if not isinstance(schema, dict):
        return [f"{path}: schema must be an object"]
    schema_type = schema.get("type")
    if root and schema_type != "object":
        return [f"{path}: root schema type must be object"]
    if schema_type is not None and schema_type not in SCHEMA_TYPES:
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
    examples_path = _as_path(target, tool.get("examples_path"))
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
    source_path = _as_path(target, tool.get("source_path"))
    if source_path is not None:
        source_hash = _file_hash(source_path)
        if source_hash is not None:
            return source_hash
    return str(tool.get("fingerprint") or "")


def _contract_fingerprint(target: Path, tool: dict[str, Any]) -> str:
    paths: dict[str, str | None] = {}
    for field in ("input_schema_path", "output_schema_path", "examples_path"):
        path = _as_path(target, tool.get(field))
        paths[field] = _file_hash(path) if path is not None else None
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
            issues.append(_tool_issue(tool, "missing_runtime", "runtime_id is required for MCP execution"))
        if not tool.get("mcp_tool_name"):
            issues.append(_tool_issue(tool, "missing_mcp_tool_name", "mcp_tool_name is required for MCP execution"))
        if not tool.get("command"):
            issues.append(_tool_issue(tool, "missing_command", "command is required for local MCP stdio execution"))
    if not _contract_defined(tool):
        if tool.get("command") or tool.get("family") in {"script", "custom", "mcp", "openapi", "graphql"}:
            issues.append(_tool_issue(tool, "missing_contract", "tool has no call contract metadata"))
        return issues
    for field, issue_prefix in (("input_schema_path", "input_schema"), ("output_schema_path", "output_schema")):
        schema_path = _schema_path(target, tool, field)
        if schema_path is None:
            if field == "input_schema_path":
                issues.append(
                    _tool_issue(tool, "missing_input_schema", "input_schema_path is required for call planning")
                )
            continue
        if not schema_path.is_file():
            issues.append(_tool_issue(tool, f"missing_{issue_prefix}", f"missing schema: {schema_path}"))
            continue
        schema, error = _read_json(schema_path)
        if error is not None:
            issues.append(_tool_issue(tool, f"invalid_{issue_prefix}", f"{schema_path}: {error}"))
            continue
        shape_errors = _schema_shape_errors(schema)
        if shape_errors:
            issues.append(_tool_issue(tool, f"unsupported_{issue_prefix}", f"{schema_path}: {'; '.join(shape_errors)}"))
    examples_path = _as_path(target, tool.get("examples_path"))
    if tool.get("examples_path") and (examples_path is None or not examples_path.is_file()):
        issues.append(_tool_issue(tool, "missing_examples", f"missing examples: {examples_path}"))
    for label in tool.get("env_labels", []):
        if UNSAFE_FIELD_PATTERN.search(label):
            issues.append(_tool_issue(tool, "unsafe_env_labels", f"unsafe env label name: {label}"))
    for key, value in tool.get("argument_template", {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key)):
            issues.append(_tool_issue(tool, "bad_argument_template", f"invalid template output key: {key}"))
        for variable in re.findall(r"\{([^{}]+)\}", str(value)):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
                issues.append(_tool_issue(tool, "bad_argument_template", f"invalid template variable: {variable}"))
    return issues


def _managed_header(metadata: dict[str, Any]) -> str:
    rendered = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    return f"<!-- {PROJECTION_MARKER} {rendered} -->"


def _managed_yaml_comment(metadata: dict[str, Any]) -> str:
    rendered = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    return f"# {PROJECTION_MARKER} {rendered}"


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
    prefix = f"<!-- {PROJECTION_MARKER} "
    if not first.startswith(prefix) or not first.endswith(" -->"):
        if first != "---":
            return None, text
        yaml_prefix = f"# {PROJECTION_MARKER} "
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
    source_path = _as_path(target, tool.get("source_path"))
    projection_path = _as_path(target, projection_value)
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
    if not _path_within_target(target, projection_path):
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
    source_fingerprint = _text_hash(source_text)
    body = _render_projection_body(tool, harness, source_text, _relative_path(target, source_path))
    managed_body = _projection_managed_body(tool, harness, projection_path, body)
    projection_fingerprint = _text_hash(managed_body)
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
    actual_projection_fingerprint = _text_hash(existing_body or "")
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
    tools, errors = _load_config(target)
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
        "config_path": str(config_path(target)),
        "valid": not errors,
        "errors": errors,
        "tool_id": tool_id,
        "tools": [tool.get("id") for tool in selected],
        "projections": [{key: value for key, value in item.items() if key != "rendered"} for item in projections],
        "counts": counts,
    }


def _projection_issue(tool: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    status = str(item.get("status") or "projection")
    harness = str(item.get("harness") or "")
    detail = str(item.get("detail") or "")
    issue = _tool_issue(
        tool,
        f"{status}_projection" if status not in {"missing"} else "missing_projection",
        f"{harness}: {detail}",
        harness=harness,
        target=str(item.get("projection_path") or ""),
    )
    issue.update(
        {
            "projection_status": status,
            "tool_source_fingerprint": item.get("source_fingerprint"),
            "expected_projection_fingerprint": item.get("expected_projection_fingerprint")
            or item.get("expected_fingerprint"),
            "actual_projection_fingerprint": item.get("actual_projection_fingerprint"),
        }
    )
    return issue


def _tool_issue(
    tool: dict[str, Any], issue_type: str, detail: str, *, harness: str | None = None, target: str | None = None
) -> dict[str, Any]:
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


def _is_parity_issue(issue: dict[str, Any]) -> bool:
    return str(issue.get("issue_type") or issue.get("name") or "") in PARITY_ISSUE_TYPES


def _parity_issue_fingerprint(issue: dict[str, Any]) -> str:
    return _stable_hash(
        {
            "tool_id": issue.get("tool_id"),
            "family": issue.get("family"),
            "issue_type": issue.get("issue_type") or issue.get("name"),
            "harness": issue.get("harness"),
            "projection_target": issue.get("projection_target"),
            "projection_status": issue.get("projection_status"),
            "tool_source_fingerprint": issue.get("tool_source_fingerprint"),
            "expected_projection_fingerprint": issue.get("expected_projection_fingerprint"),
            "actual_projection_fingerprint": issue.get("actual_projection_fingerprint"),
            "detail": issue.get("detail"),
        }
    )


def _latest_parity_closeout(target: Path) -> dict[str, Any] | None:
    root = parity_closeouts_path(target)
    if not root.is_dir():
        return None
    closeouts: list[dict[str, Any]] = []
    for path in root.glob("*/closeout.json"):
        payload, error = _read_json(path)
        if error is None and isinstance(payload, dict):
            payload.setdefault("path", str(path))
            closeouts.append(payload)
    closeouts.sort(key=lambda item: str(item.get("created_at") or item.get("closeout_id") or ""), reverse=True)
    return closeouts[0] if closeouts else None


def _apply_parity_closeout(target: Path, issues: list[dict[str, Any]]) -> dict[str, Any]:
    closeout = _latest_parity_closeout(target)
    closed = set(closeout.get("source_fingerprints", [])) if isinstance(closeout, dict) else set()
    active: list[dict[str, Any]] = []
    quieted: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    for issue in issues:
        if not _is_parity_issue(issue):
            active.append(issue)
            continue
        fingerprint = _parity_issue_fingerprint(issue)
        issue["parity_fingerprint"] = fingerprint
        if fingerprint in closed:
            quieted.append(issue)
        else:
            active.append(issue)
            if closed:
                changed.append(issue)
    return {
        "issues": active,
        "quieted_issues": quieted,
        "changed_issues": changed,
        "latest_closeout": closeout,
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
        "contract": _contract_summary(target, tool),
    }
    unsafe = _unsafe_fields(tool.get("raw", {}))
    if unsafe:
        issues.append(_tool_issue(tool, "unsafe_auth_fields", f"unsafe field names: {', '.join(unsafe[:8])}"))
    issues.extend(_contract_issues(target, tool))
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
    for harness in tool.get("supported_harnesses", []):
        projection_item = _projection_item(target, tool, harness)
        status = str(projection_item.get("status") or "missing")
        summary["projection_coverage"][harness] = status
        if projection_item.get("projection_path"):
            summary.setdefault("projection_paths", {})[harness] = projection_item["projection_path"]
        if status == "missing" and projection_item.get("action") == "skip":
            issues.append(_tool_issue(tool, "parity_gap", f"missing projection for {harness}", harness=harness))
            continue
        if status == "missing_source":
            continue
        if status in {"missing", "stale", "conflicted", "unmanaged"}:
            issues.append(_projection_issue(tool, projection_item))
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
    tools, errors = _load_config(target)
    for tool in tools:
        if tool.get("enabled", True) and tool.get("id") == tool_id:
            return tool, errors
    if not errors:
        errors.append(f"tool not found: {tool_id}")
    return None, errors


def _contracts_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    tools, errors = _load_config(target)
    contracts: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for tool in tools:
        if not tool.get("enabled", True):
            continue
        summary = _contract_summary(target, tool)
        tool_issues = _contract_issues(target, tool)
        summary["issue_count"] = len(tool_issues)
        summary["issues"] = tool_issues
        contracts.append(summary)
        issues.extend(tool_issues)
    return {
        "target": str(target),
        "config_path": str(config_path(target)),
        "valid": not errors,
        "errors": errors,
        "contracts": contracts,
        "contract_count": len(contracts),
        "issue_count": len(issues),
        "issues": issues,
    }


def _describe_payload(target: Path, tool_id: str) -> dict[str, Any]:
    target = target.expanduser().resolve()
    tool, errors = _find_tool(target, tool_id)
    summary: dict[str, Any] | None = None
    issues: list[dict[str, Any]] = []
    if tool is not None:
        inspected, inspect_issues = _inspect_tool(target, tool)
        summary = inspected
        issues = inspect_issues
    return {
        "target": str(target),
        "config_path": str(config_path(target)),
        "valid": not errors,
        "errors": errors,
        "tool": summary,
        "issues": issues,
        "issue_count": len(issues),
    }


def _load_args(args: str | None, args_json: Path | None) -> tuple[object | None, str | None]:
    if args and args_json:
        return None, "pass only one of --args or --args-json"
    if args_json is not None:
        try:
            return json.loads(args_json.expanduser().read_text()), None
        except OSError as exc:
            return None, str(exc)
        except json.JSONDecodeError as exc:
            return None, f"invalid args JSON: {exc.msg}"
    if args is not None:
        try:
            return json.loads(args), None
        except json.JSONDecodeError as exc:
            return None, f"invalid args JSON: {exc.msg}"
    return {}, None


def _call_plan_payload(
    target: Path,
    tool_id: str,
    *,
    args: str | None = None,
    args_json: Path | None = None,
) -> dict[str, Any]:
    target = target.expanduser().resolve()
    tool, errors = _find_tool(target, tool_id)
    parsed_args, args_error = _load_args(args, args_json)
    blockers: list[str] = list(errors)
    validation_errors: list[str] = []
    if args_error is not None:
        blockers.append(args_error)
    if parsed_args is not None and not isinstance(parsed_args, dict):
        blockers.append("args must be a JSON object")
    mapped_arguments: dict[str, str] = {}
    projection_blockers: list[dict[str, Any]] = []
    schema: object | None = None
    if tool is not None:
        if not tool.get("command"):
            blockers.append("command is required for call planning")
        if tool.get("family") == "mcp":
            if not tool.get("runtime_id"):
                blockers.append("runtime_id is required for MCP call planning")
            if not tool.get("mcp_tool_name"):
                blockers.append("mcp_tool_name is required for MCP call planning")
        auth_label = str(tool.get("auth_label") or "")
        if auth_label and UNSAFE_FIELD_PATTERN.search(auth_label):
            blockers.append("auth_label appears unsafe")
        for label in tool.get("env_labels", []):
            if UNSAFE_FIELD_PATTERN.search(str(label)):
                blockers.append(f"env label appears unsafe: {label}")
        schema, schema_error = _load_schema(target, tool, "input_schema_path")
        if schema_error is not None:
            blockers.append(schema_error)
        elif schema is None:
            blockers.append("input_schema_path is required for call planning")
        else:
            shape_errors = _schema_shape_errors(schema)
            if shape_errors:
                blockers.extend(shape_errors)
            elif isinstance(parsed_args, dict):
                validation_errors = _validate_json_value(parsed_args, schema)  # type: ignore[arg-type]
                blockers.extend(validation_errors)
        for harness in tool.get("supported_harnesses", []):
            projection = _projection_item(target, tool, harness)
            if projection.get("status") in {"conflicted", "unmanaged"}:
                projection_blockers.append({key: value for key, value in projection.items() if key != "rendered"})
        if projection_blockers:
            blockers.append("one or more projections are conflicted or unmanaged")
        if isinstance(parsed_args, dict):
            for key, template in tool.get("argument_template", {}).items():
                missing = [
                    var for var in re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", template) if var not in parsed_args
                ]
                for var in missing:
                    blockers.append(f"argument_template {key} references missing arg {var}")
                rendered = _render_argument_template(str(template), parsed_args)
                if UNSAFE_FIELD_PATTERN.search(str(key)) or any(
                    UNSAFE_FIELD_PATTERN.search(var)
                    for var in re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", str(template))
                ):
                    mapped_arguments[str(key)] = "[redacted]"
                else:
                    mapped_arguments[str(key)] = rendered
    safe_env_labels = [
        "[redacted]" if UNSAFE_FIELD_PATTERN.search(str(label)) else str(label)
        for label in (tool.get("env_labels", []) if tool is not None else [])
    ]
    safe_args = _redact_payload(parsed_args) if parsed_args is not None else None
    plan_payload = {
        "tool_id": tool_id,
        "family": tool.get("family") if tool is not None else None,
        "command": tool.get("command") if tool is not None else None,
        "cwd": tool.get("cwd") if tool is not None else None,
        "timeout": tool.get("timeout") if tool is not None else None,
        "runtime_id": tool.get("runtime_id") if tool is not None else None,
        "requires_runtime": (tool.get("requires_runtime", False) or tool.get("family") == "mcp")
        if tool is not None
        else False,
        "runtime_health_path": tool.get("runtime_health_path") if tool is not None else None,
        "mcp_server_id": tool.get("mcp_server_id") if tool is not None else None,
        "mcp_tool_name": tool.get("mcp_tool_name") if tool is not None else None,
        "auth_label": "[redacted]"
        if tool is not None and UNSAFE_FIELD_PATTERN.search(str(tool.get("auth_label") or ""))
        else (tool.get("auth_label") if tool is not None else None),
        "env_labels": safe_env_labels,
        "arguments": mapped_arguments,
        "args": safe_args,
        "permissions": tool.get("permissions", []) if tool is not None else [],
        "effects": tool.get("effects", []) if tool is not None else [],
        "approval_required": (tool.get("approval_mode") if tool is not None else "never") != "never",
        "approval_mode": tool.get("approval_mode", "never") if tool is not None else "never",
    }
    projection_summary: dict[str, Any] = {"counts": {}, "projections": []}
    contract_fingerprint = None
    source_fingerprint = None
    if tool is not None:
        projection_summary = _projection_plan_payload(target, tool_id=tool_id)["counts"]
        projection_items = _projection_plan_payload(target, tool_id=tool_id)["projections"]
        projection_summary = {
            "counts": projection_summary,
            "projections": [
                {
                    "harness": item.get("harness"),
                    "status": item.get("status"),
                    "action": item.get("action"),
                    "projection_path": item.get("projection_path"),
                }
                for item in projection_items
            ],
        }
        contract_fingerprint = _contract_fingerprint(target, tool)
        source_fingerprint = _source_fingerprint(target, tool)
    policy_decision = _policy_decision(target, plan_payload)
    blockers.extend(policy_decision["blockers"])
    return {
        "target": str(target),
        "config_path": str(config_path(target)),
        "valid": tool is not None and not blockers,
        "tool_id": tool_id,
        "plan": plan_payload,
        "blockers": blockers,
        "policy": {key: value for key, value in policy_decision.items() if key != "env"},
        "validation_errors": validation_errors,
        "projection_blockers": projection_blockers,
        "projection_summary": projection_summary,
        "contract_fingerprint": contract_fingerprint,
        "source_fingerprint": source_fingerprint,
    }


def _read_calls(target: Path) -> list[dict[str, Any]]:
    path = calls_path(target)
    if not path.is_file():
        return []
    calls: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            calls.append(item)
    return calls


def _write_calls(target: Path, calls: list[dict[str, Any]]) -> None:
    path = calls_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n" for item in calls)
    path.write_text(text)


def _call_fingerprint(plan_payload: dict[str, Any]) -> str:
    return _stable_hash(
        {
            "tool_id": plan_payload.get("tool_id"),
            "plan": plan_payload.get("plan"),
            "contract_fingerprint": plan_payload.get("contract_fingerprint"),
            "source_fingerprint": plan_payload.get("source_fingerprint"),
        }
    )


def _call_plan_from_record(call: dict[str, Any]) -> dict[str, Any]:
    contract = call.get("contract") if isinstance(call.get("contract"), dict) else {}
    return {
        "tool_id": call.get("tool_id"),
        "family": call.get("family"),
        "command": call.get("command"),
        "cwd": contract.get("cwd"),
        "timeout": contract.get("timeout"),
        "runtime_id": contract.get("runtime_id"),
        "requires_runtime": contract.get("requires_runtime", False),
        "runtime_health_path": contract.get("runtime_health_path"),
        "mcp_server_id": contract.get("mcp_server_id"),
        "mcp_tool_name": contract.get("mcp_tool_name"),
        "auth_label": contract.get("auth_label"),
        "env_labels": contract.get("env_labels", []),
        "arguments": call.get("arguments"),
        "args": call.get("args"),
        "permissions": contract.get("permissions", []),
        "effects": contract.get("effects", []),
        "approval_required": contract.get("approval_required"),
        "approval_mode": contract.get("approval_mode"),
    }


def _stored_call_fingerprint(call: dict[str, Any]) -> str:
    return _stable_hash(
        {
            "tool_id": call.get("tool_id"),
            "plan": _call_plan_from_record(call),
            "contract_fingerprint": call.get("contract_fingerprint"),
            "source_fingerprint": call.get("source_fingerprint"),
        }
    )


def _approval_fingerprint(call: dict[str, Any]) -> str:
    return _stable_hash(
        {
            "id": call.get("id"),
            "tool_id": call.get("tool_id"),
            "status": call.get("status"),
            "reviewed_at": call.get("reviewed_at"),
            "review_reason": call.get("review_reason"),
            "call_fingerprint": call.get("call_fingerprint"),
            "contract_fingerprint": call.get("contract_fingerprint"),
            "source_fingerprint": call.get("source_fingerprint"),
        }
    )


def _make_call_record(plan_payload: dict[str, Any]) -> dict[str, Any]:
    fingerprint = _call_fingerprint(plan_payload)
    now = _now().isoformat()
    plan = plan_payload.get("plan") if isinstance(plan_payload.get("plan"), dict) else {}
    return {
        "id": f"call-{fingerprint}",
        "status": "pending",
        "created_at": now,
        "reviewed_at": None,
        "review_reason": None,
        "tool_id": plan_payload.get("tool_id"),
        "family": plan.get("family"),
        "command": plan.get("command"),
        "args": plan.get("args"),
        "arguments": plan.get("arguments"),
        "contract": {
            "approval_mode": plan.get("approval_mode"),
            "approval_required": plan.get("approval_required"),
            "permissions": plan.get("permissions", []),
            "effects": plan.get("effects", []),
            "auth_label": plan.get("auth_label"),
            "env_labels": plan.get("env_labels", []),
            "cwd": plan.get("cwd"),
            "timeout": plan.get("timeout"),
            "runtime_id": plan.get("runtime_id"),
            "requires_runtime": plan.get("requires_runtime", False),
            "runtime_health_path": plan.get("runtime_health_path"),
            "mcp_server_id": plan.get("mcp_server_id"),
            "mcp_tool_name": plan.get("mcp_tool_name"),
        },
        "blockers": plan_payload.get("blockers", []),
        "policy": plan_payload.get("policy", {}),
        "projection_summary": plan_payload.get("projection_summary", {}),
        "contract_fingerprint": plan_payload.get("contract_fingerprint"),
        "source_fingerprint": plan_payload.get("source_fingerprint"),
        "call_fingerprint": fingerprint,
        "approval_fingerprint": None,
        "started_at": None,
        "completed_at": None,
        "run_id": None,
        "receipt_path": None,
        "exit_code": None,
    }


def _queue_call_payload(
    target: Path,
    tool_id: str,
    *,
    args: str | None = None,
    args_json: Path | None = None,
    include_blocked: bool = False,
) -> tuple[dict[str, Any], int]:
    target = target.expanduser().resolve()
    plan_payload = _call_plan_payload(target, tool_id, args=args, args_json=args_json)
    record = _make_call_record(plan_payload)
    if record["blockers"] and not include_blocked:
        return {
            "target": str(target),
            "calls_path": str(calls_path(target)),
            "created": 0,
            "skipped": 0,
            "blocked": 1,
            "call": record,
            "reason": "blocked call plans require --include-blocked",
        }, 1
    calls = _read_calls(target)
    for existing in calls:
        if existing.get("call_fingerprint") != record["call_fingerprint"]:
            continue
        if existing.get("status") in {"pending", "approved"}:
            return {
                "target": str(target),
                "calls_path": str(calls_path(target)),
                "created": 0,
                "skipped": 1,
                "blocked": 0,
                "call": existing,
                "reason": f"equivalent call already {existing.get('status')}",
            }, 0
        if existing.get("status") == "rejected":
            return {
                "target": str(target),
                "calls_path": str(calls_path(target)),
                "created": 0,
                "skipped": 1,
                "blocked": 0,
                "call": existing,
                "reason": "equivalent rejected call requires changed args or contract fingerprint",
            }, 0
    existing_ids = {str(existing.get("id")) for existing in calls}
    if record["id"] in existing_ids:
        record["id"] = (
            f"{record['id']}-queued-{_stable_hash({'created_at': record['created_at'], 'count': len(calls)})}"
        )
    calls.append(record)
    _write_calls(target, calls)
    return {
        "target": str(target),
        "calls_path": str(calls_path(target)),
        "created": 1,
        "skipped": 0,
        "blocked": 0,
        "call": record,
        "reason": None,
    }, 0


def _resolve_call(target: Path, call_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str | None]:
    calls = _read_calls(target)
    matches = [item for item in calls if str(item.get("id", "")).startswith(call_id)]
    if not matches:
        return None, calls, f"call not found: {call_id}"
    if len(matches) > 1:
        return None, calls, f"call id is ambiguous: {call_id}"
    return matches[0], calls, None


def _call_current_fingerprints(target: Path, call: dict[str, Any]) -> tuple[str | None, str | None]:
    tool_id = str(call.get("tool_id") or "")
    tool, _ = _find_tool(target, tool_id)
    if tool is None:
        return None, None
    return _contract_fingerprint(target, tool), _source_fingerprint(target, tool)


def _call_projection_summary(target: Path, tool_id: str) -> dict[str, Any]:
    payload = _projection_plan_payload(target, tool_id=tool_id)
    return {
        "counts": payload.get("counts", {}),
        "projections": [
            {
                "harness": item.get("harness"),
                "status": item.get("status"),
                "action": item.get("action"),
                "projection_path": item.get("projection_path"),
            }
            for item in payload.get("projections", [])
            if isinstance(item, dict)
        ],
    }


def _runtime_snapshot_for_call(target: Path, call: dict[str, Any], *, run_health: bool = True) -> dict[str, Any] | None:
    contract = call.get("contract") if isinstance(call.get("contract"), dict) else {}
    runtime_id = contract.get("runtime_id")
    if not isinstance(runtime_id, str) or not runtime_id.strip():
        return None
    runtime, errors = _find_runtime(target, runtime_id)
    if runtime is None:
        return {
            "id": runtime_id,
            "state": "missing",
            "running": False,
            "health_ok": False,
            "errors": errors,
        }
    return _runtime_status_item(target, runtime, run_health=run_health)


def _run_id_for_call(call: dict[str, Any], started_at: str) -> str:
    suffix = _stable_hash({"call_id": call.get("id"), "started_at": started_at})
    return f"run-{suffix}"


def _call_run_blockers(target: Path, call: dict[str, Any], *, expected_status: str = "approved") -> list[str]:
    blockers: list[str] = []
    status = str(call.get("status") or "")
    if status != expected_status:
        if status == "completed":
            blockers.append("completed calls cannot be run again")
        elif status == "failed":
            blockers.append("failed calls are not approved for another run")
        elif status == "running":
            blockers.append("call is already running")
        else:
            if expected_status == "approved":
                blockers.append(f"call must be approved before run: {status or 'unknown'}")
            else:
                blockers.append(f"call must be {expected_status} before run: {status or 'unknown'}")
    if call.get("blockers"):
        blockers.append("blocked calls cannot be run")
    family = call.get("family")
    if family not in {"script", "mcp"}:
        blockers.append("only script and mcp family calls can be run")
    if not isinstance(call.get("command"), str) or not str(call.get("command")).strip():
        blockers.append("command is required")
    if _high_risk_command(call.get("command")):
        blockers.append("command shape is high risk")
    approval_fingerprint = call.get("approval_fingerprint")
    if not approval_fingerprint:
        blockers.append("approval fingerprint is missing")
    elif approval_fingerprint != _approval_fingerprint(call):
        blockers.append("approval fingerprint is stale")
    if call.get("call_fingerprint") != _stored_call_fingerprint(call):
        blockers.append("stored args or call metadata fingerprint is stale")
    current_contract, current_source = _call_current_fingerprints(target, call)
    if current_contract != call.get("contract_fingerprint"):
        blockers.append("contract fingerprint is stale")
    if current_source != call.get("source_fingerprint"):
        blockers.append("source fingerprint is stale")
    tool_id = str(call.get("tool_id") or "")
    if not tool_id:
        blockers.append("tool_id is missing")
    else:
        tool, errors = _find_tool(target, tool_id)
        if tool is None:
            blockers.extend(errors or [f"tool not found: {tool_id}"])
        else:
            if tool.get("family") != family:
                blockers.append(f"configured tool family changed: {tool.get('family')}")
            current_projection = _call_projection_summary(target, tool_id)
            if current_projection != call.get("projection_summary", {}):
                blockers.append("projection summary is stale")
            cwd_value = call.get("contract", {}).get("cwd") if isinstance(call.get("contract"), dict) else None
            cwd_path = _as_path(target, cwd_value) if cwd_value else target
            if cwd_path is None or not cwd_path.is_dir():
                blockers.append(f"cwd does not exist: {cwd_path}")
    if not _command_parts(call.get("command")):
        blockers.append("command could not be parsed")
    contract = call.get("contract") if isinstance(call.get("contract"), dict) else {}
    if contract.get("requires_runtime"):
        runtime_snapshot = _runtime_snapshot_for_call(target, call)
        if runtime_snapshot is None:
            blockers.append("runtime is required but runtime_id is missing")
        elif runtime_snapshot.get("state") == "missing":
            blockers.append(f"required runtime is missing: {runtime_snapshot.get('id')}")
        elif not runtime_snapshot.get("running"):
            blockers.append(f"required runtime is not running: {runtime_snapshot.get('id')}")
        elif runtime_snapshot.get("managed") is False:
            blockers.append(f"required runtime is not managed by Brigade: {runtime_snapshot.get('id')}")
        elif runtime_snapshot.get("health_ok") is False:
            blockers.append(f"required runtime is unhealthy: {runtime_snapshot.get('id')}")
    if family == "mcp":
        if not contract.get("runtime_id"):
            blockers.append("runtime_id is required for MCP calls")
        if not contract.get("mcp_tool_name"):
            blockers.append("mcp_tool_name is required for MCP calls")
    policy_decision = _policy_decision(target, _call_plan_from_record(call))
    blockers.extend(policy_decision["blockers"])
    return blockers


def _write_run_receipt(
    target: Path,
    *,
    call: dict[str, Any],
    run_id: str,
    started_at: str,
    completed_at: str,
    duration_seconds: float,
    status: str,
    exit_code: int | None,
    timed_out: bool,
    stdout: object,
    stderr: object,
    argv: list[str],
    cwd: Path,
    policy_decision: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_dir = runs_path(target)
    run_dir.mkdir(parents=True, exist_ok=True)
    stdout_text = "" if stdout is None else str(stdout)
    stderr_text = "" if stderr is None else str(stderr)
    contract = call.get("contract") if isinstance(call.get("contract"), dict) else {}
    runtime_snapshot = _runtime_snapshot_for_call(target, call, run_health=False)
    if policy_decision is None:
        policy_decision = _policy_decision(target, _call_plan_from_record(call))
    safe_policy = {key: value for key, value in policy_decision.items() if key != "env"}
    env_values = policy_decision.get("env") if isinstance(policy_decision.get("env"), dict) else {}
    for value in env_values.values():
        if value:
            stdout_text = stdout_text.replace(str(value), "[redacted]")
            stderr_text = stderr_text.replace(str(value), "[redacted]")
    if extra:
        extra = _redact_known_values(extra, [str(value) for value in env_values.values() if value])
    stdout_path = run_dir / f"{run_id}.stdout.log"
    stderr_path = run_dir / f"{run_id}.stderr.log"
    receipt_path = run_dir / f"{run_id}.json"
    stdout_path.write_text(stdout_text)
    stderr_path.write_text(stderr_text)
    receipt = {
        "id": run_id,
        "call_id": call.get("id"),
        "tool_id": call.get("tool_id"),
        "family": call.get("family"),
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_seconds": round(duration_seconds, 3),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "timeout": contract.get("timeout"),
        "command_label": call.get("command"),
        "argv": [_redact_text(part, 160) for part in argv],
        "cwd": str(cwd),
        "args": call.get("args"),
        "arguments": call.get("arguments"),
        "stdout_summary": _redact_text(stdout_text),
        "stderr_summary": _redact_text(stderr_text),
        "stdout_log_path": str(stdout_path),
        "stderr_log_path": str(stderr_path),
        "receipt_path": str(receipt_path),
        "contract_fingerprint": call.get("contract_fingerprint"),
        "source_fingerprint": call.get("source_fingerprint"),
        "call_fingerprint": call.get("call_fingerprint"),
        "approval_fingerprint": call.get("approval_fingerprint"),
        "approval": {
            "reviewed_at": call.get("reviewed_at"),
            "review_reason": call.get("review_reason"),
        },
        "permissions": contract.get("permissions", []),
        "effects": contract.get("effects", []),
        "runtime_id": contract.get("runtime_id"),
        "mcp_server_id": contract.get("mcp_server_id"),
        "mcp_tool_name": contract.get("mcp_tool_name"),
        "runtime": runtime_snapshot,
        "policy": safe_policy,
        "env_labels_used": policy_decision.get("env_labels_used", []),
        "projection_summary": call.get("projection_summary", {}),
    }
    if extra:
        receipt.update(extra)
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt


def _run_receipt_paths(target: Path) -> list[Path]:
    run_dir = runs_path(target)
    if not run_dir.is_dir():
        return []
    return sorted(path for path in run_dir.glob("*.json") if path.is_file())


def _read_run_receipt(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text())
    except OSError as exc:
        return None, str(exc)
    except json.JSONDecodeError as exc:
        return None, f"invalid run receipt JSON: {exc.msg}"
    if not isinstance(payload, dict):
        return None, "run receipt must be a JSON object"
    payload.setdefault("receipt_path", str(path))
    return payload, None


def _run_sort_key(receipt: dict[str, Any]) -> str:
    return str(receipt.get("started_at") or receipt.get("completed_at") or receipt.get("id") or "")


def _run_public_summary(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": receipt.get("id"),
        "status": receipt.get("status"),
        "call_id": receipt.get("call_id"),
        "tool_id": receipt.get("tool_id"),
        "family": receipt.get("family"),
        "started_at": receipt.get("started_at"),
        "completed_at": receipt.get("completed_at"),
        "duration_seconds": receipt.get("duration_seconds"),
        "exit_code": receipt.get("exit_code"),
        "timed_out": receipt.get("timed_out"),
        "timeout": receipt.get("timeout"),
        "policy": receipt.get("policy", {}),
        "runtime": receipt.get("runtime"),
        "mcp_server_id": receipt.get("mcp_server_id"),
        "mcp_tool_name": receipt.get("mcp_tool_name"),
        "mcp_request_id": receipt.get("mcp_request_id"),
        "mcp_response_summary": receipt.get("mcp_response_summary"),
        "stdout_summary": receipt.get("stdout_summary"),
        "stderr_summary": receipt.get("stderr_summary"),
        "stdout_log_path": receipt.get("stdout_log_path"),
        "stderr_log_path": receipt.get("stderr_log_path"),
        "receipt_path": receipt.get("receipt_path"),
    }


def _run_history_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    runs: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for path in _run_receipt_paths(target):
        receipt, error = _read_run_receipt(path)
        if error is not None or receipt is None:
            errors.append({"receipt_path": str(path), "error": error or "invalid run receipt"})
            continue
        summary = _run_public_summary(receipt)
        runs.append(summary)
        status = str(summary.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    runs.sort(key=_run_sort_key, reverse=True)
    return {
        "target": str(target),
        "runs_path": str(runs_path(target)),
        "runs": runs,
        "run_count": len(runs),
        "counts": counts,
        "errors": errors,
        "error_count": len(errors),
        "latest": runs[0] if runs else None,
    }


def _resolve_run_receipt(target: Path, run_id: str) -> tuple[dict[str, Any] | None, str | None]:
    matches: list[dict[str, Any]] = []
    parse_errors: list[str] = []
    for path in _run_receipt_paths(target):
        receipt, error = _read_run_receipt(path)
        if error is not None or receipt is None:
            parse_errors.append(f"{path}: {error}")
            continue
        candidate_id = str(receipt.get("id") or path.stem)
        if candidate_id.startswith(run_id) or path.stem.startswith(run_id):
            matches.append(receipt)
    if not matches:
        suffix = f"; skipped malformed receipts: {'; '.join(parse_errors)}" if parse_errors else ""
        return None, f"run not found: {run_id}{suffix}"
    if len(matches) > 1:
        return None, f"run id is ambiguous: {run_id}"
    return matches[0], None


def _checkpoint_paths(target: Path) -> list[Path]:
    path = checkpoints_path(target)
    if not path.is_dir():
        return []
    return sorted(item for item in path.glob("*.json") if item.is_file())


def _read_checkpoint(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text())
    except OSError as exc:
        return None, str(exc)
    except json.JSONDecodeError as exc:
        return None, f"invalid checkpoint JSON: {exc.msg}"
    if not isinstance(payload, dict):
        return None, "checkpoint must be a JSON object"
    payload.setdefault("checkpoint_path", str(path))
    return payload, None


def _write_checkpoint(target: Path, checkpoint: dict[str, Any]) -> Path:
    checkpoint_id = str(checkpoint.get("id") or "")
    path = checkpoints_path(target) / f"{checkpoint_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint["checkpoint_path"] = str(path)
    path.write_text(json.dumps(checkpoint, indent=2, sort_keys=True) + "\n")
    return path


def _checkpoint_public_summary(checkpoint: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": checkpoint.get("id"),
        "status": checkpoint.get("status"),
        "tool_id": checkpoint.get("tool_id"),
        "call_id": checkpoint.get("call_id"),
        "run_id": checkpoint.get("run_id"),
        "reason": checkpoint.get("reason"),
        "requested_action": checkpoint.get("requested_action"),
        "prompt": checkpoint.get("prompt"),
        "context": checkpoint.get("context", {}),
        "choices": checkpoint.get("choices", []),
        "selected_choice": checkpoint.get("selected_choice"),
        "created_at": checkpoint.get("created_at"),
        "expires_at": checkpoint.get("expires_at"),
        "reviewed_at": checkpoint.get("reviewed_at"),
        "review_reason": checkpoint.get("review_reason"),
        "resume_run_id": checkpoint.get("resume_run_id"),
        "checkpoint_path": checkpoint.get("checkpoint_path"),
    }


def _resolve_checkpoint(target: Path, checkpoint_id: str) -> tuple[dict[str, Any] | None, str | None]:
    matches: list[dict[str, Any]] = []
    for path in _checkpoint_paths(target):
        checkpoint, error = _read_checkpoint(path)
        if error is not None or checkpoint is None:
            continue
        candidate_id = str(checkpoint.get("id") or path.stem)
        if candidate_id.startswith(checkpoint_id) or path.stem.startswith(checkpoint_id):
            matches.append(checkpoint)
    if not matches:
        return None, f"checkpoint not found: {checkpoint_id}"
    if len(matches) > 1:
        return None, f"checkpoint id is ambiguous: {checkpoint_id}"
    return matches[0], None


def _normalize_checkpoint(
    target: Path,
    path: Path,
    *,
    call: dict[str, Any],
    run_id: str,
    fallback_created_at: str,
) -> tuple[dict[str, Any] | None, str | None]:
    raw, error = _read_checkpoint(path)
    if error is not None or raw is None:
        return None, error or "invalid checkpoint"
    checkpoint_id = raw.get("id")
    if not isinstance(checkpoint_id, str) or not checkpoint_id.strip():
        checkpoint_id = f"checkpoint-{_stable_hash({'path': str(path), 'call_id': call.get('id'), 'run_id': run_id})}"
    choices = raw.get("choices", raw.get("allowed_resume_choices", []))
    if isinstance(choices, str):
        choices = [choices]
    if not isinstance(choices, list):
        choices = []
    context = raw.get("context", {})
    if not isinstance(context, (dict, list)):
        context = {"value": context}
    checkpoint = {
        "id": checkpoint_id.strip(),
        "status": "pending",
        "tool_id": call.get("tool_id"),
        "call_id": call.get("id"),
        "run_id": run_id,
        "reason": _redact_text(raw.get("reason") or "tool requested operator checkpoint", 240),
        "requested_action": _redact_text(raw.get("requested_action") or raw.get("action") or "review", 240),
        "prompt": _redact_text(raw.get("prompt") or raw.get("operator_prompt") or "", 1000),
        "context": _redact_payload(context),
        "choices": [_redact_text(choice, 160) for choice in choices],
        "created_at": str(raw.get("created_at") or fallback_created_at),
        "expires_at": raw.get("expires_at"),
        "reviewed_at": None,
        "review_reason": None,
        "selected_choice": None,
        "resume_run_id": None,
        "contract_fingerprint": call.get("contract_fingerprint"),
        "source_fingerprint": call.get("source_fingerprint"),
        "call_fingerprint": call.get("call_fingerprint"),
        "approval_fingerprint": call.get("approval_fingerprint"),
        "projection_summary": call.get("projection_summary", {}),
    }
    _write_checkpoint(target, checkpoint)
    if path.name != f"{checkpoint['id']}.json":
        try:
            path.unlink()
        except OSError:
            pass
    return checkpoint, None


def _collect_run_checkpoints(
    target: Path,
    *,
    call: dict[str, Any],
    run_id: str,
    fallback_created_at: str,
    started_epoch: float,
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for path in _checkpoint_paths(target):
        try:
            if path.stat().st_mtime < started_epoch - 1:
                continue
        except OSError:
            continue
        raw, error = _read_checkpoint(path)
        if error is not None or raw is None:
            continue
        raw_call = raw.get("call_id")
        raw_run = raw.get("run_id")
        if raw_call not in (None, "", call.get("id")):
            continue
        if raw_run not in (None, "", run_id):
            continue
        if raw.get("status") not in (None, "", "pending"):
            continue
        checkpoint, normalize_error = _normalize_checkpoint(
            target,
            path,
            call=call,
            run_id=run_id,
            fallback_created_at=fallback_created_at,
        )
        if normalize_error is None and checkpoint is not None:
            found.append(checkpoint)
    found.sort(key=lambda item: str(item.get("created_at") or ""))
    return found


def _replay_plan_payload(target: Path, receipt: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    tool_id = receipt.get("tool_id")
    args = receipt.get("args")
    blockers: list[str] = []
    if not isinstance(tool_id, str) or not tool_id.strip():
        blockers.append("receipt tool_id is missing")
        tool_id = ""
    if not isinstance(args, dict):
        blockers.append("receipt does not contain replay args")
        args = {}
    plan_payload = _call_plan_payload(target, tool_id, args=json.dumps(args, sort_keys=True))
    blockers.extend(plan_payload.get("blockers", []))
    if not blockers:
        candidate = _make_call_record(plan_payload)
        candidate["status"] = "approved"
        candidate["reviewed_at"] = _now().isoformat()
        candidate["review_reason"] = f"replay validation for {receipt.get('id')}"
        candidate["approval_fingerprint"] = _approval_fingerprint(candidate)
        blockers.extend(_call_run_blockers(target, candidate))
    return plan_payload, blockers


def _replay_call_payload(target: Path, run_id: str) -> tuple[dict[str, Any], int]:
    target = target.expanduser().resolve()
    receipt, error = _resolve_run_receipt(target, run_id)
    if receipt is None:
        return {"target": str(target), "runs_path": str(runs_path(target)), "error": error}, 1
    plan_payload, blockers = _replay_plan_payload(target, receipt)
    payload: dict[str, Any] = {
        "target": str(target),
        "runs_path": str(runs_path(target)),
        "calls_path": str(calls_path(target)),
        "run": _run_public_summary(receipt),
        "plan": plan_payload,
        "blockers": blockers,
        "created": 0,
        "executed": 0,
    }
    if blockers:
        payload["error"] = "run replay is blocked"
        return payload, 1
    record = _make_call_record(plan_payload)
    record["replay_of_run_id"] = receipt.get("id")
    record["replay_source_call_id"] = receipt.get("call_id")
    record["replay_created_at"] = _now().isoformat()
    calls = _read_calls(target)
    existing_ids = {str(call.get("id")) for call in calls}
    if record["id"] in existing_ids:
        record["id"] = (
            f"{record['id']}-replay-{_stable_hash({'run_id': receipt.get('id'), 'created_at': record['replay_created_at']})}"
        )
    calls.append(record)
    _write_calls(target, calls)
    payload["call"] = record
    payload["created"] = 1
    return payload, 0


def _log_path_exists(target: Path, value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    path = Path(value)
    if not path.is_absolute():
        path = target / path
    return path.is_file()


def _run_history_health(target: Path) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for path in _run_receipt_paths(target):
        receipt, error = _read_run_receipt(path)
        if error is not None or receipt is None:
            issues.append(
                {
                    "status": WARN,
                    "name": "tool_run_receipt_invalid",
                    "issue_type": "run_receipt_invalid",
                    "detail": f"{path}: {error or 'invalid run receipt'}",
                    "run_id": path.stem,
                }
            )
            continue
        runs.append(receipt)
        status = str(receipt.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
        run_id = str(receipt.get("id") or path.stem)
        tool_id = receipt.get("tool_id")
        if status == "failed":
            issues.append(
                {
                    "status": WARN,
                    "name": "tool_run_failed",
                    "issue_type": "run_failed",
                    "tool_id": tool_id,
                    "run_id": run_id,
                    "call_id": receipt.get("call_id"),
                    "detail": f"{run_id} failed with exit_code={receipt.get('exit_code')}",
                }
            )
            if receipt.get("family") == "mcp":
                issues.append(
                    {
                        "status": WARN,
                        "name": "tool_mcp_execution_failed",
                        "issue_type": str(receipt.get("mcp_error_type") or "mcp_execution_failed"),
                        "tool_id": tool_id,
                        "run_id": run_id,
                        "call_id": receipt.get("call_id"),
                        "detail": f"{run_id} MCP execution failed: {_short(str(receipt.get('stderr_summary') or receipt.get('mcp_response_summary') or ''))}",
                    }
                )
        if receipt.get("timed_out") is True:
            issues.append(
                {
                    "status": WARN,
                    "name": "tool_run_timed_out",
                    "issue_type": "run_timed_out",
                    "tool_id": tool_id,
                    "run_id": run_id,
                    "call_id": receipt.get("call_id"),
                    "detail": f"{run_id} timed out",
                }
            )
        for key in ("stdout_log_path", "stderr_log_path"):
            if not _log_path_exists(target, receipt.get(key)):
                issues.append(
                    {
                        "status": WARN,
                        "name": "tool_run_missing_log",
                        "issue_type": "run_missing_log",
                        "tool_id": tool_id,
                        "run_id": run_id,
                        "call_id": receipt.get("call_id"),
                        "detail": f"{run_id} missing {key}",
                    }
                )
        _, blockers = _replay_plan_payload(target, receipt)
        if blockers:
            issues.append(
                {
                    "status": WARN,
                    "name": "tool_run_replay_blocked",
                    "issue_type": "run_replay_blocked",
                    "tool_id": tool_id,
                    "run_id": run_id,
                    "call_id": receipt.get("call_id"),
                    "detail": f"{run_id} replay blocked: {_short('; '.join(blockers))}",
                }
            )
    runs.sort(key=_run_sort_key, reverse=True)
    return {
        "runs_path": str(runs_path(target)),
        "run_count": len(runs),
        "counts": counts,
        "issue_count": len(issues),
        "issues": issues,
        "top_issue": issues[0] if issues else None,
        "latest": _run_public_summary(runs[0]) if runs else None,
    }


def _checkpoint_expired(checkpoint: dict[str, Any], *, now: datetime | None = None) -> bool:
    expires = _parse_iso_datetime(checkpoint.get("expires_at"))
    if expires is None:
        return False
    return (now or _now()) > expires


def _checkpoint_resume_blockers(
    target: Path, checkpoint: dict[str, Any]
) -> tuple[list[str], dict[str, Any] | None, list[dict[str, Any]]]:
    blockers: list[str] = []
    status = str(checkpoint.get("status") or "")
    if status != "approved":
        blockers.append(f"checkpoint must be approved before resume: {status or 'unknown'}")
    if _checkpoint_expired(checkpoint):
        blockers.append("checkpoint is expired")
    call_id = str(checkpoint.get("call_id") or "")
    call: dict[str, Any] | None = None
    calls: list[dict[str, Any]] = []
    if not call_id:
        blockers.append("checkpoint call_id is missing")
    else:
        call, calls, error = _resolve_call(target, call_id)
        if call is None:
            blockers.append(error or f"call not found: {call_id}")
    if call is not None:
        if checkpoint.get("contract_fingerprint") != call.get("contract_fingerprint"):
            blockers.append("checkpoint contract fingerprint is stale")
        if checkpoint.get("source_fingerprint") != call.get("source_fingerprint"):
            blockers.append("checkpoint source fingerprint is stale")
        if checkpoint.get("call_fingerprint") != call.get("call_fingerprint"):
            blockers.append("checkpoint call fingerprint is stale")
        blockers.extend(_call_run_blockers(target, call, expected_status="resume-pending"))
    return blockers, call, calls


def _checkpoint_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    checkpoints: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for path in _checkpoint_paths(target):
        checkpoint, error = _read_checkpoint(path)
        if error is not None or checkpoint is None:
            errors.append({"checkpoint_path": str(path), "error": error or "invalid checkpoint"})
            continue
        summary = _checkpoint_public_summary(checkpoint)
        checkpoints.append(summary)
        status = str(summary.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    checkpoints.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {
        "target": str(target),
        "checkpoints_path": str(checkpoints_path(target)),
        "checkpoints": checkpoints,
        "checkpoint_count": len(checkpoints),
        "counts": counts,
        "errors": errors,
        "error_count": len(errors),
    }


def _checkpoint_health(target: Path) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    checkpoints: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    now = _now()
    for path in _checkpoint_paths(target):
        checkpoint, error = _read_checkpoint(path)
        if error is not None or checkpoint is None:
            issues.append(
                {
                    "status": WARN,
                    "name": "tool_checkpoint_invalid",
                    "issue_type": "checkpoint_invalid",
                    "detail": f"{path}: {error or 'invalid checkpoint'}",
                    "checkpoint_id": path.stem,
                }
            )
            continue
        checkpoints.append(checkpoint)
        status = str(checkpoint.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
        checkpoint_id = str(checkpoint.get("id") or path.stem)
        base = {
            "tool_id": checkpoint.get("tool_id"),
            "checkpoint_id": checkpoint_id,
            "call_id": checkpoint.get("call_id"),
            "run_id": checkpoint.get("run_id"),
        }
        if _checkpoint_expired(checkpoint, now=now) and status not in {"rejected", "resumed"}:
            issues.append(
                {
                    **base,
                    "status": WARN,
                    "name": "tool_checkpoint_expired",
                    "issue_type": "checkpoint_expired",
                    "detail": f"{checkpoint_id} expired at {checkpoint.get('expires_at')}",
                }
            )
        created = _parse_iso_datetime(checkpoint.get("created_at"))
        if status in {"pending", "approved"} and created is not None:
            age_hours = (now - created).total_seconds() / 3600
            if age_hours > CALL_STALE_HOURS:
                issues.append(
                    {
                        **base,
                        "status": WARN,
                        "name": "tool_checkpoint_stale",
                        "issue_type": "checkpoint_stale",
                        "detail": f"{checkpoint_id} {status} for {age_hours:.1f}h",
                    }
                )
        if status == "approved":
            blockers, _, _ = _checkpoint_resume_blockers(target, checkpoint)
            if blockers:
                issues.append(
                    {
                        **base,
                        "status": WARN,
                        "name": "tool_checkpoint_blocked",
                        "issue_type": "checkpoint_blocked",
                        "detail": f"{checkpoint_id} resume blocked: {_short('; '.join(blockers))}",
                    }
                )
        if status == "rejected":
            issues.append(
                {
                    **base,
                    "status": WARN,
                    "name": "tool_checkpoint_rejected",
                    "issue_type": "checkpoint_rejected",
                    "detail": f"{checkpoint_id} rejected: {checkpoint.get('review_reason') or ''}".strip(),
                }
            )
        if status == "failed":
            issues.append(
                {
                    **base,
                    "status": WARN,
                    "name": "tool_checkpoint_failed",
                    "issue_type": "checkpoint_failed",
                    "detail": f"{checkpoint_id} resume failed",
                }
            )
    checkpoints.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {
        "checkpoints_path": str(checkpoints_path(target)),
        "checkpoint_count": len(checkpoints),
        "counts": counts,
        "issue_count": len(issues),
        "issues": issues,
        "top_issue": issues[0] if issues else None,
        "latest": _checkpoint_public_summary(checkpoints[0]) if checkpoints else None,
    }


def _mcp_jsonrpc_requests(call: dict[str, Any]) -> list[dict[str, Any]]:
    contract = call.get("contract") if isinstance(call.get("contract"), dict) else {}
    tool_name = str(contract.get("mcp_tool_name") or call.get("tool_id") or "")
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    return [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "brigade", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": args},
        },
    ]


def _parse_mcp_responses(stdout: object) -> tuple[list[dict[str, Any]], list[str]]:
    responses: list[dict[str, Any]] = []
    errors: list[str] = []
    for line in str(stdout or "").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"invalid JSON-RPC response: {exc.msg}")
            continue
        if not isinstance(payload, dict):
            errors.append("JSON-RPC response must be an object")
            continue
        responses.append(payload)
        if payload.get("error"):
            errors.append(f"JSON-RPC error for id={payload.get('id')}: {_short(str(payload.get('error')))}")
    return responses, errors


def _mcp_response_by_id(responses: list[dict[str, Any]], request_id: int) -> dict[str, Any] | None:
    for response in responses:
        if response.get("id") == request_id:
            return response
    return None


def _mcp_tool_list_contains(response: dict[str, Any] | None, tool_name: str) -> bool:
    result = response.get("result") if isinstance(response, dict) else None
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        return False
    for item in tools:
        if isinstance(item, dict) and item.get("name") == tool_name:
            return True
    return False


def _run_mcp_call(
    target: Path,
    *,
    call: dict[str, Any],
    run_id: str,
    cwd: Path,
    policy_decision: dict[str, Any],
    timeout_value: float | None,
) -> tuple[object, object, int | None, bool, str, dict[str, Any]]:
    contract = call.get("contract") if isinstance(call.get("contract"), dict) else {}
    env_values = policy_decision.get("env") if isinstance(policy_decision.get("env"), dict) else {}
    run_env = os.environ.copy()
    for label, value in env_values.items():
        run_env[str(label)] = str(value)
    run_env["BRIGADE_TOOL_CHECKPOINT_DIR"] = str(checkpoints_path(target))
    run_env["BRIGADE_TOOL_CALL_ID"] = str(call.get("id") or "")
    run_env["BRIGADE_TOOL_RUN_ID"] = run_id
    tool_name = str(contract.get("mcp_tool_name") or "")
    requests = _mcp_jsonrpc_requests(call)
    request_text = "".join(json.dumps(request, sort_keys=True) + "\n" for request in requests)
    started_request_id = requests[-1]["id"]
    status = "completed"
    exit_code: int | None = None
    timed_out = False
    stdout: object = ""
    stderr: object = ""
    try:
        completed = subprocess.run(
            _command_parts(call.get("command")),
            input=request_text,
            cwd=cwd,
            env=run_env,
            text=True,
            capture_output=True,
            timeout=timeout_value,
            check=False,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        exit_code = completed.returncode
        if completed.returncode != 0:
            status = "failed"
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        timed_out = True
        status = "failed"
    except OSError as exc:
        stderr = str(exc)
        status = "failed"
    responses, response_errors = _parse_mcp_responses(stdout)
    if response_errors and status == "completed":
        status = "failed"
    if status == "completed" and _mcp_response_by_id(responses, 1) is None:
        response_errors.append("missing initialize response")
        status = "failed"
    list_response = _mcp_response_by_id(responses, 2)
    if status == "completed" and not _mcp_tool_list_contains(list_response, tool_name):
        response_errors.append(f"MCP tool not listed by server: {tool_name}")
        status = "failed"
    call_response = _mcp_response_by_id(responses, 3)
    if status == "completed" and call_response is None:
        response_errors.append("missing tools/call response")
        status = "failed"
    if response_errors:
        stderr = (str(stderr or "") + "\n" + "\n".join(response_errors)).strip()
    extra = {
        "mcp_server_id": contract.get("mcp_server_id") or contract.get("runtime_id"),
        "mcp_tool_name": tool_name,
        "mcp_request_id": started_request_id,
        "mcp_request_payload": _redact_payload(requests[-1]),
        "mcp_response_summary": _redact_payload(call_response or {}),
        "mcp_response_count": len(responses),
        "mcp_error_type": "mcp_execution_failed" if status == "failed" else None,
    }
    return stdout, stderr, exit_code, timed_out, status, extra


def _next_approved_call(calls: list[dict[str, Any]]) -> dict[str, Any] | None:
    approved = [call for call in calls if call.get("status") == "approved"]
    approved.sort(key=lambda call: str(call.get("created_at") or ""))
    return approved[0] if approved else None


def _run_call_payload(
    target: Path, *, call_id: str | None = None, next_call: bool = False
) -> tuple[dict[str, Any], int]:
    target = target.expanduser().resolve()
    calls = _read_calls(target)
    call: dict[str, Any] | None
    error: str | None = None
    if next_call:
        call = _next_approved_call(calls)
        if call is None:
            error = "no approved calls available"
    elif call_id:
        call, calls, error = _resolve_call(target, call_id)
    else:
        call = None
        error = "pass a call id or --next"
    if call is None:
        return {"target": str(target), "calls_path": str(calls_path(target)), "error": error}, 1
    blockers = _call_run_blockers(target, call)
    if blockers:
        return {
            "target": str(target),
            "calls_path": str(calls_path(target)),
            "call": call,
            "blockers": blockers,
            "error": "call is not runnable",
        }, 1
    contract = call.get("contract") if isinstance(call.get("contract"), dict) else {}
    cwd_value = contract.get("cwd")
    cwd = _as_path(target, cwd_value) if cwd_value else target
    assert cwd is not None
    argv = _command_parts(call.get("command"))
    if call.get("family") != "mcp":
        for key in sorted((call.get("arguments") if isinstance(call.get("arguments"), dict) else {}).keys()):
            value = call["arguments"][key]
            if value is None:
                continue
            argv.extend(shlex.split(str(value)))
    started_at = _now().isoformat()
    run_id = _run_id_for_call(call, started_at)
    receipt_path = runs_path(target) / f"{run_id}.json"
    checkpoints_path(target).mkdir(parents=True, exist_ok=True)
    call["status"] = "running"
    call["started_at"] = started_at
    call["completed_at"] = None
    call["run_id"] = run_id
    call["receipt_path"] = str(receipt_path)
    call["exit_code"] = None
    _write_calls(target, calls)

    timeout = contract.get("timeout")
    timeout_value = float(timeout) if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) else None
    policy_decision = _policy_decision(target, _call_plan_from_record(call), include_env_values=True)
    run_env = os.environ.copy()
    env_values = policy_decision.get("env") if isinstance(policy_decision.get("env"), dict) else {}
    for label, value in env_values.items():
        run_env[str(label)] = str(value)
    run_env["BRIGADE_TOOL_CHECKPOINT_DIR"] = str(checkpoints_path(target))
    run_env["BRIGADE_TOOL_CALL_ID"] = str(call.get("id") or "")
    run_env["BRIGADE_TOOL_RUN_ID"] = run_id
    start_monotonic = time.monotonic()
    started_epoch = time.time()
    stdout: object = ""
    stderr: object = ""
    exit_code: int | None = None
    timed_out = False
    status = "completed"
    extra_receipt: dict[str, Any] = {}
    if call.get("family") == "mcp":
        stdout, stderr, exit_code, timed_out, status, extra_receipt = _run_mcp_call(
            target,
            call=call,
            run_id=run_id,
            cwd=cwd,
            policy_decision=policy_decision,
            timeout_value=timeout_value,
        )
    else:
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                env=run_env,
                text=True,
                capture_output=True,
                timeout=timeout_value,
                check=False,
            )
            stdout = completed.stdout
            stderr = completed.stderr
            exit_code = completed.returncode
            if completed.returncode != 0:
                status = "failed"
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            timed_out = True
            status = "failed"
        except OSError as exc:
            stderr = str(exc)
            status = "failed"
    duration_seconds = time.monotonic() - start_monotonic
    completed_at = _now().isoformat()
    checkpoints = _collect_run_checkpoints(
        target,
        call=call,
        run_id=run_id,
        fallback_created_at=completed_at,
        started_epoch=started_epoch,
    )
    checkpoint = checkpoints[0] if checkpoints else None
    if checkpoint is not None:
        status = "waiting"
        extra_receipt["checkpoint_id"] = checkpoint.get("id")
        extra_receipt["checkpoint"] = _checkpoint_public_summary(checkpoint)
    receipt = _write_run_receipt(
        target,
        call=call,
        run_id=run_id,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=duration_seconds,
        status=status,
        exit_code=exit_code,
        timed_out=timed_out,
        stdout=stdout,
        stderr=stderr,
        argv=argv,
        cwd=cwd,
        policy_decision=policy_decision,
        extra=extra_receipt,
    )
    call["status"] = status
    call["completed_at"] = completed_at
    call["exit_code"] = exit_code
    call["timed_out"] = timed_out
    call["receipt_path"] = receipt["receipt_path"]
    if checkpoint is not None:
        call["checkpoint_id"] = checkpoint.get("id")
    _write_calls(target, calls)
    return {
        "target": str(target),
        "calls_path": str(calls_path(target)),
        "runs_path": str(runs_path(target)),
        "call": call,
        "receipt": receipt,
    }, 0 if status in {"completed", "waiting", "resumed"} else 1


def _resume_checkpoint_payload(target: Path, checkpoint_id: str) -> tuple[dict[str, Any], int]:
    target = target.expanduser().resolve()
    checkpoint, error = _resolve_checkpoint(target, checkpoint_id)
    if checkpoint is None:
        return {"target": str(target), "checkpoints_path": str(checkpoints_path(target)), "error": error}, 1
    blockers, call, calls = _checkpoint_resume_blockers(target, checkpoint)
    if blockers or call is None:
        return {
            "target": str(target),
            "checkpoints_path": str(checkpoints_path(target)),
            "checkpoint": _checkpoint_public_summary(checkpoint),
            "blockers": blockers,
            "error": "checkpoint is not resumable",
        }, 1
    contract = call.get("contract") if isinstance(call.get("contract"), dict) else {}
    cwd_value = contract.get("cwd")
    cwd = _as_path(target, cwd_value) if cwd_value else target
    assert cwd is not None
    argv = _command_parts(call.get("command"))
    for key in sorted((call.get("arguments") if isinstance(call.get("arguments"), dict) else {}).keys()):
        value = call["arguments"][key]
        if value is None:
            continue
        argv.extend(shlex.split(str(value)))
    started_at = _now().isoformat()
    run_id = _run_id_for_call({**call, "id": f"{call.get('id')}:resume:{checkpoint.get('id')}"}, started_at)
    receipt_path = runs_path(target) / f"{run_id}.json"
    call["status"] = "running"
    call["started_at"] = started_at
    call["completed_at"] = None
    call["run_id"] = run_id
    call["receipt_path"] = str(receipt_path)
    call["exit_code"] = None
    _write_calls(target, calls)

    timeout = contract.get("timeout")
    timeout_value = float(timeout) if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) else None
    policy_decision = _policy_decision(target, _call_plan_from_record(call), include_env_values=True)
    run_env = os.environ.copy()
    env_values = policy_decision.get("env") if isinstance(policy_decision.get("env"), dict) else {}
    for label, value in env_values.items():
        run_env[str(label)] = str(value)
    run_env["BRIGADE_TOOL_CHECKPOINT_DIR"] = str(checkpoints_path(target))
    run_env["BRIGADE_TOOL_CALL_ID"] = str(call.get("id") or "")
    run_env["BRIGADE_TOOL_RUN_ID"] = run_id
    run_env["BRIGADE_TOOL_RESUME_CHECKPOINT_ID"] = str(checkpoint.get("id") or "")
    run_env["BRIGADE_TOOL_RESUME_CHOICE"] = str(checkpoint.get("selected_choice") or "")
    start_monotonic = time.monotonic()
    stdout: object = ""
    stderr: object = ""
    exit_code: int | None = None
    timed_out = False
    status = "resumed"
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=run_env,
            text=True,
            capture_output=True,
            timeout=timeout_value,
            check=False,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        exit_code = completed.returncode
        if completed.returncode != 0:
            status = "failed"
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        timed_out = True
        status = "failed"
    except OSError as exc:
        stderr = str(exc)
        status = "failed"
    completed_at = _now().isoformat()
    receipt = _write_run_receipt(
        target,
        call=call,
        run_id=run_id,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=time.monotonic() - start_monotonic,
        status=status,
        exit_code=exit_code,
        timed_out=timed_out,
        stdout=stdout,
        stderr=stderr,
        argv=argv,
        cwd=cwd,
        policy_decision=policy_decision,
        extra={
            "checkpoint_id": checkpoint.get("id"),
            "original_call_id": checkpoint.get("call_id"),
            "original_run_id": checkpoint.get("run_id"),
            "resume_run_id": run_id,
            "resume": {
                "checkpoint_id": checkpoint.get("id"),
                "selected_choice": checkpoint.get("selected_choice"),
                "reviewed_at": checkpoint.get("reviewed_at"),
                "review_reason": checkpoint.get("review_reason"),
            },
        },
    )
    call["status"] = status
    call["completed_at"] = completed_at
    call["exit_code"] = exit_code
    call["timed_out"] = timed_out
    call["receipt_path"] = receipt["receipt_path"]
    call["resume_checkpoint_id"] = checkpoint.get("id")
    _write_calls(target, calls)
    checkpoint["status"] = "resumed" if status == "resumed" else "failed"
    checkpoint["resume_run_id"] = run_id
    _write_checkpoint(target, checkpoint)
    return {
        "target": str(target),
        "calls_path": str(calls_path(target)),
        "checkpoints_path": str(checkpoints_path(target)),
        "runs_path": str(runs_path(target)),
        "checkpoint": _checkpoint_public_summary(checkpoint),
        "call": call,
        "receipt": receipt,
    }, 0 if status == "resumed" else 1


def _call_health(target: Path) -> dict[str, Any]:
    calls = _read_calls(target)
    now = _now()
    issues: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for call in calls:
        status = str(call.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
        created = _parse_iso_datetime(call.get("created_at"))
        if status == "pending" and created is not None:
            age_hours = (now - created).total_seconds() / 3600
            if age_hours > CALL_STALE_HOURS:
                issues.append(
                    {
                        "status": WARN,
                        "name": "tool_call_stale_pending",
                        "issue_type": "call_stale_pending",
                        "tool_id": call.get("tool_id"),
                        "call_id": call.get("id"),
                        "detail": f"{call.get('id')} pending for {age_hours:.1f}h",
                    }
                )
        if status == "pending" and call.get("blockers"):
            issues.append(
                {
                    "status": WARN,
                    "name": "tool_call_blocked",
                    "issue_type": "call_blocked",
                    "tool_id": call.get("tool_id"),
                    "call_id": call.get("id"),
                    "detail": f"{call.get('id')} has {len(call.get('blockers', []))} blocker(s)",
                }
            )
        if status == "approved":
            current_contract, current_source = _call_current_fingerprints(target, call)
            if current_contract != call.get("contract_fingerprint") or current_source != call.get("source_fingerprint"):
                issues.append(
                    {
                        "status": WARN,
                        "name": "tool_call_stale_approved",
                        "issue_type": "call_stale_approved",
                        "tool_id": call.get("tool_id"),
                        "call_id": call.get("id"),
                        "detail": f"{call.get('id')} approved with stale contract or source fingerprint",
                    }
                )
        if status == "running":
            started = _parse_iso_datetime(call.get("started_at"))
            if started is not None:
                age_hours = (now - started).total_seconds() / 3600
                if age_hours > CALL_RUNNING_STALE_HOURS:
                    issues.append(
                        {
                            "status": WARN,
                            "name": "tool_call_running_stale",
                            "issue_type": "call_running_stale",
                            "tool_id": call.get("tool_id"),
                            "call_id": call.get("id"),
                            "detail": f"{call.get('id')} running for {age_hours:.1f}h",
                        }
                    )
        if status == "failed":
            issues.append(
                {
                    "status": WARN,
                    "name": "tool_call_failed",
                    "issue_type": "call_failed",
                    "tool_id": call.get("tool_id"),
                    "call_id": call.get("id"),
                    "detail": f"{call.get('id')} failed with exit_code={call.get('exit_code')}",
                }
            )
        if status in {"held", "rejected"}:
            issues.append(
                {
                    "status": WARN,
                    "name": f"tool_call_{status}",
                    "issue_type": f"call_{status}",
                    "tool_id": call.get("tool_id"),
                    "call_id": call.get("id"),
                    "detail": f"{call.get('id')} is {status}: {call.get('review_reason') or ''}".strip(),
                }
            )
    return {
        "calls_path": str(calls_path(target)),
        "calls": calls,
        "counts": counts,
        "pending_count": counts.get("pending", 0),
        "issue_count": len(issues),
        "issues": issues,
        "top_issue": issues[0] if issues else None,
    }


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
    wants_runtime = runtimes_config_path(target).is_file() or any(
        tool.get("runtime_id") or tool.get("requires_runtime") for tool in tools
    )
    runtime_health = (
        _runtime_payload(target, run_health=False)
        if wants_runtime
        else {
            "config_path": str(runtimes_config_path(target)),
            "state_path": str(runtime_state_path(target)),
            "counts": {},
            "runtime_count": 0,
            "issue_count": 0,
            "top_issue": None,
            "issues": [],
            "runtimes": [],
        }
    )
    issues.extend(runtime_health["issues"])
    issues.extend(_tool_runtime_issues(target, tools, runtime_health))
    policy_health = _policy_health(target, tools)
    issues.extend(policy_health["issues"])
    call_health = _call_health(target)
    issues.extend(call_health["issues"])
    run_health = _run_history_health(target)
    issues.extend(run_health["issues"])
    checkpoint_health = _checkpoint_health(target)
    issues.extend(checkpoint_health["issues"])
    if errors:
        issues.insert(0, {"status": WARN, "name": "tool_config", "issue_type": "config", "detail": "; ".join(errors)})
    raw_issues = list(issues)
    parity = _apply_parity_closeout(target, raw_issues)
    issues = parity["issues"]
    return {
        "target": str(target),
        "config_path": str(config_path(target)),
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


def health(target: Path) -> dict[str, Any]:
    payload = _catalog_payload(target)
    packs = _tool_pack_health(target)
    sync_plan = _sync_plan_summary(target)
    return {
        "config_path": payload["config_path"],
        "valid": payload["valid"],
        "tool_count": payload["tool_count"],
        "raw_issue_count": payload["raw_issue_count"],
        "issue_count": payload["issue_count"],
        "top_issue": payload["top_issue"],
        "issues": payload["issues"],
        "parity": payload["parity"],
        "packs": packs,
        "sync_plan": sync_plan,
        "call_queue": payload["call_queue"],
        "run_history": payload["run_history"],
        "checkpoints": payload["checkpoints"],
        "runtimes": payload["runtimes"],
        "policy": payload["policy"],
    }


def _issue_records(target: Path) -> list[dict[str, Any]]:
    payload = _catalog_payload(target)
    records: list[dict[str, Any]] = []
    for issue in payload["issues"]:
        issue_type = str(issue.get("issue_type") or issue.get("name") or "tool_issue")
        tool_id = str(issue.get("tool_id") or "catalog")
        detail = str(issue.get("detail") or "")
        source_fingerprint = str(
            issue.get("parity_fingerprint")
            or _stable_hash(
                {
                    "tool_id": tool_id,
                    "issue_type": issue_type,
                    "detail": detail,
                    "harness": issue.get("harness"),
                    "call_id": issue.get("call_id"),
                    "run_id": issue.get("run_id"),
                    "checkpoint_id": issue.get("checkpoint_id"),
                    "projection_target": issue.get("projection_target"),
                    "projection_status": issue.get("projection_status"),
                    "tool_source_fingerprint": issue.get("tool_source_fingerprint"),
                    "expected_projection_fingerprint": issue.get("expected_projection_fingerprint"),
                    "actual_projection_fingerprint": issue.get("actual_projection_fingerprint"),
                }
            )
        )
        metadata = {
            "tool_id": tool_id,
            "tool_family": issue.get("family"),
            "tool_issue_type": issue_type,
            "tool_harness": issue.get("harness"),
            "tool_call_id": issue.get("call_id"),
            "tool_run_id": issue.get("run_id"),
            "tool_checkpoint_id": issue.get("checkpoint_id"),
            "projection_target": issue.get("projection_target"),
            "tool_issue_detail": detail,
            "source_item_key": f"tool-catalog:{tool_id}:{issue_type}:{issue.get('harness') or ''}:{issue.get('call_id') or ''}:{issue.get('run_id') or ''}:{issue.get('checkpoint_id') or ''}",
            "source_fingerprint": source_fingerprint,
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


def init(*, target: Path, force: bool = False, update_gitignore: bool = True, default_tools: bool = True) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = config_path(target)
    if path.exists() and not force:
        print(f"error: tool catalog config already exists: {path}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    # A minimal install gets an empty catalog: no default tools and no
    # tracked tools/*.md sources until the user opts in (--full).
    path.write_text(_format_tools_toml() if default_tools else _format_tool_entries([]))
    source_results = _ensure_default_tool_sources(target) if default_tools else []
    print(f"tools_config: {path}")
    print(f"tools: {len(DEFAULT_TOOLS) if default_tools else 0}")
    print(f"sources: {sum(1 for item in source_results if item['action'] == 'create')}")
    if update_gitignore:
        result = apply_gitignore(target, _gitignore_selection(target))
        print(f"gitignore: {result}")
    else:
        print("gitignore: skipped")
    print("next_command: brigade tools list")
    return 0


def _read_tool_entries(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.is_file():
        return [], []
    if tomllib is None:
        return [], ["tool catalog requires Python tomllib support"]
    try:
        payload = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:  # type: ignore[union-attr]
        return [], [f"invalid tool catalog config: {exc}"]
    values = payload.get("tool")
    if values is None:
        return [], []
    if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
        return [], ["tool catalog must contain [[tool]] table entries"]
    return [dict(item) for item in values], []


def _projection_scope(target: Path) -> set[str] | None:
    """Return the projection targets wired for this workspace, or None for all.

    A workspace with a Brigade selection (`.brigade/config.json`) only gets
    default projections for its selected harnesses plus the neutral `scripts`
    folder. Without a selection, defaults stay unscoped.
    """
    try:
        config = load_brigade_config(target)
    except Exception:
        return None
    if config is None:
        return None
    harnesses = [h for h in (config.selection.harnesses or []) if isinstance(h, str) and h.strip()]
    if not harnesses:
        return None
    scope = set(harnesses)
    owner = str(config.selection.owner or "").strip()
    if owner:
        scope.add(owner)
    scope.add("scripts")
    return scope


def _gitignore_selection(target: Path) -> Selection:
    """Use the workspace's configured selection for gitignore rewrites.

    Re-applying the managed gitignore block with a narrower selection than the
    install drops inbox entries for the other selected harnesses.
    """
    try:
        config = load_brigade_config(target)
    except Exception:
        config = None
    if config is not None and config.selection.harnesses:
        return config.selection
    return Selection("repo", ["codex"], "codex")


def _scoped_default_tool(tool: dict[str, Any], scope: set[str] | None) -> dict[str, Any]:
    entry = dict(tool)
    if scope is None:
        return entry
    entry["supported_harnesses"] = [h for h in entry.get("supported_harnesses", []) if h in scope]
    entry["projections"] = {h: p for h, p in (entry.get("projections") or {}).items() if h in scope}
    return entry


def defaults(
    *,
    target: Path,
    dry_run: bool = False,
    force: bool = False,
    update_gitignore: bool = True,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = config_path(target)
    entries, errors = _read_tool_entries(path)
    if errors:
        payload = {
            "target": str(target),
            "config_path": str(path),
            "valid": False,
            "errors": errors,
            "dry_run": dry_run,
            "force": force,
            "created": False,
            "added": [],
            "updated": [],
            "skipped": [],
            "conflicts": [],
        }
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            for error in errors:
                print(f"error: {error}", file=sys.stderr)
        return 1
    existing_by_id = {
        str(entry.get("id")): index
        for index, entry in enumerate(entries)
        if isinstance(entry.get("id"), str) and str(entry.get("id")).strip()
    }
    projection_scope = _projection_scope(target)
    defaults_by_id = {str(tool["id"]): _scoped_default_tool(tool, projection_scope) for tool in DEFAULT_TOOLS}
    added: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    conflicts: list[dict[str, str]] = []
    merged = [dict(entry) for entry in entries]
    for tool_id, default_entry in defaults_by_id.items():
        if tool_id not in existing_by_id:
            merged.append(dict(default_entry))
            added.append(tool_id)
            continue
        index = existing_by_id[tool_id]
        existing = merged[index]
        existing_source = str(existing.get("source_path") or "")
        default_source = str(default_entry.get("source_path") or "")
        if force or existing_source == default_source:
            if existing == default_entry:
                skipped.append(tool_id)
            else:
                merged[index] = dict(default_entry)
                updated.append(tool_id)
            continue
        conflicts.append(
            {
                "tool_id": tool_id,
                "existing_source_path": existing_source,
                "default_source_path": default_source,
                "detail": "existing tool id uses a different source_path",
            }
        )
    created = not path.exists()
    if not dry_run and not conflicts:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_format_tool_entries(merged))
    source_results = _ensure_default_tool_sources(target, dry_run=dry_run or bool(conflicts))
    gitignore_result = "skipped"
    if update_gitignore and not dry_run and not conflicts:
        gitignore_result = apply_gitignore(target, _gitignore_selection(target))
    payload = {
        "target": str(target),
        "config_path": str(path),
        "valid": not conflicts,
        "errors": [],
        "dry_run": dry_run,
        "force": force,
        "created": created,
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "conflicts": conflicts,
        "sources": source_results,
        "source_created_count": len([item for item in source_results if item.get("action") == "create"]),
        "source_skipped_count": len([item for item in source_results if item.get("action") == "skip"]),
        "tool_count": len(merged),
        "default_count": len(defaults_by_id),
        "projection_scope": sorted(projection_scope) if projection_scope is not None else None,
        "gitignore": gitignore_result,
        "next_command": "brigade tools apply --target . --all",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"tools defaults: {target}")
    print(f"config_path: {path}")
    print(f"dry_run: {dry_run}")
    print(f"created: {'yes' if created else 'no'}")
    print(f"added: {len(added)}")
    print(f"updated: {len(updated)}")
    print(f"skipped: {len(skipped)}")
    print(f"conflicts: {len(conflicts)}")
    print(f"sources_created: {payload['source_created_count']}")
    print(f"gitignore: {gitignore_result}")
    for tool_id in added:
        print(f"- added: {tool_id}")
    for tool_id in updated:
        print(f"- updated: {tool_id}")
    for conflict in conflicts:
        print(f"- conflict: {conflict['tool_id']} {conflict['detail']}")
    print(f"next_command: {payload['next_command']}")
    return 0 if payload["valid"] else 1


def runtime_init(*, target: Path, force: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = runtimes_config_path(target)
    if path.exists() and not force:
        print(f"error: tool runtime config already exists: {path}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_format_runtimes_toml())
    runtime_state_path(target).mkdir(parents=True, exist_ok=True)
    print(f"runtime_config: {path}")
    print(f"runtimes: {len(DEFAULT_RUNTIMES)}")
    print("next_command: brigade tools runtime list")
    return 0


def runtime_list(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _runtime_payload(target, run_health=False)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"tools runtime list: {target}")
    print(f"config_path: {payload['config_path']}")
    if payload["errors"]:
        for error in payload["errors"]:
            print(f"error: {error}")
        return 1
    print(f"runtimes: {payload['runtime_count']}")
    for runtime in payload["runtimes"]:
        print(f"- {runtime.get('id')} [{runtime.get('state')}] port={runtime.get('port') or ''}")
    return 0


def runtime_show(*, target: Path, runtime_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _runtime_payload(target, runtime_id=runtime_id, run_health=False)
    runtime = payload["runtimes"][0] if payload["runtimes"] else None
    result = {**payload, "runtime": runtime}
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if runtime is not None and payload["valid"] else 1
    if runtime is None:
        print(f"error: runtime not found: {runtime_id}", file=sys.stderr)
        return 1
    print(f"runtime: {runtime.get('id')}")
    print(f"name: {runtime.get('name')}")
    print(f"state: {runtime.get('state')}")
    print(f"pid: {runtime.get('pid') or ''}")
    print(f"command: {runtime.get('command')}")
    print(f"cwd: {runtime.get('cwd')}")
    print(f"pid_path: {runtime.get('pid_path')}")
    return 0


def runtime_status(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _runtime_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"tools runtime status: {target}")
    print(f"config_path: {payload['config_path']}")
    if payload["errors"]:
        for error in payload["errors"]:
            print(f"error: {error}")
        return 1
    for state, count in sorted(payload["counts"].items()):
        print(f"{state}: {count}")
    for runtime in payload["runtimes"]:
        print(f"- {runtime.get('id')} [{runtime.get('state')}] health={runtime.get('health_ok')}")
    return 0


def runtime_start(*, target: Path, runtime_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload, rc = _start_runtime_payload(target, runtime_id)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    print(f"tools runtime start: {runtime_id}")
    if payload.get("error"):
        print(f"error: {payload['error']}", file=sys.stderr)
        for blocker in payload.get("blockers", []):
            print(f"- {blocker}", file=sys.stderr)
        return rc
    print(f"started: {payload.get('started', 0)}")
    print(f"skipped: {payload.get('skipped', 0)}")
    print(f"pid: {payload.get('pid') or payload.get('runtime', {}).get('pid') or ''}")
    return rc


def runtime_stop(*, target: Path, runtime_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload, rc = _stop_runtime_payload(target, runtime_id)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    print(f"tools runtime stop: {runtime_id}")
    if payload.get("error"):
        print(f"error: {payload['error']}", file=sys.stderr)
        return rc
    print(f"stopped: {payload.get('stopped', 0)}")
    if payload.get("reason"):
        print(f"reason: {payload['reason']}")
    return rc


def runtime_restart(*, target: Path, runtime_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload, rc = _restart_runtime_payload(target, runtime_id)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    print(f"tools runtime restart: {runtime_id}")
    if payload.get("error"):
        print(f"error: {payload['error']}", file=sys.stderr)
        return rc
    print(f"state: {payload.get('runtime', {}).get('state')}")
    print(f"pid: {payload.get('runtime', {}).get('pid') or ''}")
    return rc


def runtime_doctor(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _runtime_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"tools runtime doctor: {target}")
    print(f"config_path: {payload['config_path']}")
    if payload["errors"]:
        for error in payload["errors"]:
            print(f"[warn] runtime_config: {error}")
    if payload["issues"]:
        for issue in payload["issues"]:
            print(f"[{issue.get('status', WARN)}] {issue.get('name')}: {issue.get('detail')}")
    else:
        print("[ok] tool_runtimes: no issues")
    print(f"runtime_issues: {payload['issue_count']}")
    return 0 if payload["valid"] else 1


def policy_init(*, target: Path, force: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = policy_path(target)
    if path.exists() and not force:
        print(f"error: tool execution policy already exists: {path}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_format_policy_toml())
    print(f"policy_config: {path}")
    print("next_command: brigade tools policy show")
    return 0


def policy_show(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    policy, errors = _load_policy_config(target)
    payload = {
        "target": str(target),
        "policy_path": str(policy_path(target)),
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
    tools, tool_errors = _load_config(target)
    payload = _policy_health(target, tools)
    payload["target"] = str(target)
    payload["tool_errors"] = tool_errors
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["enabled"] and payload["valid"] else 1
    print(f"tools policy doctor: {target}")
    print(f"policy_path: {payload['policy_path']}")
    for error in payload.get("errors", []):
        print(f"[warn] tool_policy: {error}")
    if payload["issues"]:
        for issue in payload["issues"]:
            print(f"[{issue.get('status', WARN)}] {issue.get('name')}: {issue.get('detail')}")
    else:
        print("[ok] tool_policy: no issues")
    print(f"policy_issues: {payload['issue_count']}")
    return 0 if payload["enabled"] and payload["valid"] else 1


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
        print(
            json.dumps(
                {"target": str(target), "config_path": payload["config_path"], "tool": tool}, indent=2, sort_keys=True
            )
        )
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
        and needle in " ".join(str(tool.get(key, "")) for key in ("id", "name", "family", "description")).casefold()
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


def describe(*, target: Path, tool_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _describe_payload(target, tool_id)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] and payload["tool"] is not None else 1
    if payload["errors"]:
        for error in payload["errors"]:
            print(f"error: {error}", file=sys.stderr)
        return 1
    tool = payload["tool"]
    assert isinstance(tool, dict)
    contract = tool.get("contract") if isinstance(tool.get("contract"), dict) else {}
    print(f"tool: {tool.get('id')}")
    print(f"name: {tool.get('name')}")
    print(f"family: {tool.get('family')}")
    print(f"description: {tool.get('description')}")
    print(f"command: {contract.get('command') or ''}")
    print(f"approval_mode: {contract.get('approval_mode')}")
    print(f"input_schema: {contract.get('input_schema_path') or ''}")
    print(f"output_schema: {contract.get('output_schema_path') or ''}")
    print(f"permissions: {', '.join(contract.get('permissions', []))}")
    print(f"effects: {', '.join(contract.get('effects', []))}")
    print(f"contract_issues: {payload['issue_count']}")
    return 0


def contracts(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _contracts_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"tools contracts: {target}")
    print(f"config_path: {payload['config_path']}")
    if payload["errors"]:
        for error in payload["errors"]:
            print(f"error: {error}")
        return 1
    print(f"contracts: {payload['contract_count']}")
    print(f"contract_issues: {payload['issue_count']}")
    for contract in payload["contracts"]:
        status = "ready" if contract.get("has_contract") and contract.get("issue_count") == 0 else "needs-review"
        print(f"- {contract.get('tool_id')} [{contract.get('family')}] {status} issues={contract.get('issue_count')}")
        print(f"  input_schema: {contract.get('input_schema_path') or ''}")
        print(f"  approval_mode: {contract.get('approval_mode')}")
    return 0


def call_plan(
    *,
    target: Path,
    tool_id: str,
    args: str | None = None,
    args_json: Path | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _call_plan_payload(target, tool_id, args=args, args_json=args_json)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"tools call plan: {tool_id}")
    print(f"target: {target}")
    blockers = payload.get("blockers") if isinstance(payload.get("blockers"), list) else []
    if blockers:
        print(f"blockers: {len(blockers)}")
        for blocker in blockers:
            print(f"- {blocker}")
        return 1
    plan_payload = payload["plan"]
    print(f"command: {plan_payload.get('command')}")
    print(f"approval_mode: {plan_payload.get('approval_mode')}")
    print(f"approval_required: {plan_payload.get('approval_required')}")
    print(f"permissions: {', '.join(plan_payload.get('permissions', []))}")
    print(f"effects: {', '.join(plan_payload.get('effects', []))}")
    print("arguments:")
    for key, value in plan_payload.get("arguments", {}).items():
        print(f"  {key}: {value}")
    return 0


def call_queue(
    *,
    target: Path,
    tool_id: str,
    args: str | None = None,
    args_json: Path | None = None,
    include_blocked: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload, rc = _queue_call_payload(
        target,
        tool_id,
        args=args,
        args_json=args_json,
        include_blocked=include_blocked,
    )
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    print(f"tools call queue: {tool_id}")
    print(f"calls_path: {payload['calls_path']}")
    print(f"created: {payload['created']}")
    print(f"skipped: {payload['skipped']}")
    print(f"blocked: {payload['blocked']}")
    if payload.get("reason"):
        print(f"reason: {payload['reason']}")
    call = payload.get("call") if isinstance(payload.get("call"), dict) else {}
    if call:
        print(f"call: {call.get('id')}")
        print(f"status: {call.get('status')}")
        print(f"blockers: {len(call.get('blockers', [])) if isinstance(call.get('blockers'), list) else 0}")
    return rc


def call_list(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    calls = _read_calls(target)
    counts: dict[str, int] = {}
    for call in calls:
        status = str(call.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    payload = {"target": str(target), "calls_path": str(calls_path(target)), "calls": calls, "counts": counts}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"tools call list: {target}")
    print(f"calls_path: {calls_path(target)}")
    print(f"calls: {len(calls)}")
    for status, count in sorted(counts.items()):
        print(f"{status}: {count}")
    for call in calls:
        print(
            f"- {call.get('id')} [{call.get('status')}] {call.get('tool_id')} blockers={len(call.get('blockers', []))}"
        )
    return 0


def call_show(*, target: Path, call_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    call, _, error = _resolve_call(target, call_id)
    payload = {"target": str(target), "calls_path": str(calls_path(target)), "call": call, "error": error}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if call is not None else 1
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    assert call is not None
    print(f"call: {call.get('id')}")
    print(f"tool_id: {call.get('tool_id')}")
    print(f"status: {call.get('status')}")
    print(f"created_at: {call.get('created_at')}")
    if call.get("reviewed_at"):
        print(f"reviewed_at: {call.get('reviewed_at')}")
    if call.get("review_reason"):
        print(f"review_reason: {call.get('review_reason')}")
    print(f"blockers: {len(call.get('blockers', []))}")
    return 0


def _call_review(
    *,
    target: Path,
    call_id: str,
    status: str,
    reason: str | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    call, calls, error = _resolve_call(target, call_id)
    if call is None:
        payload = {"target": str(target), "error": error}
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"error: {error}", file=sys.stderr)
        return 1
    if status == "approved" and call.get("blockers"):
        payload = {"target": str(target), "error": "blocked calls cannot be approved", "call": call}
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print("error: blocked calls cannot be approved", file=sys.stderr)
        return 1
    call["status"] = status
    call["reviewed_at"] = _now().isoformat()
    call["review_reason"] = reason
    call["approval_fingerprint"] = _approval_fingerprint(call) if status == "approved" else None
    _write_calls(target, calls)
    payload = {"target": str(target), "calls_path": str(calls_path(target)), "call": call}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"call: {call.get('id')}")
    print(f"status: {call.get('status')}")
    if reason:
        print(f"review_reason: {reason}")
    return 0


def call_approve(*, target: Path, call_id: str, json_output: bool = False) -> int:
    return _call_review(target=target, call_id=call_id, status="approved", json_output=json_output)


def call_reject(*, target: Path, call_id: str, reason: str, json_output: bool = False) -> int:
    return _call_review(target=target, call_id=call_id, status="rejected", reason=reason, json_output=json_output)


def call_hold(*, target: Path, call_id: str, reason: str, json_output: bool = False) -> int:
    return _call_review(target=target, call_id=call_id, status="held", reason=reason, json_output=json_output)


def call_run(*, target: Path, call_id: str | None = None, next_call: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if bool(call_id) == bool(next_call):
        print("error: pass exactly one call id or --next", file=sys.stderr)
        return 2
    payload, rc = _run_call_payload(target, call_id=call_id, next_call=next_call)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    if payload.get("error"):
        print(f"error: {payload['error']}", file=sys.stderr)
        for blocker in payload.get("blockers", []):
            print(f"- {blocker}", file=sys.stderr)
        return rc
    call = payload["call"]
    receipt = payload["receipt"]
    print(f"tools call run: {call.get('id')}")
    print(f"status: {call.get('status')}")
    print(f"exit_code: {call.get('exit_code')}")
    print(f"receipt_path: {receipt.get('receipt_path')}")
    print(f"stdout_summary: {receipt.get('stdout_summary')}")
    print(f"stderr_summary: {receipt.get('stderr_summary')}")
    return rc


def run_list(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _run_history_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"tools run list: {target}")
    print(f"runs_path: {payload['runs_path']}")
    print(f"runs: {payload['run_count']}")
    for status, count in sorted(payload["counts"].items()):
        print(f"{status}: {count}")
    for error in payload["errors"]:
        print(f"[warn] run_receipt_invalid: {error.get('receipt_path')} {error.get('error')}")
    for run in payload["runs"]:
        print(f"- {run.get('id')} [{run.get('status')}] {run.get('tool_id')} exit_code={run.get('exit_code')}")
    return 0


def run_show(*, target: Path, run_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    receipt, error = _resolve_run_receipt(target, run_id)
    payload = {
        "target": str(target),
        "runs_path": str(runs_path(target)),
        "run": _run_public_summary(receipt) if receipt is not None else None,
        "error": error,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if receipt is not None else 1
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    assert receipt is not None
    run = _run_public_summary(receipt)
    print(f"run: {run.get('id')}")
    print(f"tool_id: {run.get('tool_id')}")
    print(f"call_id: {run.get('call_id')}")
    print(f"status: {run.get('status')}")
    print(f"started_at: {run.get('started_at')}")
    print(f"completed_at: {run.get('completed_at')}")
    print(f"duration_seconds: {run.get('duration_seconds')}")
    print(f"exit_code: {run.get('exit_code')}")
    print(f"timed_out: {run.get('timed_out')}")
    print(f"stdout_summary: {run.get('stdout_summary')}")
    print(f"stderr_summary: {run.get('stderr_summary')}")
    print(f"stdout_log_path: {run.get('stdout_log_path')}")
    print(f"stderr_log_path: {run.get('stderr_log_path')}")
    return 0


def run_latest(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _run_history_payload(target)
    latest = payload["latest"]
    output = {"target": str(target), "runs_path": payload["runs_path"], "run": latest}
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0 if latest is not None else 1
    if latest is None:
        print(f"tools run latest: none ({payload['runs_path']})")
        return 1
    print(f"run: {latest.get('id')}")
    print(f"tool_id: {latest.get('tool_id')}")
    print(f"status: {latest.get('status')}")
    print(f"started_at: {latest.get('started_at')}")
    print(f"exit_code: {latest.get('exit_code')}")
    print(f"receipt_path: {latest.get('receipt_path')}")
    return 0


def run_replay(*, target: Path, run_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload, rc = _replay_call_payload(target, run_id)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    if payload.get("error"):
        print(f"error: {payload['error']}", file=sys.stderr)
        for blocker in payload.get("blockers", []):
            print(f"- {blocker}", file=sys.stderr)
        return rc
    call = payload["call"]
    run = payload["run"]
    print(f"tools run replay: {run.get('id')}")
    print(f"call: {call.get('id')}")
    print(f"status: {call.get('status')}")
    print("executed: 0")
    print(f"next_command: brigade tools call approve {call.get('id')}")
    return rc


def checkpoint_list(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _checkpoint_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"tools checkpoint list: {target}")
    print(f"checkpoints_path: {payload['checkpoints_path']}")
    print(f"checkpoints: {payload['checkpoint_count']}")
    for status, count in sorted(payload["counts"].items()):
        print(f"{status}: {count}")
    for error in payload["errors"]:
        print(f"[warn] checkpoint_invalid: {error.get('checkpoint_path')} {error.get('error')}")
    for checkpoint in payload["checkpoints"]:
        print(
            f"- {checkpoint.get('id')} [{checkpoint.get('status')}] {checkpoint.get('tool_id')} {checkpoint.get('requested_action')}"
        )
    return 0


def checkpoint_show(*, target: Path, checkpoint_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    checkpoint, error = _resolve_checkpoint(target, checkpoint_id)
    payload = {
        "target": str(target),
        "checkpoints_path": str(checkpoints_path(target)),
        "checkpoint": _checkpoint_public_summary(checkpoint) if checkpoint is not None else None,
        "error": error,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if checkpoint is not None else 1
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    assert checkpoint is not None
    summary = _checkpoint_public_summary(checkpoint)
    print(f"checkpoint: {summary.get('id')}")
    print(f"status: {summary.get('status')}")
    print(f"tool_id: {summary.get('tool_id')}")
    print(f"call_id: {summary.get('call_id')}")
    print(f"run_id: {summary.get('run_id')}")
    print(f"reason: {summary.get('reason')}")
    print(f"requested_action: {summary.get('requested_action')}")
    print(f"prompt: {summary.get('prompt')}")
    print(f"choices: {', '.join(str(choice) for choice in summary.get('choices', []))}")
    if summary.get("selected_choice"):
        print(f"selected_choice: {summary.get('selected_choice')}")
    return 0


def _checkpoint_review(
    *,
    target: Path,
    checkpoint_id: str,
    status: str,
    choice: str | None = None,
    reason: str | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    checkpoint, error = _resolve_checkpoint(target, checkpoint_id)
    if checkpoint is None:
        payload = {"target": str(target), "error": error}
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"error: {error}", file=sys.stderr)
        return 1
    if status == "approved":
        choices = [str(item) for item in checkpoint.get("choices", []) if isinstance(item, str)]
        if choices and choice not in choices:
            payload = {
                "target": str(target),
                "error": "choice is not allowed",
                "checkpoint": _checkpoint_public_summary(checkpoint),
            }
            if json_output:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print("error: choice is not allowed", file=sys.stderr)
            return 1
    checkpoint["status"] = status
    checkpoint["reviewed_at"] = _now().isoformat()
    checkpoint["review_reason"] = reason
    if status == "approved":
        checkpoint["selected_choice"] = choice
    _write_checkpoint(target, checkpoint)
    call: dict[str, Any] | None = None
    calls: list[dict[str, Any]] = []
    call_id = checkpoint.get("call_id")
    if isinstance(call_id, str) and call_id:
        call, calls, _ = _resolve_call(target, call_id)
    if call is not None and status == "approved":
        call["status"] = "resume-pending"
        call["checkpoint_id"] = checkpoint.get("id")
        call["approval_fingerprint"] = _approval_fingerprint(call)
        _write_calls(target, calls)
    payload = {
        "target": str(target),
        "checkpoints_path": str(checkpoints_path(target)),
        "checkpoint": _checkpoint_public_summary(checkpoint),
        "call": call,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"checkpoint: {checkpoint.get('id')}")
    print(f"status: {checkpoint.get('status')}")
    if choice:
        print(f"selected_choice: {choice}")
    if reason:
        print(f"review_reason: {reason}")
    return 0


def checkpoint_approve(*, target: Path, checkpoint_id: str, choice: str, json_output: bool = False) -> int:
    return _checkpoint_review(
        target=target,
        checkpoint_id=checkpoint_id,
        status="approved",
        choice=choice,
        json_output=json_output,
    )


def checkpoint_reject(*, target: Path, checkpoint_id: str, reason: str, json_output: bool = False) -> int:
    return _checkpoint_review(
        target=target,
        checkpoint_id=checkpoint_id,
        status="rejected",
        reason=reason,
        json_output=json_output,
    )


def checkpoint_resume(*, target: Path, checkpoint_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload, rc = _resume_checkpoint_payload(target, checkpoint_id)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    if payload.get("error"):
        print(f"error: {payload['error']}", file=sys.stderr)
        for blocker in payload.get("blockers", []):
            print(f"- {blocker}", file=sys.stderr)
        return rc
    checkpoint = payload["checkpoint"]
    receipt = payload["receipt"]
    print(f"tools checkpoint resume: {checkpoint.get('id')}")
    print(f"status: {checkpoint.get('status')}")
    print(f"resume_run_id: {receipt.get('id')}")
    print(f"exit_code: {receipt.get('exit_code')}")
    print(f"receipt_path: {receipt.get('receipt_path')}")
    return rc


def plan(*, target: Path, tool_id: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _projection_plan_payload(target, tool_id=tool_id)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"tools projection plan: {target}")
    print(f"config_path: {payload['config_path']}")
    if tool_id is not None:
        print(f"tool_id: {tool_id}")
    if payload["errors"]:
        for error in payload["errors"]:
            print(f"error: {error}")
        return 1
    projections = payload["projections"]
    print(f"projections: {len(projections)}")
    if payload["counts"]:
        print("counts:")
        for status, count in sorted(payload["counts"].items()):
            print(f"  {status}: {count}")
    for item in projections:
        print(f"- {item.get('tool_id')} {item.get('harness')} {item.get('status')} action={item.get('action')}")
        print(f"  source: {item.get('source_path')}")
        print(f"  target: {item.get('projection_path')}")
        if item.get("expected_fingerprint"):
            print(f"  expected_fingerprint: {item.get('expected_fingerprint')}")
        print(f"  detail: {item.get('detail')}")
    return 0


def apply(
    *,
    target: Path,
    tool_id: str | None = None,
    all_tools: bool = False,
    dry_run: bool = False,
    force: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if bool(tool_id) == bool(all_tools):
        print("error: pass exactly one of <tool-id> or --all", file=sys.stderr)
        return 2
    tools, errors = _load_config(target)
    selected = [tool for tool in tools if tool.get("enabled", True) and (all_tools or tool.get("id") == tool_id)]
    if tool_id is not None and not selected and not errors:
        errors.append(f"tool not found: {tool_id}")
    generated_at = datetime.now(timezone.utc)
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for tool in selected:
        for harness in tool.get("supported_harnesses", []):
            item = _projection_item(target, tool, harness, generated_at=generated_at, force=force)
            public_item = {key: value for key, value in item.items() if key != "rendered"}
            action = item.get("action")
            if action == "conflict":
                conflicts.append(public_item)
                continue
            if action not in {"create", "update"}:
                skipped.append(public_item)
                continue
            if dry_run:
                applied.append({**public_item, "dry_run": True})
                continue
            projection_path = Path(str(item["projection_path"]))
            projection_path.parent.mkdir(parents=True, exist_ok=True)
            projection_path.write_text(str(item["rendered"]))
            applied.append(public_item)
    payload = {
        "target": str(target),
        "config_path": str(config_path(target)),
        "valid": not errors,
        "errors": errors,
        "tool_id": tool_id,
        "all": all_tools,
        "dry_run": dry_run,
        "force": force,
        "applied": applied,
        "skipped": skipped,
        "conflicts": conflicts,
        "applied_count": len(applied),
        "skipped_count": len(skipped),
        "conflict_count": len(conflicts),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if not errors and not conflicts else 1
    print(f"tools projection apply: {target}")
    print(f"config_path: {config_path(target)}")
    if errors:
        for error in errors:
            print(f"error: {error}")
        return 1
    print(f"dry_run: {dry_run}")
    print(f"force: {force}")
    print(f"applied: {len(applied)}")
    print(f"skipped: {len(skipped)}")
    print(f"conflicts: {len(conflicts)}")
    for item in applied:
        verb = "would_write" if dry_run else "wrote"
        print(f"- {verb}: {item.get('tool_id')} {item.get('harness')} {item.get('projection_path')}")
    for item in conflicts:
        print(f"- conflict: {item.get('tool_id')} {item.get('harness')} {item.get('detail')}")
    return 0 if not conflicts else 1


def _packs_root(target: Path) -> Path:
    return target / ".brigade" / "tools" / "packs"


def _packs_archive_root(target: Path) -> Path:
    return target / ".brigade" / "tools" / "packs-archive"


def _tool_pack_payload(target: Path) -> dict[str, Any]:
    catalog = _catalog_payload(target)
    projection = _projection_plan_payload(target)
    payload = {
        "target": catalog["target"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tool_count": catalog["tool_count"],
        "tools": catalog["tools"],
        "projection_counts": projection["counts"],
        "projections": projection["projections"],
        "parity": catalog["parity"],
        "policy": catalog["policy"],
        "runtimes": catalog["runtimes"],
        "call_queue": catalog["call_queue"],
        "run_history": catalog["run_history"],
        "checkpoints": catalog["checkpoints"],
        "issues": catalog["issues"],
        "issue_count": catalog["issue_count"],
    }
    payload["evidence_fingerprint"] = _tool_pack_evidence_fingerprint(payload)
    return payload


def _tool_pack_evidence_fingerprint(payload: dict[str, Any]) -> str:
    tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
    issues = payload.get("issues") if isinstance(payload.get("issues"), list) else []
    parity = payload.get("parity") if isinstance(payload.get("parity"), dict) else {}
    latest_closeout = parity.get("latest_closeout") if isinstance(parity.get("latest_closeout"), dict) else {}
    return _stable_hash(
        {
            "tool_ids": [tool.get("id") for tool in tools if isinstance(tool, dict)],
            "tool_source_fingerprints": [tool.get("source_fingerprint") for tool in tools if isinstance(tool, dict)],
            "tool_count": payload.get("tool_count"),
            "projection_counts": payload.get("projection_counts"),
            "issue_count": payload.get("issue_count"),
            "issue_fingerprints": [
                issue.get("parity_fingerprint") or _stable_hash(issue) for issue in issues if isinstance(issue, dict)
            ],
            "call_queue_counts": (payload.get("call_queue") or {}).get("counts")
            if isinstance(payload.get("call_queue"), dict)
            else {},
            "run_history_counts": (payload.get("run_history") or {}).get("counts")
            if isinstance(payload.get("run_history"), dict)
            else {},
            "checkpoint_counts": (payload.get("checkpoints") or {}).get("counts")
            if isinstance(payload.get("checkpoints"), dict)
            else {},
            "parity_closeout_id": latest_closeout.get("closeout_id"),
            "parity_closeout_status": latest_closeout.get("status"),
            "parity_quieted_count": parity.get("quieted_issue_count"),
            "parity_changed_count": parity.get("changed_issue_count"),
            "policy_issue_count": (payload.get("policy") or {}).get("issue_count")
            if isinstance(payload.get("policy"), dict)
            else None,
            "runtime_issue_count": (payload.get("runtimes") or {}).get("issue_count")
            if isinstance(payload.get("runtimes"), dict)
            else None,
        }
    )


def pack_build(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    pack_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-tool-pack"
    payload = _tool_pack_payload(target)
    payload.update({"pack_id": pack_id, "status": "built"})
    pack_dir = _packs_root(target) / pack_id
    entries, entry_errors = _read_tool_entries(config_path(target))
    source_files: list[dict[str, Any]] = []
    source_root = pack_dir / "source-files"
    for entry in entries:
        source_path = entry.get("source_path")
        if not isinstance(source_path, str) or not source_path.strip():
            continue
        source = _as_path(target, source_path)
        if source is None or not source.is_file():
            source_files.append(
                {"tool_id": entry.get("id"), "source_path": source_path, "packed": False, "reason": "source missing"}
            )
            continue
        rel = Path(source_path)
        if rel.is_absolute() or ".." in rel.parts:
            source_files.append(
                {
                    "tool_id": entry.get("id"),
                    "source_path": source_path,
                    "packed": False,
                    "reason": "source path is not repo-relative",
                }
            )
            continue
        dest = source_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        source_files.append(
            {
                "tool_id": entry.get("id"),
                "source_path": source_path,
                "packed": True,
                "pack_path": str(dest.relative_to(pack_dir)),
            }
        )
    portable_catalog = {"entries": entries, "entry_errors": entry_errors, "source_files": source_files}
    payload["portable_catalog"] = portable_catalog
    if entries:
        (pack_dir / "portable-tools.toml").write_text(_format_tool_entries(entries))
    _write_json(pack_dir / "tool-pack.json", payload)
    (pack_dir / "TOOL_PACK.md").write_text(
        f"# Tool Pack {pack_id}\n\n"
        f"- tools: {payload['tool_count']}\n"
        f"- issues: {payload['issue_count']}\n"
        f"- projections: {len(payload['projections'])}\n"
        f"- import: brigade tools pack import {pack_dir}\n"
    )
    payload["path"] = str(pack_dir)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"tool_pack: {pack_id}")
    print(f"path: {pack_dir}")
    print(f"tools: {payload['tool_count']}")
    print(f"issues: {payload['issue_count']}")
    return 0


def _tool_packs(target: Path) -> list[dict[str, Any]]:
    root = _packs_root(target)
    packs: list[dict[str, Any]] = []
    if root.is_dir():
        for path in root.iterdir():
            payload, error = _read_json(path / "tool-pack.json")
            if error is None and isinstance(payload, dict):
                payload.setdefault("path", str(path))
                packs.append(payload)
    packs.sort(key=lambda item: str(item.get("created_at") or item.get("pack_id") or ""), reverse=True)
    return packs


def _sync_plan_summary(target: Path) -> dict[str, Any]:
    plan = _projection_plan_payload(target)
    blockers = [
        item
        for item in plan.get("projections", [])
        if isinstance(item, dict) and item.get("status") in {"conflicted", "unmanaged", "missing_source"}
    ]
    actions = [
        item
        for item in plan.get("projections", [])
        if isinstance(item, dict) and item.get("action") in {"create", "update", "conflict"}
    ]
    return {
        "valid": plan.get("valid"),
        "counts": plan.get("counts", {}),
        "projection_count": len(plan.get("projections", [])),
        "action_count": len(actions),
        "blocker_count": len(blockers),
        "blockers": blockers,
        "dry_run_default": True,
        "add_only": True,
        "delete_supported": False,
    }


def _tool_pack_health(target: Path) -> dict[str, Any]:
    packs = _tool_packs(target)
    latest = packs[0] if packs else None
    current = _tool_pack_payload(target)
    checks: list[dict[str, Any]] = []
    if latest is None:
        checks.append(
            {
                "status": WARN,
                "name": "tool_pack_missing",
                "issue_type": "pack_missing",
                "detail": "no tool pack has been built",
            }
        )
    else:
        if latest.get("evidence_fingerprint") != current.get("evidence_fingerprint"):
            checks.append(
                {
                    "status": WARN,
                    "name": "tool_pack_stale",
                    "issue_type": "pack_stale",
                    "detail": f"{latest.get('pack_id')} no longer matches current catalog evidence",
                }
            )
        path = latest.get("path")
        if path and not Path(str(path)).exists():
            checks.append(
                {
                    "status": WARN,
                    "name": "tool_pack_missing_path",
                    "issue_type": "pack_missing_path",
                    "detail": str(path),
                }
            )
    return {
        "packs_path": str(_packs_root(target)),
        "pack_count": len(packs),
        "latest": latest,
        "current_fingerprint": current.get("evidence_fingerprint"),
        "issue_count": len(checks),
        "issues": checks,
        "top_issue": checks[0] if checks else None,
    }


def pack_list(*, target: Path, json_output: bool = False, limit: int = 20) -> int:
    target = target.expanduser().resolve()
    packs = _tool_packs(target)[:limit]
    payload = {"target": str(target), "packs": packs, "pack_count": len(packs)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"tool packs: {target}")
    for pack in packs:
        print(f"- {pack.get('pack_id')} tools={pack.get('tool_count')} issues={pack.get('issue_count')}")
    return 0


def _find_tool_pack(target: Path, pack_id: str) -> tuple[dict[str, Any] | None, str | None]:
    packs = _tool_packs(target)
    if pack_id == "latest":
        return (packs[0], None) if packs else (None, "tool pack not found: latest")
    matches = [pack for pack in packs if str(pack.get("pack_id") or "").startswith(pack_id)]
    if not matches:
        return None, f"tool pack not found: {pack_id}"
    if len(matches) > 1:
        return None, f"tool pack id is ambiguous: {pack_id}"
    return matches[0], None


def pack_show(*, target: Path, pack_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    pack, error = _find_tool_pack(target, pack_id)
    if pack is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if json_output:
        print(json.dumps({"target": str(target), "pack": pack}, indent=2, sort_keys=True))
        return 0
    print(f"tool_pack: {pack.get('pack_id')}")
    print(f"tools: {pack.get('tool_count')}")
    print(f"issues: {pack.get('issue_count')}")
    return 0


def pack_archive(*, target: Path, pack_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    pack, error = _find_tool_pack(target, pack_id)
    if pack is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    source = Path(str(pack.get("path") or _packs_root(target) / str(pack.get("pack_id"))))
    destination = _packs_archive_root(target) / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        print(f"error: archived tool pack already exists: {destination}", file=sys.stderr)
        return 2
    source.rename(destination)
    payload = {
        "target": str(target),
        "pack_id": pack.get("pack_id"),
        "status": "archived",
        "archive_path": str(destination),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"archived: {pack.get('pack_id')}")
    print(f"path: {destination}")
    return 0


def pack_import(*, target: Path, pack: Path, force: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    pack = pack.expanduser().resolve()
    manifest, manifest_error = _read_json(pack / "tool-pack.json")
    if manifest_error is not None or not isinstance(manifest, dict):
        print(f"error: not a tool pack: {pack}", file=sys.stderr)
        return 2
    portable_catalog = manifest.get("portable_catalog") if isinstance(manifest.get("portable_catalog"), dict) else {}
    entries = portable_catalog.get("entries") if isinstance(portable_catalog.get("entries"), list) else []
    entries = [dict(entry) for entry in entries if isinstance(entry, dict)]
    if not entries and (pack / "portable-tools.toml").is_file():
        entries, _ = _read_tool_entries(pack / "portable-tools.toml")
    current_entries, errors = _read_tool_entries(config_path(target))
    conflicts: list[dict[str, Any]] = []
    copied_sources: list[dict[str, Any]] = []
    skipped_existing: list[dict[str, Any]] = []
    existing_by_id = {str(entry.get("id")): entry for entry in current_entries if entry.get("id")}
    for entry in entries:
        tool_id = str(entry.get("id") or "")
        if not tool_id:
            conflicts.append({"tool_id": "", "reason": "packed entry missing id"})
            continue
        existing_entry = existing_by_id.get(tool_id)
        if existing_entry is not None and not force and _stable_hash(existing_entry) != _stable_hash(entry):
            conflicts.append(
                {
                    "tool_id": tool_id,
                    "reason": "tool id already exists with different definition",
                    "existing_source_path": existing_entry.get("source_path"),
                }
            )
        source_path = entry.get("source_path")
        if isinstance(source_path, str) and source_path.strip():
            rel = Path(source_path)
            if rel.is_absolute() or ".." in rel.parts:
                conflicts.append(
                    {"tool_id": tool_id, "reason": "source path is not repo-relative", "source_path": source_path}
                )
                continue
            packed_source = pack / "source-files" / rel
            target_source = target / rel
            if (
                packed_source.is_file()
                and target_source.is_file()
                and packed_source.read_bytes() != target_source.read_bytes()
                and not force
            ):
                conflicts.append(
                    {
                        "tool_id": tool_id,
                        "reason": "source file already exists with different content",
                        "source_path": source_path,
                    }
                )
    if errors:
        conflicts.append({"tool_id": None, "reason": "existing catalog errors", "errors": errors})
    if conflicts:
        payload = {
            "target": str(target),
            "pack": str(pack),
            "valid": False,
            "imported": [],
            "copied_sources": copied_sources,
            "skipped_existing": skipped_existing,
            "conflicts": conflicts,
        }
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 1
        for conflict in conflicts:
            print(f"conflict: {conflict.get('tool_id')} {conflict.get('reason')}")
        return 1
    merged_by_id = {str(entry.get("id")): dict(entry) for entry in current_entries if entry.get("id")}
    imported: list[dict[str, Any]] = []
    for entry in entries:
        tool_id = str(entry.get("id") or "")
        if not tool_id:
            continue
        source_path = entry.get("source_path")
        if isinstance(source_path, str) and source_path.strip():
            rel = Path(source_path)
            packed_source = pack / "source-files" / rel
            target_source = target / rel
            if packed_source.is_file():
                target_source.parent.mkdir(parents=True, exist_ok=True)
                if force or not target_source.exists() or packed_source.read_bytes() == target_source.read_bytes():
                    shutil.copy2(packed_source, target_source)
                    copied_sources.append(
                        {"tool_id": tool_id, "source_path": source_path, "target_path": str(target_source)}
                    )
        if tool_id in existing_by_id and not force:
            skipped_existing.append(
                {
                    "tool_id": tool_id,
                    "reason": "identical entry already exists",
                    "source_path": entry.get("source_path"),
                }
            )
            continue
        merged_by_id[tool_id] = dict(entry)
        imported.append({"tool_id": tool_id, "source_path": entry.get("source_path")})
    config_path(target).parent.mkdir(parents=True, exist_ok=True)
    config_path(target).write_text(_format_tool_entries(list(merged_by_id.values())))
    result = apply_gitignore(target, _gitignore_selection(target))
    payload = {
        "target": str(target),
        "pack": str(pack),
        "pack_id": manifest.get("pack_id"),
        "valid": True,
        "imported_count": len(imported),
        "imported": imported,
        "copied_sources": copied_sources,
        "skipped_existing": skipped_existing,
        "skipped_existing_count": len(skipped_existing),
        "gitignore": result,
        "next_command": "brigade tools plan --target .",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"tool_pack_import: {manifest.get('pack_id') or pack.name}")
    print(f"imported: {len(imported)}")
    print("next_command: brigade tools plan --target .")
    return 0


def sync_plan(*, target: Path, tool_id: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload = _projection_plan_payload(target, tool_id=tool_id)
    payload.update({"mode": "sync-plan", "dry_run_default": True, "delete_supported": False, "add_only": True})
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"tools sync plan: {target}")
    print(f"projections: {len(payload['projections'])}")
    print("dry_run_default: true")
    print("delete_supported: false")
    return 0 if payload["valid"] else 1


def sync_apply(
    *,
    target: Path,
    tool_id: str | None = None,
    all_tools: bool = False,
    dry_run: bool = True,
    force: bool = False,
    json_output: bool = False,
) -> int:
    if tool_id is None and not all_tools:
        if not dry_run and force:
            print("error: pass <tool-id> or --all for write sync apply", file=sys.stderr)
            return 2
        all_tools = True
    if not dry_run and not force:
        # Sync apply is intentionally conservative: explicit non-dry-run writes require --force.
        dry_run = True
    return apply(
        target=target,
        tool_id=tool_id,
        all_tools=all_tools,
        dry_run=dry_run,
        force=force,
        json_output=json_output,
    )


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


def parity_status(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _catalog_payload(target)
    parity = payload.get("parity") if isinstance(payload.get("parity"), dict) else {}
    projection_issues = [issue for issue in payload["issues"] if _is_parity_issue(issue)]
    response = {
        "target": str(target),
        "closeouts_path": str(parity_closeouts_path(target)),
        "latest_closeout": parity.get("latest_closeout"),
        "projection_issue_count": len(projection_issues),
        "quieted_issue_count": parity.get("quieted_issue_count", 0),
        "changed_issue_count": parity.get("changed_issue_count", 0),
        "issues": projection_issues,
        "quieted_issues": parity.get("quieted_issues", []),
        "changed_issues": parity.get("changed_issues", []),
    }
    if json_output:
        print(json.dumps(response, indent=2, sort_keys=True))
        return 0
    print(f"tools parity: {target}")
    print(f"projection_issues: {response['projection_issue_count']}")
    print(f"quieted_issues: {response['quieted_issue_count']}")
    print(f"changed_issues: {response['changed_issue_count']}")
    latest = response.get("latest_closeout")
    if isinstance(latest, dict):
        print(f"latest_closeout: {latest.get('closeout_id')} [{latest.get('status')}]")
    else:
        print("latest_closeout: none")
    return 0


def parity_closeout(*, target: Path, reason: str | None = None, defer: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _catalog_payload(target)
    source_issues = [
        issue for issue in payload.get("raw_issues", []) if isinstance(issue, dict) and _is_parity_issue(issue)
    ]
    fingerprints = [_parity_issue_fingerprint(issue) for issue in source_issues]
    closeout_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-tool-parity-closeout"
    closeout = {
        "closeout_id": closeout_id,
        "created_at": _now().isoformat(),
        "status": "deferred" if defer else "reviewed",
        "reason": reason or "",
        "issue_count": len(source_issues),
        "source_fingerprints": fingerprints,
        "safe_summary": f"{len(source_issues)} tool projection parity issue(s) {'deferred' if defer else 'reviewed'}",
    }
    _write_json(parity_closeouts_path(target) / closeout_id / "closeout.json", closeout)
    if json_output:
        print(json.dumps(closeout, indent=2, sort_keys=True))
        return 0
    print(f"tool_parity_closeout: {closeout_id}")
    print(f"status: {closeout['status']}")
    print(f"issues: {closeout['issue_count']}")
    return 0
