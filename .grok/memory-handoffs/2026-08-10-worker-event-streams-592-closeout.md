# Memory Handoff

## Type

project-context

## Title

Worker event streams #592 first-slice closeout

## Summary

Close out #592 by documenting the full Codex app-server item-type classification matrix and adding adversarial golden fixtures for every matrix item type on top of the existing fail-closed scrubber.

## Durable facts

- Module brigade.worker_events already enforced fail-closed scrubbing for turn/started|completed and item/started|completed plus requestApproval#auto-declined
- docs/worker-event-streams.md now tables every matrix item type and maps #592 acceptance criteria to enforcement points
- Golden fixture worker-events-appserver.v1.golden.json includes item_completed_<type>_adversarial cases for all 14 item types
- First slice still excludes legacy backfill and export/archive scrubbed-sidecar wiring

## Evidence

- files changed: docs/worker-event-streams.md, src/brigade/fixtures/worker-events-appserver.v1.golden.json, tests/test_worker_events.py, CHANGELOG.md
- commands run: brigade code impact scrub_event; brigade work verify run (ruff+pytest worker_events); ./scripts/verify

## Recommended memory action

no-card

## Target document

.learnings/FEATURE_REQUESTS.md

## Suggested document content

### Closeout

Issue #592 first slice was already library-complete on main via #823; this
closeout expands the documented item-type matrix, adds adversarial golden
cases for every matrix item type, and opens a PR that closes the still-open
issue. Legacy backfill and export/archive sidecar wiring remain out of scope.
