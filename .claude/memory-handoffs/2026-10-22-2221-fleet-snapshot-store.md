# Memory Handoff

## Type

bugfix

## Title

Snapshot continuation cost and authorization maintenance

## Summary

Parsing serialized snapshots on every page made limit-one walks quadratic.
Ordinal storage also needs current-grant visibility and totals.

## Durable facts

- `fleet_hub_paging` stores collection counts and individual rows. Authorization
  triggers update visibility and count deltas during grant or revocation writes.
  Ordinary continuations require the visibility index: stale `ANALYZE` statistics
  can otherwise select a primary-key scan after grants narrow. Read payloads
  only for selected rows.
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
- After receipt: `20261022-231733-work-verify-1edd40`. Medians were 0.054573 and
  0.022098 seconds, with 4,207 and 830,256 decoded bytes. Measurements exclude
  initial snapshot creation and transport. Fixtures reset between completed walks.
- Stale-statistics red receipt: `20261022-225305-work-verify-947dcb`, 297 versus 8,361 steps.
- Payload-read red receipt: `20261022-231207-work-verify-f0c686`, 17,217 bytes for oversized/lookahead rows.
- Prefilter mutation receipt: `20261022-221313-work-verify-46e77b`.
- Merged main: `409d7653e`, preserving campaign migrations and unread counts.
- Full focused receipt: `20261022-231336-work-verify-468bec`, 1,041 passed.
- Merged paging/visibility receipt: `20261022-233951-work-verify-798a69`, 658 passed.

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
