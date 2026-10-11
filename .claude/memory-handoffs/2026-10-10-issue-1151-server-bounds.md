# Memory Handoff

## Type

security

## Title

Fleet Hub pre-auth connection admission

## Summary

The Fleet Hub constructed a plain stdlib threaded server, so idle connections could create unlimited handlers before authentication. The connection-admission slice of issue #1151 reserves global and source capacity before thread creation and returns it after handler execution and socket cleanup.

## Durable facts

- `fleet_hub.make_server` now selects `BoundedThreadingHTTPServer`. Limits are 32 admitted handlers overall and eight per TCP peer address.
- Admission rejects overflow by closing the socket before request parsing. It never waits for capacity in the accept loop.
- Source accounting uses the transport peer, ignoring forwarded headers. Clients behind one reverse proxy share its eight-handler budget.
- The admission lock protects both budgets. Each request has a one-shot reservation, preventing double release when a startup interrupt races a completed handler. Source entries disappear when their last handler finishes.
- The connection-admission slice leaves issue #1151's field bounds, retention and request throttling for separate work.

## Evidence

- Source: `src/brigade/fleet_hub.py`, `src/brigade/fleet_hub_server.py`. Tests: `tests/test_fleet_hub_server.py`.
- Baseline receipt `20261011-023845-work-verify-be6322`: five failures because excess connections received `b'ready'` instead of EOF.
- Focused receipt `20261011-024529-work-verify-631bb9` passed using `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_fleet_hub_server.py","tests/test_fleet_sync.py","tests/test_fleet_nodes.py"]' --capture brigade-work`.
- Review-fix receipt `20261011-024433-work-verify-a3a413` reproduced `KeyError: 'source-a'` on double release before the one-shot fix.
- Full-gate receipt `20261011-024243-work-verify-b413ea` returned status 75: `full verification already running for this checkout; run ./scripts/verify-focused <pytest-selector>... or wait for the active Brigade receipt`. No lock bypass or retry was used.
- Code graph sync and affected analysis covered `src/brigade/fleet_hub.py`. Symbol callers identified `fleet_hub.run`; static test attribution was incomplete, so existing hub factory suites were included.

## Recommended memory action

create-card

## Target card

fleet-hub-connection-admission.md

## Suggested card content

```markdown
---
topic: Fleet Hub connection admission
category: security
tags: [brigade, fleet-hub, availability]
---

# Fleet Hub connection admission

`fleet_hub.make_server` uses `BoundedThreadingHTTPServer` to reserve capacity before starting a pre-auth handler. Its fixed limits are 32 handlers globally and eight per TCP source address. Excess sockets close immediately. Completion, handler error and thread-start failure return capacity once, and empty source counters are removed. Reverse proxy clients share the proxy's source budget. HTTP authentication and socket deadlines retain their existing behavior. The change addresses connection admission within issue #1151.
```
