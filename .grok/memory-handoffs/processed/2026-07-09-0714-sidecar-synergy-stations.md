# Memory Handoff

## Type

decision

## Title

Sidecar synergy stations + Grok harness wiring

## Summary

Implemented first-class search/tokens station CLIs, station.json discover, usage-tracker and plating managed tools, shared station_health helper, and docs. Grok is already a first-class Brigade writer harness; this workspace was reconfigured to include grok so handoffs land in `.grok/memory-handoffs/`. Token Glace is the current name (TokenJuice is the old name).

## Durable facts

- Grok harness is first-class in Brigade (`KNOWN_HARNESSES`, `WRITER_INBOXES`, skills adapter, templates). Workspace config needs `--harnesses ...,grok` to materialize `.grok/memory-handoffs/`.
- Core station CLIs (always registered): evidence, search, tokens. Extras-gated: pantry, notifications.
- Shared health schema lives in `brigade.station_health` (health, next_commands, docs, boundaries, plan write).
- `brigade stations discover` finds local `station.json` (schema brigade.station.v1).
- Managed tools added: usage-tracker (tokens), plating (guard). Search registry lists graphtrail + code-search-*.
- Token Glace is current; TokenJuice is the old name.

## Evidence

- files changed: `src/brigade/search_cmd.py`, `tokens_cmd.py`, `station_health.py`, `stations_cmd.py`, `managed.py`, `registry.py`, CLI modules, tests, QUICKSTART, technical-guide
- commands run: `brigade work verify run --target . --command "./scripts/verify" --capture brigade-work` (passed, run 20260709-071107-work-verify-a8702f)
- branch: `feat/sidecar-synergy-stations`

## Recommended memory action

create-card

## Target card

sidecar-station-clis.md

## Suggested card content

First-class station CLIs for evidence/search/tokens plan and health-check process-boundary sidecars. Discover external station.json with `brigade stations discover`. Grok writer inbox is `.grok/memory-handoffs/` when harness selected.
