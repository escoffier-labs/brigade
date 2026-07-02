# MiseLedger Adapter Contract

`miseledger.adapter.v1` is a JSONL contract for source tools that want to feed MiseLedger without knowing the database schema.

Each line is one JSON object. Unknown fields are tolerated and preserved in `items.raw_json`.

Required fields:

- `schema`: must be `miseledger.adapter.v1`
- `source.kind`
- `collection.external_id`
- `collection.kind`
- `item.external_id`
- `item.kind`

Recommended fields:

- `item.created_at` as RFC3339
- `item.text` for FTS search
- `item.metadata` for useful non-secret source structure
- `actor.external_id`, `actor.type`, and `actor.name`
- `artifacts` for files, command output, patches, screenshots, logs, and URLs
- `links` for URL references. The importer persists them as `artifact.kind=url`
- `raw.format`, `raw.hash`, `raw.path`, and `raw.ordinal`

Example:

```json
{"schema":"miseledger.adapter.v1","source":{"kind":"discrawl","name":"Discrawl","version":"0.6.0"},"collection":{"external_id":"discord:guild:demo/channel:ai-crawl","kind":"discord_channel","name":"#ai-crawl"},"item":{"external_id":"discord:message:1","kind":"message","created_at":"2026-06-03T12:39:06-04:00","text":"adapter contract example","summary":null,"tags":["miseledger"]},"actor":{"external_id":"discord:user:demo","type":"human","name":"Demo User"},"artifacts":[],"links":[],"relations":[],"raw":{"format":"json","hash":"sha256:<hash>","path":"raw/discrawl/ai-crawl.jsonl","ordinal":1}}
```

Identity boundary:

```text
source_kind + collection_external_id + item_external_id + content_hash
```

If a source lacks stable IDs, adapters should create deterministic external IDs from source path, ordinal, timestamp, actor, kind, and normalized content hash.

## Native Adapter Generators

MiseLedger includes conservative native generators for local agent-session JSON and JSONL:

```bash
miseledger adapter codex <path-or-dir> --out <file|->
miseledger adapter openclaw <path-or-dir> --out <file|->
miseledger adapter claude <path-or-dir> --out <file|->
miseledger adapter hermes <path-or-dir> --out <file|->
miseledger adapter opencode <path-or-dir> --out <file|->
```

They emit the same `miseledger.adapter.v1` JSONL contract as external tools. Native import commands generate adapter records and reuse the adapter import path internally:

```bash
miseledger import codex <path-or-dir> --json
miseledger import openclaw <path-or-dir> --json
miseledger import claude <path-or-dir> --json
miseledger import hermes <path-or-dir> --json
miseledger import opencode <path-or-dir> --json
miseledger import discovered --json
miseledger watch once --json
miseledger watch once --if-changed --json
```

Scanner rules:

- Accept a file or directory.
- Walk recursively for relevant `.jsonl` files and source-specific JSON files such as Hermes `session_*.json` snapshots.
- Skip obvious backups, deleted files, `skills-prompts`, and sidecar metadata.
- Preserve raw refs with `raw.format=json`, `raw.path`, `raw.ordinal`, and `raw.hash`.
- Never crash on unknown event shapes. Emit warnings and keep going.
- Use deterministic external IDs from file path, session ID, ordinal, event type, timestamp, and content hash.
- Keep `item.text` searchable without dumping huge raw JSON blobs as text.
- Store non-secret structure in `item.metadata`, including harness, event type, session ID, run ID, model, workspace or cwd, file path, and ordinal where available.
- Stream generated adapter records into ingest during native imports.
- Record source-file scan manifests with path, size, mtime, content hash, generated hash, record count, and warnings.

Claude support targets `~/.claude/projects/**/*.jsonl` style project logs. The MVP scanner imports ordinary project session JSONL and does not special-case subagents yet; subagent lines are treated as normal agent-session evidence unless a future fixture shows a safer split.

Hermes support targets `~/.hermes/sessions/session_*.json` snapshots and trajectory JSONL. MiseLedger does not parse Hermes `state.db` directly.

## StationTrail External Scanner

StationTrail is a separate scanner/exporter for local agent session logs. It emits this same `miseledger.adapter.v1` JSONL contract and can be piped directly into adapter ingest:

```bash
stationtrail codex ~/.codex/sessions --out - | miseledger import adapter -
stationtrail claude ~/.claude/projects --out - | miseledger import adapter -
stationtrail openclaw ~/.openclaw/agents --out - | miseledger import adapter -
stationtrail hermes ~/.hermes/sessions --out - | miseledger import adapter -
stationtrail all --out - --redact paths,secrets | miseledger import adapter -
```

Or let MiseLedger run StationTrail when the `stationtrail` binary is installed on `PATH`:

```bash
miseledger import stationtrail codex ~/.codex/sessions --json
miseledger import stationtrail claude ~/.claude/projects --json
miseledger import stationtrail openclaw ~/.openclaw/agents --json
miseledger import stationtrail opencode opencode-session.json --json
miseledger import stationtrail hermes ~/.hermes/sessions --json
```

Use StationTrail when source-specific harness parsing should live outside MiseLedger. MiseLedger also has native parsers for Codex, Claude, OpenClaw, OpenCode sanitized export JSON, and Hermes snapshot or trajectory files. Keep MiseLedger focused on ingest, normalized storage, FTS, scan manifests, relation resolution, and evidence output.

StationTrail `discover`, `doctor`, `doctor --live`, `inspect`, and `--dry-run --json` modes report roots, structural keys, counts, records, and warnings without printing transcript content. MiseLedger's `import stationtrail` wrapper records StationTrail scan manifests when StationTrail writes summary output. For `stationtrail all`, prefer piping to `miseledger import adapter -` so mixed-source records retain their individual `source.kind`.

Scan manifests can be compared without reading transcript content into output:

```bash
miseledger scans diff <path> --json
miseledger scans changed --json
```

SourceHarvest can be wrapped directly by MiseLedger when the `sourceharvest` binary is installed on `PATH`:

```bash
miseledger import sourceharvest markdown ./notes --source notes --collection notes:local --json
miseledger import sourceharvest files ./notes --source notes --collection notes:files --glob "*.md,*.txt" --json
miseledger import sourceharvest html ./site-export --source docs --collection docs:html --json
miseledger import sourceharvest gitlog . --source gitlog --collection repo:miseledger --json
miseledger import sourceharvest json export.json --source export --collection export:records --records-path records --json
```

The wrapper requests SourceHarvest's summary JSON internally, streams adapter JSONL through normal ingest, and records a lightweight scan manifest when the summary path can be statted locally. The manifest contains path, size, mtime, content hash for regular files or summary hash for directories, generated adapter hash, record count, and warning count. It does not contain harvested text.
