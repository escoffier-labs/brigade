# Memory Handoff

## Type

bugfix

## Title

Fleet paging migrations must retain current-version schema repair

## Summary

Concurrent Fleet Hub migrations took schema versions 38 through 40 before paging landed. Paging therefore needs version 41, plus additive repair for databases already stamped at the current version without the snapshot table or expiry index. Merging the paging branch must retain the Events repair order and grant filtering before session limits.

## Durable facts

- `fleet_hub._missing_additive_columns` creates missing Events tables before column alterations and indexes, then appends paging table/index DDL.
- Events and campaign tests assert the exact schema version. Keep those expectations aligned when reserving a new version.
- `_init_schema` has a read-only fast path when the current-version schema is complete. Missing additive objects trigger repair in a write transaction.
- Snapshot continuation filters against current grants while retaining original row offsets. Inserts, renewals, release, and reacquisition must not reorder that snapshot.
- Node claim pages include display metadata and active Grok Bot lease rows. Agent pages use ordinary claims restricted by owner grants and omit `job`, `session`, and `owner_conductor`.

## Evidence

- files changed: `src/brigade/fleet_hub.py`, `src/brigade/fleet_client_message_credentials.py`, `tests/test_fleet_who_paging.py`
- commands run: `brigade code impact _missing_additive_columns`, `brigade code impact _station_read`, `brigade code affected src/brigade/fleet_hub.py src/brigade/fleet_hub_messages.py src/brigade/fleet_client_message_credentials.py src/brigade/fleet_hub_paging.py`
- focused baseline receipt: `20261022-215001-work-verify-d9e1e2`, 178 passed. Paging receipt: `20261022-215149-work-verify-3ed2be`, 41 passed.
- version regression control: `20261022-215259-work-verify-7702c7` failed with `has schema version 40; this brigade supports 38` after temporarily restoring the stale branch version. The working version was restored.

## Recommended memory action

create-card

## Target card

brigade-fleet-paging-migrations.md

## Suggested card content

```markdown
---
topic: Fleet Hub paging migrations
category: development
tags: [brigade, fleet, sqlite, paging]
---

# Fleet Hub paging migrations

Before pushing a hub schema change, read the schema version on current main and reserve the next version. The migration and current-version repair paths both need idempotent table and index creation. Preserve table-before-column-before-index ordering for Events repairs.

Roster and claims cursors traverse stored snapshots. Recheck grants on every page without changing snapshot offsets, and filter permitted sessions before their SQL row limit. Agent claim pages omit job, session, and owner_conductor. Node pages retain the display projection, including active Grok Bot leases.
```
