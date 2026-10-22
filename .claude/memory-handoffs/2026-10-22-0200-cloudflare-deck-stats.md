# Memory Handoff

## Type
project-context

## Title
Cloudflare status fields in Deck statistics

## Summary
Issue #1877 adds a read-only Cloudflare panel to `/deck/stats` using a closed v2 statistics snapshot. The collector keeps fixed command vectors and normalized results; the rendering layer treats stale or unavailable observations as unknown. Wrangler’s JSON shapes differ by command: deployment status returns one deployment object, deployments list and versions list return arrays, and version creation time is under `metadata.created_on`. Each read must fail independently so a D1 error does not discard deployment values. Worker reads require only a configured Worker, and D1 reads require only a configured database. Daily compaction preserves both the best regular statistics snapshot and the newest Cloudflare observation; latest snapshot selection prefers the newest UTC day before coverage, and the panel follows the newest snapshot’s opt-in state. Retained v1 snapshots remain valid and have no `cloudflare` field. Wrangler does not provide a D1 purge event timestamp, so `last_purge` must remain unknown until an event source is added. A database-only status read that succeeds through Wrangler OAuth is authenticated even without an Analytics token; the GraphQL viewer check must include the configured account, and gradual deployments must not be reduced to one arbitrary version.

## Durable facts
- The stats Hub accepts retained `brigade.admin-stats.v1` snapshots and the new `brigade.admin-stats.v2` shape.
- Cloudflare collection is opt-in with `fleet.stats.status_panel = true`; credentials and raw API or CLI payloads are not stored in the snapshot.
- Wrangler `deployments status --json` returns the deployment object directly; `versions list --json` returns an array with `metadata.created_on`.
- Keep deployment, version, D1, and GraphQL reads independent. A failed D1 read leaves its cell unknown while successful Worker status values remain available.
- Daily snapshot compaction must not evict a fresh Cloudflare observation for a snapshot with broader regular source coverage. Retain the best regular snapshot and latest Cloudflare snapshot separately, and let the page hide the Cloudflare section when the newest retained snapshot has no Cloudflare observation.
- The v1 stats schema has no `cloudflare` key. Read it with a schema-safe lookup, and order retained daily rows by UTC date before source coverage so poor coverage today does not expose yesterday's snapshot.
- Build status Wrangler commands per resource: deployment and version reads need the Worker name, while D1 info and the fixed count query need only the database name.
- A successful database-only Wrangler read can establish credential availability without an Analytics bearer. For Worker Analytics, the fixed viewer check must match the configured `accountTag`; seeing some other account is not sufficient.
- Wrangler deployment status may report multiple versions during gradual rollout. The panel’s singular version and publish-time fields should remain unknown for that shape.
- The full verification gate can fail on unrelated test shards even after the focused stats gate passes; inspect its shard failure list before attributing failures to a change.

## Evidence
- files changed: `src/brigade/fleet_admin_stats_collector.py`, `src/brigade/fleet_admin_stats.py`, `src/brigade/fleet_admin_stats_page.py`, `docs/fleet-sync.md`, `tests/test_fleet_admin_stats_collector.py`
- commands run: `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_fleet_admin_stats_collector.py","tests/test_fleet_admin_stats.py"]' --capture brigade-work`; `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_fleet_admin_stats.py"]' --capture brigade-work`; `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_fleet_admin_stats.py","tests/test_fleet_admin_stats_collector.py"]' --capture brigade-work` (prior result: 122 passed); `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_fleet_admin_stats_collector.py"]' --capture brigade-work` (latest result: 47 passed); `brigade work verify run --target . --argv-json '["./scripts/verify"]' --timeout 3600 --capture brigade-work`
- full gate result: 66 failed, 22715 passed, 240 skipped; focused gate completed successfully.

## Recommended memory action
no-card

## Target document
.learnings/LEARNINGS.md

## Suggested document content
### Cloudflare stats source limits
Wrangler does not expose a D1 purge event timestamp. Do not substitute the latest observed row date for a purge time; render `last_purge` as unknown until the Worker or another trusted source records an actual purge event.
