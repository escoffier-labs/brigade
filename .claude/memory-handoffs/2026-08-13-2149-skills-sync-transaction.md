# Memory Handoff

## Type

decision

## Title

Skills sync uses the shared projection transaction kernel

## Summary

Multi-target `brigade skills sync --write` now plans rendered bundles, receipts, rollback files, and install history as one shared projection-kernel operation. The kernel owns its redacted journal and before-images, so a write either commits every file or restores the planned set.

## Durable facts

- A write failure restores bundles, receipts, rollback files, and history together, then reports the projection terminal state.
- Sync filters `projection.unfinished_operations()` to `skills-` operation IDs. A pending MCP or other projector operation does not block skills sync; a pending skills operation reports `brigade projection recover <id>`.
- A foreign unmanaged bundle remains blocked and does not create a receipt or append install history.

## Evidence

- files changed: `src/brigade/skills_cmd.py`, `tests/test_skills_cmd.py`
- commands run: `brigade work verify run --target . --argv-json '["pytest", "-q", "tests/test_skills_cmd.py"]' --capture brigade-work`
- error strings: `destination drift between plan and prepare`, `recovery required`

## Recommended memory action

no-card

## Target document

.learnings/LEARNINGS.md

## Suggested document content

### Skills sync projection transactions

`skills sync --write` builds one `skills-` projection operation for mutable bundle files, receipts, rollback files, and history. Use the shared `brigade projection recover <id>` path for an unfinished skills operation. Do not let other projector operations block skills sync, and do not create provenance records for foreign unmanaged bundles.
