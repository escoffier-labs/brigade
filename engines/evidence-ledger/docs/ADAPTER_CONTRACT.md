# Logspine Adapter Contract

`logspine.adapter.v1` is a JSONL contract for source tools that want to feed Logspine without knowing the database schema.

Each line is one JSON object. Unknown fields are tolerated and preserved in `items.raw_json`.

Required fields:

- `schema`: must be `logspine.adapter.v1`
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
{"schema":"logspine.adapter.v1","source":{"kind":"discrawl","name":"Discrawl","version":"0.6.0"},"collection":{"external_id":"discord:guild:demo/channel:ai-crawl","kind":"discord_channel","name":"#ai-crawl"},"item":{"external_id":"discord:message:1","kind":"message","created_at":"2026-06-03T12:39:06-04:00","text":"adapter contract example","summary":null,"tags":["logspine"]},"actor":{"external_id":"discord:user:demo","type":"human","name":"Demo User"},"artifacts":[],"links":[],"relations":[],"raw":{"format":"json","hash":"sha256:<hash>","path":"raw/discrawl/ai-crawl.jsonl","ordinal":1}}
```

Identity boundary:

```text
source_kind + collection_external_id + item_external_id + content_hash
```

If a source lacks stable IDs, adapters should create deterministic external IDs from source path, ordinal, timestamp, actor, kind, and normalized content hash.

## Native Adapter Generators

Logspine includes conservative native generators for local agent-session JSONL:

```bash
spine adapter codex <path-or-dir> --out <file|->
spine adapter openclaw <path-or-dir> --out <file|->
spine adapter claude <path-or-dir> --out <file|->
```

They emit the same `logspine.adapter.v1` JSONL contract as external tools. Native import commands generate adapter records and reuse the adapter import path internally:

```bash
spine import codex <path-or-dir> --json
spine import openclaw <path-or-dir> --json
spine import claude <path-or-dir> --json
spine import discovered --json
spine watch once --json
spine watch once --if-changed --json
```

Scanner rules:

- Accept a file or directory.
- Walk recursively for relevant `.jsonl` files.
- Skip obvious backups, deleted files, `skills-prompts`, and sidecar metadata.
- Preserve raw refs with `raw.format=json`, `raw.path`, `raw.ordinal`, and `raw.hash`.
- Never crash on unknown event shapes. Emit warnings and keep going.
- Use deterministic external IDs from file path, session ID, ordinal, event type, timestamp, and content hash.
- Keep `item.text` searchable without dumping huge raw JSON blobs as text.
- Store non-secret structure in `item.metadata`, including harness, event type, session ID, run ID, model, workspace or cwd, file path, and ordinal where available.
- Stream generated adapter records into ingest during native imports.
- Record source-file scan manifests with path, size, mtime, content hash, generated hash, record count, and warnings.

Claude support targets `~/.claude/projects/**/*.jsonl` style project logs. The MVP scanner imports ordinary project session JSONL and does not special-case subagents yet; subagent lines are treated as normal agent-session evidence unless a future fixture shows a safer split.

## AgentTrail External Scanner

AgentTrail is a separate scanner/exporter for local agent session logs. It emits this same `logspine.adapter.v1` JSONL contract and can be piped directly into adapter ingest:

```bash
agenttrail codex ~/.codex/sessions --out - | spine import adapter -
agenttrail claude ~/.claude/projects --out - | spine import adapter -
agenttrail openclaw ~/.openclaw/agents --out - | spine import adapter -
agenttrail hermes ~/.hermes/sessions --out - | spine import adapter -
agenttrail all --out - --redact paths,secrets | spine import adapter -
```

Or let Logspine run AgentTrail when the `agenttrail` binary is installed on `PATH`:

```bash
spine import agenttrail codex ~/.codex/sessions --json
spine import agenttrail claude ~/.claude/projects --json
spine import agenttrail openclaw ~/.openclaw/agents --json
spine import agenttrail opencode opencode-session.json --json
spine import agenttrail hermes ~/.hermes/sessions --json
```

Use AgentTrail when source-specific harness parsing should live outside Logspine. Keep Logspine focused on ingest, normalized storage, FTS, scan manifests, relation resolution, and evidence output.

AgentTrail `discover`, `doctor`, `doctor --live`, `inspect`, and `--dry-run --json` modes report roots, structural keys, counts, records, and warnings without printing transcript content. Logspine's `import agenttrail` wrapper records AgentTrail scan manifests when AgentTrail writes summary output. For `agenttrail all`, prefer piping to `spine import adapter -` so mixed-source records retain their individual `source.kind`.

Scan manifests can be compared without reading transcript content into output:

```bash
spine scans diff <path> --json
spine scans changed --json
```

SourceHarvest can be wrapped directly by Logspine when the `sourceharvest` binary is installed on `PATH`:

```bash
spine import sourceharvest markdown ./notes --source notes --collection notes:local --json
spine import sourceharvest files ./notes --source notes --collection notes:files --glob "*.md,*.txt" --json
spine import sourceharvest html ./site-export --source docs --collection docs:html --json
spine import sourceharvest gitlog . --source gitlog --collection repo:logspine --json
spine import sourceharvest json export.json --source export --collection export:records --records-path records --json
```
