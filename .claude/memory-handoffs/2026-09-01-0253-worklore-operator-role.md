# Memory Handoff

## Type
decision

## Title
Worklore operator role uses enrolled node tokens, not the Hub admin token

## Summary
Native Worklore mutations now authorize a Worklore-only operator role from `fleet.worklore.operator_nodes` (optional bounded env override `BRIGADE_WORKLORE_OPERATOR_NODES`). Ops Deck and `brigade fleet work` use an enrolled node token. The shared Hub admin token remains a backwards-compatible override and is not required or deployed for Ops Deck. Native create is retry-safe via `Idempotency-Key`.

## Durable facts
- An authenticated node is operator only when its `node_id` is configured. Operator nodes may create, patch, transition, reset attempts, and mutate operator links. Other nodes keep read, import, execution-link, and owned attempt permissions.
- Operator-node audit events keep `actor_id` and `node_id` as that node and use `actor_type=operator`. Admin-token events stay `actor_id=admin` with `node_id` unset.
- `POST /work/items` accepts `Idempotency-Key` (max 256). Table `work_create_keys` is keyed by `(actor_id, idempotency_key)`. Same key and same canonical payload returns the original item with no second event. Same key and a different payload returns 409 `import-conflict`.
- `worklore_client.create_item` and `brigade fleet work create` send a node token and generate or accept the key. Do not send the Hub admin token for normal Worklore work.

## Evidence
- files changed: `src/brigade/worklore_http.py`, `src/brigade/worklore_store.py`, `src/brigade/worklore_client.py`, `src/brigade/fleet_hub_http.py`, `src/brigade/cli/fleet.py`, `tests/test_worklore_http.py`, `tests/test_worklore_store.py`, `tests/test_worklore_client.py`, `docs/phase-worklore-ledger.md`, `docs/phase-worklore-opsdeck-plan.md`, `docs/phase-worklore-core-plan.md`
- commands run: `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_worklore_http.py","tests/test_worklore_store.py","tests/test_worklore_client.py"]' --capture brigade-work`
- receipt: `20260901-025323-work-verify-9a20c7`, `47 passed in 4.21s`

## Recommended memory action
create-card

## Target card
worklore-operator-role.md

## Suggested card content
---
title: Worklore operator role
category: architecture
updated: 2026-09-01
---

Authorize native Worklore mutations with enrolled operator node tokens listed in `fleet.worklore.operator_nodes` or `BRIGADE_WORKLORE_OPERATOR_NODES`. Keep the Hub admin token as a backwards-compatible override only. Ops Deck and CLI must not send it. Native create retries use `Idempotency-Key` stored in `work_create_keys` per operator identity; same payload replays, different payload returns `import-conflict`. Operator-node events keep the node id and `actor_type=operator`.
