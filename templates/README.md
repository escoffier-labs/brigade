# Brigade Templates

This folder is the public index for Brigade's starter templates.

The canonical installable files live under [`src/brigade/templates`](../src/brigade/templates/) because the Python package loads them from there. Keep edits in that package directory so `brigade init` and release builds install the same files people see in the repo.

## Fresh Workspace Templates

These are the starter files for someone building an agent workspace from scratch:

- [`AGENTS.md`](../src/brigade/templates/workspace/AGENTS.md)
- [`CLAUDE.md`](../src/brigade/templates/workspace/CLAUDE.md)
- [`MEMORY.md`](../src/brigade/templates/workspace/MEMORY.md)
- [`TOOLS.md`](../src/brigade/templates/workspace/TOOLS.md)
- [`USER.md`](../src/brigade/templates/workspace/USER.md)
- [`SOUL.md`](../src/brigade/templates/workspace/SOUL.md)
- [`SAFETY_RULES.md`](../src/brigade/templates/workspace/SAFETY_RULES.md)
- [`INSTALL_FOR_AGENTS.md`](../src/brigade/templates/workspace/INSTALL_FOR_AGENTS.md)
- [`IDENTITY.md`](../src/brigade/templates/workspace/IDENTITY.md)
- [`HEARTBEAT.md`](../src/brigade/templates/workspace/HEARTBEAT.md)

Run:

```bash
brigade init --depth workspace --harnesses codex
```

## Memory Cards

Starter memory cards live in [`src/brigade/templates/memory/cards`](../src/brigade/templates/memory/cards/). They are public-safe examples and operating patterns, not this repo's live memory.

## Harness Adapters

- [Claude handoff template](../src/brigade/templates/claude/memory-handoffs/TEMPLATE.md)
- [Codex handoff template](../src/brigade/templates/codex/memory-handoffs/TEMPLATE.md)
- [OpenClaw fragments](../src/brigade/templates/openclaw/)
- [Hermes fragments](../src/brigade/templates/hermes/)
- [Generic harness contract](../src/brigade/templates/generic/memory-contract.md)

## Repo Baseline

Repo installs use:

- [repo depth manifest](../src/brigade/templates/depth/repo.json)
- [workspace depth manifest](../src/brigade/templates/depth/workspace.json)
- [pre-push hook](../src/brigade/templates/hooks/pre-push)
- [content policies](../src/brigade/templates/policies/)

## Boundary

Root workspace files such as `/AGENTS.md`, `/MEMORY.md`, `/TOOLS.md`, `/memory/cards/`, `.brigade/`, and `.claude/` are local dogfood state and intentionally ignored. Template source belongs under `src/brigade/templates/`.
