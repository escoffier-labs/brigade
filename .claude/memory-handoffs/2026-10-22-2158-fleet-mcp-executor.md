# Memory Handoff

## Type

project-context

## Title

Fleet MCP replay and Executor approval boundaries

## Summary

Issue #1887 slice 7 can expose the message client's existing replay key without
a hub schema migration. Executor approval requires an explicit app policy and a
browser-bound connection, and station cancellation still has a bounded cleanup
delay.

## Durable facts

- Hub messages deduplicate by authenticated sender and `client_id`, check a payload digest and return the original ID. Send and reply share this namespace, bounded by message retention.
- The message client returns an existing queued key before attempting to drain the spool. Recovery must flush that credential's spool before expecting a delivered ID.
- The shared stateless MCP station ignores cancellation notifications. Disconnecting does not immediately cancel the hub wait. The Executor recipe uses short waits and records immediate capacity release as incomplete.
- Executor's generated MCP approval rule only gates destructive hints. Send and reply are additive, so the app explicitly gates all writes. Default model-mode approvals are agent-answerable through `resume`.

## Evidence

- Files: `src/brigade/fleet_mcp.py`, `tests/test_fleet_mcp_executor.py`, `docs/fleet-mcp-executor.md`.
- Commands: `brigade code sync --target .`, `brigade code impact src/brigade/fleet_mcp.py --target .`, `brigade code affected src/brigade/fleet_mcp.py --json`.
- Impact: fleet CLI registration, skills dispatch and inbox delivery framing. The graph also attributed `tests/test_dot_fleet_tools.py`, `tests/test_evidence_ack_protocol.py` and `tests/test_grokbot_obsidian_adapters.py` through broad call-name edges. Dynamic MCP fixture coverage required explicit fleet and HTTP test selection.
- Focused command: `brigade work verify run --target . --command "./scripts/verify-focused tests/test_fleet_mcp.py tests/test_fleet_mcp_executor.py tests/test_mcp_http.py tests/test_fleet_inbox_delivery.py" --capture brigade-work`.
- Receipt `20261022-215345-work-verify-417abc`: 178 passed, one existing Windows-only skip. Ad hoc receipts are audit-only.
- Receipt `20261022-215755-work-verify-4e768b`: the documented app source passed strict TypeScript checking with `apps@0.0.1-beta.90` and `@modelcontextprotocol/sdk@1.32.1`. No Executor deployment was attempted.

## Recommended memory action

create-card

## Target card

fleet-mcp-executor-boundaries.md

## Suggested card content

```markdown
---
topic: Fleet MCP replay and Executor compatibility
category: project-context
tags: [brigade, fleet, mcp, executor]
---

# Fleet MCP Executor boundaries

Fleet send and reply accept optional sender-scoped `client_id` replay keys.
Already queued retries return `spooled` until the credential's flush workflow
drains them. Executor needs an explicit approval policy for send, reply and ack,
plus a browser-bound OAuth grant. Station annotations never enforce approval or
replace hub grants. See `docs/fleet-mcp-executor.md` for manual checks and the
known cancellation propagation limit.
```
