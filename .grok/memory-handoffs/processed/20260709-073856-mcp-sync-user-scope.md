# Memory Handoff

## Type

workflow

## Title

MCP catalog synced to claude-user, codex-user, openclaw

## Summary

Built union catalog from Claude/Codex/OpenClaw user configs into .brigade/mcp.json and wrote it back to all three with adopt+force. Fixed x server to http transport.

## Durable facts

- brigade mcp import claude-user|codex-user|openclaw --merge --user-scope builds the catalog.
- brigade mcp sync --harness <name> --user-scope --write --adopt --force aligns each harness.
- Adapters: claude-user, codex-user, openclaw (user-scoped). No grok MCP adapter.
- x server must be transport http when only a url is present.

## Evidence

- files written: ~/.claude.json, ~/.codex/config.toml, ~/.openclaw/openclaw.json, .brigade/mcp.json
- commands: brigade mcp import ... --merge --user-scope; brigade mcp sync ... --write --adopt --force

## Recommended memory action

create-card

## Target card

mcp-sync-user-scope.md

## Suggested card content

---
topic: mcp-sync-user-scope
category: workflow
tags: [brigade, mcp, claude, codex, openclaw]
---

# MCP sync across Claude Code, Codex, OpenClaw

Canonical catalog is `.brigade/mcp.json`. Import union from user-scoped harnesses, then sync with `--user-scope --write --adopt --force`.

### Targets

- claude-user: `~/.claude.json`
- codex-user: `~/.codex/config.toml`
- openclaw: `~/.openclaw/openclaw.json`

### Notes

- No Grok MCP adapter yet (doctor notes that)
- Fix broken `x` import: url-only must be `transport: http`
- Catalog is machine-local (home paths); keep gitignored
