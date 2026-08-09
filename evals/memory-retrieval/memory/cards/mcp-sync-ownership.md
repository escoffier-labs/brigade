---
title: MCP Sync Ownership
tags: [mcp, sync, harness]
description: Brigade owns the merged MCP config; harness-local edits are overwritten on sync.
---

# MCP Sync Ownership

`brigade mcp sync` is the sole writer of the managed MCP block. Hand-editing a harness MCP file inside the managed section is discarded on the next sync. Put local-only servers outside the managed markers or register them through Brigade.
