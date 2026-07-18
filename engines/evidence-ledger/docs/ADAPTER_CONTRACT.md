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
miseledger adapter cursor <path-or-dir> --out <file|->
miseledger adapter grok <path-or-dir> --out <file|->
```

They emit the same `miseledger.adapter.v1` JSONL contract as external tools. Native import commands generate adapter records and reuse the adapter import path internally:

```bash
miseledger import codex <path-or-dir> --json
miseledger import openclaw <path-or-dir> --json
miseledger import claude <path-or-dir> --json
miseledger import hermes <path-or-dir> --json
miseledger import opencode <path-or-dir> --json
miseledger import cursor <path-or-dir> --json
miseledger import grok <path-or-dir> --json
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

Claude support targets `~/.claude/projects/**/*.jsonl` style project logs. The MVP scanner imports ordinary project session JSONL and does not special-case subagents yet. Subagent lines are treated as normal agent-session evidence unless a future fixture shows a safer split.

Hermes support targets `~/.hermes/sessions/session_*.json` snapshots and trajectory JSONL. MiseLedger does not parse Hermes `state.db` directly.

Grok support targets `~/.grok/sessions/**/summary.json` and `chat_history.jsonl`. Cursor support targets the current read-only `User/globalStorage/conversation-search.db` search database and retains the older prompt-history and chat-metadata JSON layout.

## Built-In Crawlers

MiseLedger's user-facing crawlers generate the same `miseledger.adapter.v1` records internally and stream them through normal ingest:

```bash
miseledger crawl sessions --json
miseledger crawl cursor --json
miseledger crawl chatgpt-export ~/Downloads/chatgpt-export.zip --json
miseledger crawl claude-export ~/Downloads/claude-export.zip --json
```

For local artifacts and git history:

```bash
miseledger crawl docs ./notes --json
miseledger crawl files ./notes --source notes --collection notes:files --glob "*.md,*.txt" --json
miseledger crawl html ./site-export --source docs --collection docs:html --json
miseledger crawl gitlog . --source gitlog --collection repo:miseledger --json
miseledger crawl json export.json --source export --collection export:records --records-path records --json
miseledger crawl jsonl export.jsonl --source notes --collection notes:local --json
```

External crawler binaries can emit adapter JSONL directly or run through MiseLedger domain wrappers:

```bash
miseledger crawl discord --limit 100 --json
miseledger crawl github --repo escoffier-labs/miseledger --json
miseledger crawl github --repo escoffier-labs/miseledger --numbers 34,35 --limit 2 --json
miseledger crawl slack --workspace T123 --json
miseledger crawl granola --json
miseledger crawl notion --json
miseledger crawl gmail --account me@example.com --query "subject:miseledger" --json
miseledger crawl telegram --chat "MiseLedger" --json
miseledger import adapter discrawl.adapter.jsonl --json
```

The Discord, Slack, Granola, Notion, and Gmail wrappers consume adapter JSONL emitted by their crawler binaries. GitHub is a current-Gitcrawl compatibility route: MiseLedger calls `gitcrawl sync` and `gitcrawl threads --json`, then converts returned issue and pull-request rows locally. Telegram is also a compatibility route: MiseLedger calls `telecrawl --json messages` and converts the public message array to adapter records locally.

The archived StationTrail and SourceHarvest repositories previously emitted this same adapter contract. Already-generated adapter JSONL files from those tools remain importable with `miseledger import adapter`.

Scan manifests can be compared without reading transcript content into output:

```bash
miseledger scans diff <path> --json
miseledger scans changed --json
```

Crawler and native-import scan manifests contain path, size, mtime, content hash for regular files or summary hash for directories, generated adapter hash, record count, and warning count. They do not contain harvested text.
