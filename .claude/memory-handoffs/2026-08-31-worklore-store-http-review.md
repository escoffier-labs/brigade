# Memory Handoff

## Type

bugfix

## Title

Worklore store and HTTP review behavior

## Summary

Worklore imports now identify unchanged observations without writing sync events and persist the replay counts with each idempotency key. The HTTP handlers validate supported query fields before dispatching deterministic keyset pagination and use the configured Brigade home for the feature gate.

## Durable facts

- A no-op import compares the item title and durable link projection, including stale state, but excludes sync timestamps.
- Execution-link replays return the persisted link shape, while a run linked to a different item returns `link-conflict`.
- Item detail retains its existing `events` key, while `/work/items/<id>/events` is the paginated event endpoint.

## Evidence

- files changed: `src/brigade/worklore_store.py`, `src/brigade/worklore_http.py`
- commands run: `brigade work verify run --target . --command "./scripts/verify-focused tests/test_worklore_store.py tests/test_worklore_http.py tests/test_worklore_client.py" --capture brigade-work --timeout 900`
- receipt: `20260901-020725-work-verify-733649`, `37 passed in 6.51s`

## Recommended memory action

no-card

## Target document

.learnings/LEARNINGS.md

## Suggested document content

### Worklore imports and paged HTTP views

No-op adapter observations must compare persisted fields rather than sync timestamps. Store the resulting unchanged count with the idempotency key so a replay returns the original count and creates no events. Keep OpsDeck item-detail events separate from the paginated per-item events endpoint.
