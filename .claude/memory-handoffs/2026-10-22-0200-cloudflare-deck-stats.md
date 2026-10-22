# Memory Handoff

## Type
project-context

## Title
Cloudflare status fields in Deck statistics

## Summary
Issue #1877 adds a read-only Cloudflare panel to `/deck/stats` using a closed v2 statistics snapshot. The collector keeps fixed command vectors and normalized results; the rendering layer treats stale or unavailable observations as unknown. Wrangler does not provide a D1 purge event timestamp, so `last_purge` must remain unknown until an event source is added.

## Durable facts
- The stats Hub accepts retained `brigade.admin-stats.v1` snapshots and the new `brigade.admin-stats.v2` shape.
- Cloudflare collection is opt-in with `fleet.stats.status_panel = true`; credentials and raw API or CLI payloads are not stored in the snapshot.
- The full verification gate can fail on unrelated test shards even after the focused stats gate passes; inspect its shard failure list before attributing failures to a change.

## Evidence
- files changed: `src/brigade/fleet_admin_stats_collector.py`, `src/brigade/fleet_admin_stats.py`, `src/brigade/fleet_admin_stats_page.py`, `docs/fleet-sync.md`
- commands run: `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_fleet_admin_stats_collector.py","tests/test_fleet_admin_stats.py"]' --capture brigade-work`; `brigade work verify run --target . --argv-json '["./scripts/verify"]' --timeout 3600 --capture brigade-work`
- full gate result: 66 failed, 22715 passed, 240 skipped; focused gate completed successfully.

## Recommended memory action
no-card

## Target document
.learnings/LEARNINGS.md

## Suggested document content
### Cloudflare stats source limits
Wrangler does not expose a D1 purge event timestamp. Do not substitute the latest observed row date for a purge time; render `last_purge` as unknown until the Worker or another trusted source records an actual purge event.
