# Memory Handoff

## Type

bugfix

## Title

Fleet messaging startup and storage review boundaries

## Summary

PR #1705 restores hub startup through symlinked data directories and skips message spool access without a hub URL and node token. The review's claimed missing-token AttributeError was inconsistent with the settings loader, which returns empty strings. The early return still prevents unauthenticated spool mutation.

## Durable facts

- Resolve the database parent after mkdir, then validate database and sidecar files individually. Resolving the whole database path before validation would accept a final-file symlink.
- Existing shared parent modes remain unchanged. New hub directories use 0700 and database files use 0600.
- No current queue writer creates an empty-token spool. A seeded expired record exposes the old unauthenticated cleanup path without pretending that state is produced by normal enqueue operations.
- Static graph attribution is a lower bound. The affected-file query omitted the messaging and dashboard tests that directly exercise these changes, so those selectors were included explicitly.

## Evidence

- Files: `src/brigade/fleet_client_messages.py`, `src/brigade/fleet_hub.py`, `src/brigade/fleet_dashboard.py`, `tests/test_fleet_messages_startup.py`.
- Failing regression receipt: `20261015-195324-work-verify-bbb773` (7 failed, 2 passed before implementation).
- Focused verification receipt: `20261015-195413-work-verify-2197be` (393 passed).
- Command: `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_fleet_messages_startup.py","tests/test_fleet_messages.py","tests/test_fleet_dashboard.py","tests/test_fleet_client_hardening.py","tests/test_fleet_nodes.py","tests/test_fleet_hub_server.py","tests/test_module_size_ratchet.py","tests/test_worklore_http.py"]' --capture brigade-work`.
- Independent flat Opus advice: `/tmp/opus-2nd-1705-1.md`, checked against the source and regression results.

## Recommended memory action

no-card

## Target document

.learnings/LEARNINGS.md

## Suggested document content

### Messaging storage validation

When allowing relocated hub data directories, resolve the parent while preserving checks on final database and sidecar files. Verify a missing-credential review claim against the settings loader: unset Fleet credentials are empty strings, and a pre-spool guard enforces a no-access boundary rather than preventing a None conversion crash.
