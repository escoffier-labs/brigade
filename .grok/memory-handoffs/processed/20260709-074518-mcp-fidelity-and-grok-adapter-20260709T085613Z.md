# Memory Handoff

## Type

bugfix

## Title

MCP empty-args + url-only import fixed; Grok adapter

## Summary

Fixed Codex fingerprint conflicts for empty args (#181), url-only import as stdio (#182), and added grok/grok-user MCP adapters (#183). All four user harnesses plan as 24 skip.

## Durable facts

- to_provider omits empty args lists so TOML write/read fingerprints match.
- url-only and URL-as-command entries coerce to http/sse on import.
- grok project adapter: .grok/config.toml; grok-user: ~/.grok/config.toml with --user-scope.

## Evidence

- issues #181 #182 #183; verify run 20260709-074211-work-verify-842fda
- files: src/brigade/mcp_adapters.py, tests/test_mcp_adapters.py, docs/mcp-sync.md

## Recommended memory action

create-card

## Target card

mcp-fidelity-and-grok-adapter.md

## Suggested card content

---
topic: mcp-fidelity-and-grok-adapter
category: workflow
tags: [brigade, mcp, codex, openclaw, grok]
---

# MCP fidelity fixes and Grok adapter

### Bugs fixed

- #181: omit empty `args` in `to_provider` so Codex/Grok TOML round-trips stay fingerprint-stable
- #182: url-only import coerces to http/sse (never stdio+url); handles bogus transport and URL-as-command

### Grok adapter (#183)

- `grok` → `.grok/config.toml` (project)
- `grok-user` → `~/.grok/config.toml` (user-scope, `--user-scope`)
- Reuses Codex-like `[mcp_servers.<name>]` TOML read/write

### Operator path

```bash
brigade mcp import claude-user --merge --user-scope
brigade mcp sync --harness grok-user --user-scope --write --adopt --force
brigade mcp plan --harness codex-user --user-scope   # all skip after fix
```
