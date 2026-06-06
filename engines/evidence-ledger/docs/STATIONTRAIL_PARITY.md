# StationTrail Boundary

StationTrail and MiseLedger are intended to work together, not compete for the same product surface.

StationTrail owns local harness export. MiseLedger owns durable archive ingest, SQLite storage, FTS, relations, scan manifests, evidence bundles, HTTP, and MCP.

## Support Matrix

| Source | StationTrail | MiseLedger native | Recommended path |
| --- | --- | --- | --- |
| Codex sessions | yes | yes | Use StationTrail for scanner parity, use native MiseLedger for compatibility and fixture smoke. |
| Claude project logs | yes | yes | Use StationTrail for scanner parity, use native MiseLedger for compatibility and fixture smoke. |
| OpenClaw sessions and trajectories | yes | yes | Use StationTrail for scanner parity, use native MiseLedger for compatibility and fixture smoke. |
| OpenCode sessions | yes | no | Export with StationTrail and import adapter JSONL into MiseLedger. |
| Hermes sessions | yes | yes | Use either path for snapshots and trajectory JSONL. MiseLedger does not parse Hermes `state.db` directly. |
| Future harnesses | preferred owner | sample-gated | Add parser support to StationTrail first unless MiseLedger needs a minimal compatibility adapter. |

## Practical Split

Use StationTrail when the task is:

- discover local harness roots
- inspect live source shapes without transcript content
- dry-run scanner coverage
- redact paths or secret-like values during export
- export OpenCode or future harness logs
- keep parser-specific logic out of the archive layer

Use MiseLedger when the task is:

- import `miseledger.adapter.v1` JSONL
- track scan manifests
- search across crawlers, local source exports, and agent sessions
- show normalized items
- resolve relations
- create stable evidence bundles for Brigade or agents
- serve local HTTP or MCP
- run archive maintenance and doctor checks

## Commands

StationTrail to MiseLedger:

```bash
stationtrail all --out - --redact paths,secrets | miseledger import adapter - --json
stationtrail opencode ./session.jsonl --out - | miseledger import adapter - --json
miseledger import stationtrail codex ~/.codex/sessions --json
miseledger import stationtrail claude ~/.claude/projects --json
miseledger import stationtrail openclaw ~/.openclaw/agents --json
miseledger import stationtrail hermes ~/.hermes/sessions --json
```

MiseLedger compatibility adapters:

```bash
miseledger import codex ~/.codex/sessions --json
miseledger import claude ~/.claude/projects --json
miseledger import openclaw ~/.openclaw/agents --json
miseledger import hermes ~/.hermes/sessions --json
```

## Non-Goals

MiseLedger should not chase session browser parity, resume workflows, GUI features, or every harness parser. It should keep native parsers conservative and sample-gated, while StationTrail can evolve as the dedicated harness exporter.
