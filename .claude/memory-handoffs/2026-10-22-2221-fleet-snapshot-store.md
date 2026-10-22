# Memory Handoff

## Type

bugfix

## Title

Snapshot continuation cost and authorization maintenance

## Summary

Parsing a serialized snapshot for each page made limit-one walks quadratic.
An ordinal row store also needs current-grant visibility and totals maintained
alongside the snapshot data.

## Durable facts

- `fleet_hub_paging` stores collection counts and individual rows. Authorization
  triggers update visibility and count deltas during grant or revocation writes.
  Ordinary continuations seek the visibility index without scanning the roster.
- Final-page deletion and expired-snapshot cleanup remove rows and counts in the
  same transaction. Terminal cleanup costs one deletion of the stored rows.
- Snapshot creation requires explicit `limit` or `cursor`. MCP supplies a default
  limit. CLI claims already uses the unpaged `GET /claims` endpoint.
- Paging uses schema 44. Main uses 41/42 for campaign indexes. Version 43 is reserved
  for questions. Upgrade tests cover 42 and 43 to 44, and legacy schema-41 cursors.
- Caller count and byte budgets evict only that caller's oldest walk. Global
  budgets still refuse. Old-format cursors fail with a restart instruction.
- Output-only grant tests missed a removed prefilter. Checking stored rows and
  cursor offsets caught it: offset four exposed three forbidden preceding rows.

## Evidence

- files changed: `src/brigade/fleet_hub_paging.py`,
  `src/brigade/fleet_hub_messages.py`, `tests/test_fleet_snapshot_store.py`
- command: `brigade work verify run --target . --command "./scripts/verify-focused scripts/benchmark_fleet_paging.py -s" --capture brigade-work`
- Baseline receipt: `20261022-220926-work-verify-1993e5`. Five walks of 2,000
  rows, 8,304,012 payload bytes, had medians 25.566885 seconds at limit one and
  0.106360 seconds at limit 200. Both decoded 8,304,068 bytes per continuation.
- After receipt: `20261022-224828-work-verify-ccc3d2`. Medians were 0.044671 and
  0.015540 seconds, with 4,207 and 830,256 decoded bytes. Measurements exclude
  initial snapshot creation and transport. Fixtures reset between completed walks.
- Prefilter mutation receipt: `20261022-221313-work-verify-46e77b`.
- Merged main: `e9d1cd5d8`, preserving campaign migrations and behavior.
- Focused gate receipt: `20261022-224457-work-verify-548631`, 1,040 passed.

## Recommended memory action

no-card

## Target document

.learnings/LEARNINGS.md

## Suggested document content

### Snapshot authorization and complexity checks

Bounded paging needs an indexed seek and cached counts, plus an explicit policy
for changing grants. Maintain visibility during authorization writes so read
cost stays independent of snapshot length. Test stored rows and cursor offsets
alongside output filtering, and count decoder bytes and SQLite steps on replay.
