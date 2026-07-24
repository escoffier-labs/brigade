# Case 1: first consolidation scope

## Decision question

Should Agent Pantry and agent-notify be inside the first Brigade
consolidation?

## Evidence context

- [Issue #366](https://github.com/escoffier-labs/brigade/issues/366) separates
  Agent Pantry's browser-session, secret-handling, and security lifecycle from
  Brigade's first source consolidation.
- agent-notify is a small opt-in Go delivery adapter. Agent Pantry may call it
  for expiry alerts, but that integration does not require shared source
  ownership.
- Manifest-managed installation does not require either component's source to
  move in the first consolidation.

## Known-good answer

No. Keep both source and release ownership separate from the first
consolidation.
