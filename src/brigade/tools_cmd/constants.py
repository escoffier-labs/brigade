"""Constants for the tools command family."""

from __future__ import annotations

import re
from typing import Any

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
DEFAULT_POLICY: dict[str, Any] = {
    "allowed_families": ["script"],
    "allowed_effects": ["local-read", "local-write"],
    "denied_effects": ["remote-mutation", "secret-read"],
    "required_approval_modes": ["on-request", "always"],
    "max_timeout": 60,
    "allowed_runtimes": ["local-helper"],
    "env_bindings": {"SAFE_ENV": "SAFE_ENV"},
}
