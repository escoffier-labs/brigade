# Memory Handoff

## Type

decision

## Title

Fleet message counts preserve authentication and explicit acknowledgement

## Summary

Issue #1715 adds a body-free unread count alongside the existing sender acknowledgement views and agent board counts. The task clarification preserves the distinction between authentication rejection and revocation detected inside a message operation.

## Durable facts

- Revoked node or agent tokens receive HTTP 401 at authentication. A principal revoked after authentication receives HTTP 403 with `message_sender_inactive` from the transaction guard. Keep both contracts.
- Replying does not acknowledge the original message. An unread count only decreases on explicit acknowledgement or expiry.
- The hub `count` action returns `unread_count` for the authenticated principal, with no recipient override. It uses a read transaction and excludes expired rows without pruning or selecting bodies.
- MCP adds optional `fleet_inbox.count_only`. CLI uses `fleet msg count`. Existing `sent` views already expose `acked_at` and `acked_by` only for the sender's history.

## Evidence

- Files: `src/brigade/fleet_hub_messages.py`, `src/brigade/fleet_client_messages.py`, `src/brigade/fleet_mcp.py`, `src/brigade/cli/fleet_messages.py`, `tests/test_fleet_message_visibility.py`.
- Commands: `brigade code sync`, `brigade code impact src/brigade/fleet_hub_messages.py`, `brigade code impact src/brigade/fleet_client_messages.py`, `brigade code impact src/brigade/fleet_mcp.py`, `brigade code impact src/brigade/cli/fleet_messages.py`.
- Graph impact includes the HTTP handler, CLI registration, Dot fleet tools, events and live delivery. Graph attribution reports 31 affected test files and 56 impacted files, with dynamic fixture coverage explicitly incomplete.
- `brigade work verify run --target . --command "./scripts/verify-focused tests/test_fleet_message_visibility.py" --capture brigade-work`: red receipt `20261022-215211-work-verify-b69139`, green receipt `20261022-215248-work-verify-7b6efc` (17 passed). Receipts are audit-only.

## Recommended memory action

create-card

## Target card

fleet-message-visibility.md

## Suggested card content

---
topic: Fleet message visibility and unread counts
category: project-context
tags: [brigade, fleet, messaging]
---

# Fleet message visibility

Use `fleet msg count --json` or `fleet_inbox` with `count_only: true` for body-free totals. Counts cover the credential's unacknowledged, unexpired inbox, without retention writes. Replying leaves acknowledgement explicit. Sender views expose acknowledgement time and principal for the sender's own messages. Preserve HTTP 401 for revoked credentials at authentication and HTTP 403 for revocation caught after authentication by the active-principal transaction guard.
