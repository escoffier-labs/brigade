# MEMORY.md - Master Index

## How Memory Works

- This file is an index, not the memory body.
- Store durable details in `memory/cards/*.md`, daily notes, or routed handoffs.
- Keep entries short and link to the source file.
- One memory owner is canonical: **OpenClaw**.

## Starter Cards

- [memory-architecture](memory/cards/memory-architecture.md) - how this workspace stores durable knowledge
- [handoff-flow](memory/cards/handoff-flow.md) - how Memory Handoffs flow into canonical memory
- [content-safety](memory/cards/content-safety.md) - publish guards and what they block

## Solo-mise dev pointers

- [AGENTS.md](AGENTS.md#solo-mise-repo-specific-rules) - repo-specific invariants and pre-commit checklist
- [TOOLS.md](TOOLS.md) - dev commands (pytest, content-guard, dogfood)
- [RELEASE.md](RELEASE.md) - tag + pipx-verify checklist
- [QUICKSTART.md](QUICKSTART.md) - user-facing 5-minute install path
- `.solo-mise/openclaw/` - OpenClaw fragments for manual merge

## Current Priorities

- Replace this section with short pointers only as the project develops.

## Maintenance

- Consolidate duplicate entries.
- Remove stale pointers after verifying the source is obsolete.
- Keep this file under ~200 lines so it stays in cache.
