# Grok Bot Obsidian Missing-File Plan

## Problem

Obsidian Local REST API MCP reports an absent `vault_read` path as an MCP error envelope. `NativeMcpPort` rejects every error envelope before its existing exact missing-file comparison, so create-note work cannot prove that its target is absent.

## Decision

Recognize one closed missing-file envelope inside `_read_vault_file` before generic result parsing:

- the top-level object has only `isError` and `content`
- `isError` is exactly `true`
- `content` has one block
- the block has only `type` and `text`
- `type` is `text`
- `text` is within the existing MCP output byte limit
- `text` equals `File not found: <normalized requested path>`

Every other error envelope remains a `protocol_error`. `_tool_text` keeps rejecting `isError` results for all other adapter operations.

## Rejected alternatives

- Accept any error text containing `File not found`. This could bind one requested path to a different missing path or accept extra untrusted content.
- Relax `_tool_text` globally. Other tools do not use a missing-file sentinel and must keep failing closed on MCP errors.
- Treat any `isError` response as absence. Authentication, transport, plugin, and protocol failures must not authorize create behavior.

## Acceptance

- The exact live missing-file envelope returns `None` for the exact normalized requested path.
- A different path, extra top-level key, extra content block, extra block key, non-text block, other error message, non-string text, or oversized text raises `protocol_error`.
- The native call uses the normalized requested path.
- Existing success parsing, sanitization, output limits, and strict error handling stay unchanged.
