# Memory Handoff

## Type

decision

## Title

Sidecar station CLIs: search, tokens, discover, plating

## Summary

Shipped first-class search/tokens station CLIs, stations discover for station.json, usage-tracker and plating managed tools, and shared station_health. Grok is already a first-class harness; this workspace now selects it. Token Glace is the current name (TokenJuice is old).

## Durable facts

- Core station CLIs always registered: evidence, search, tokens (status/doctor + review-only plans).
- Extras-gated: pantry and notifications require brigade extras on or BRIGADE_EXTRAS=1.
- brigade stations discover finds local station.json (brigade.station.v1) and prints brigade add <path>.
- Managed tools: usage-tracker on tokens; plating on guard; search lists graphtrail + code-search-*.
- Shared schema in brigade.station_health (health, next_commands, docs, boundaries, plan write).
- Token Glace is current name; TokenJuice is the old name.
- Grok writer inbox is .grok/memory-handoffs/; select with --harnesses ...,grok.

## Evidence

- files changed: src/brigade/search_cmd.py, tokens_cmd.py, station_health.py, stations_cmd.py, managed.py, registry.py, cli/search.py, cli/tokens.py, tests, QUICKSTART, technical-guide
- commands run: brigade work verify run --target . --command "./scripts/verify" --capture brigade-work (passed run 20260709-071107-work-verify-a8702f)
- PR: https://github.com/escoffier-labs/brigade/pull/179 branch feat/sidecar-synergy-stations

## Recommended memory action

create-card

## Target card

sidecar-station-clis.md

## Suggested card content

---
topic: sidecar-station-clis
category: architecture
tags: [brigade, stations, search, tokens, evidence, pantry, grok, token-glace]
---

# Sidecar station CLIs and catalogs

First-class station command groups plan and health-check process-boundary sidecars; they do not fold binaries into Brigade.

### Core station CLIs (always registered)

- `brigade evidence` — status, doctor, crawl plan, export plan (MiseLedger)
- `brigade search` — status, doctor, sync plan (GraphTrail + optional code-search)
- `brigade tokens` — status, doctor, wire plan (Token Glace + optional usage-tracker)

### Extras-gated

- `pantry`, `notifications` need `brigade extras on` or `BRIGADE_EXTRAS=1`

### Catalog discover

- `brigade stations list` — built-in catalog + managed surfaces
- `brigade stations discover` — local `station.json` (`schema: brigade.station.v1`), prints `brigade add <path>`

### Managed tools added

- tokens: `usage-tracker`
- guard: `plating` (optional publish demos / leak scan / drift verify)
- search already includes `graphtrail`, `code-search-api`, `code-search-mcp`

### Shared health schema

- `brigade.station_health`: installed, health, summary, next_commands, docs, boundaries, plan write under `.brigade/<station>/plans/`

### Naming

- Token Glace is current; TokenJuice is the old name

### Grok harness

- First-class writer: inbox `.grok/memory-handoffs/`, skills `.grok/skills/{id}`
- Select with `--harnesses ...,grok` (this workspace reconfigured to include grok)

### Operator path

- Verify: `brigade work verify run --target . --command "./scripts/verify" --capture brigade-work`
- PR: https://github.com/escoffier-labs/brigade/pull/179
