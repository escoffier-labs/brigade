# Memory Handoff

## Type

decision

## Title

Hermes MCP adapter shipped

## Summary

Added hermes user-scoped MCP adapter for ~/.hermes/config.yaml mcp_servers; merged to main after stations PR.

## Durable facts

- brigade mcp sync --harness hermes --user-scope --write --adopt --force writes the union catalog into Hermes.
- Zero-dep YAML subset; preserves non-mcp_servers keys in config.yaml.

## Evidence

- PR #190 merged; cards promoted for station/mcp work earlier

## Recommended memory action

create-card

## Target card

hermes-mcp-adapter.md

## Suggested card content

---
topic: hermes-mcp-adapter
category: workflow
tags: [brigade, mcp, hermes]
---

# Hermes MCP adapter

User-scoped adapter for `~/.hermes/config.yaml` under `mcp_servers`.
Zero-dep YAML subset surgical merge. Use `--user-scope` for import/sync/plan.
