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

## Qualified Relation Targets

Relations may target an item in another source using the versioned qualified
target object:

```json
{"type":"derived_from","target":{"source":"brigade","collection":"brigade:receipts","external_id":"receipt:handoff-demo-1"}}
```

Fields:

- `target.source`: target `sources.kind`
- `target.collection`: target `collections.external_id` (optional; when empty any collection in that source may match)
- `target.external_id`: target `items.external_id`

Legacy adapters that only set `target_external_id` remain valid. Those relations
resolve within the source item's own `source_id`, matching pre-Slice-1a behavior.
When `target` is present it takes precedence for resolution.

Unresolved external ids stay stored with `target_item_id` null and remain
queryable for later backfill via `miseledger relations backfill`.

## Native Memory Card Source

```bash
miseledger crawl memory <workspace> [--json] [--dry-run] [--rebuild] [--limit N]
```

The native `brigade-memory` source walks `memory/cards/**/*.md`, parses the flat
Brigade frontmatter subset (not full YAML), and emits `miseledger.adapter.v1`
records with `item.kind=memory_card`.

### Operator-declared memory namespace

Every memory root must declare an opaque namespace id in `memory/NAMESPACE`:

```text
memory-<uuid4>
```

Rules:

- Generated once by the canonical memory owner, copied with the canonical store,
  and never derived from basenames, absolute paths, gitignored aliases, or clone
  paths.
- `collection.external_id` is the namespace id itself. Item identity, tombstones,
  relation health, and rebuild are scoped to that collection.
- Two roots that share the same explicit card id remain isolated when their
  namespaces differ.
- Legacy pre-namespace rows use `collection.external_id=memory:cards`. Namespaced
  crawls dual-read them for diagnostics and never tombstone or rebuild them
  (scoped-rebuild rule). Migration onto a namespace is an explicit operator step
  outside the crawl path.
- The engine does not mint or backfill card ids. Markdown remains canonical.

Identity rules:

- Explicit opaque `id` / `card_id` frontmatter wins (`identity_source=explicit_id`).
- Otherwise the normalized workspace-relative path is used (`identity_source=path`,
  external id `path:<rel>`).
- Explicit-id renames preserve identity. Path-fallback renames are remove+create.
- `topic`, title, filename stem, body text, and the #724 content fingerprint are
  not canonical identity.
- Duplicate explicit ids fail the scan before reconciliation (no last-wins).

Duplicate detection:

- The engine reuses Brigade #724 `content_fingerprint` (normalize: strip
  frontmatter, lowercase, strip punctuation, collapse whitespace, SHA-256).
- Fingerprints are stored in item metadata for duplicate detection only and are
  never used as `item.external_id`.

Completed-scan reconciliation:

- Only a completed memory scan may soft-tombstone missing cards, and only within
  the active namespace collection.
- Failed or interrupted scans tombstone nothing and mark the prior completed
  snapshot for that namespace stale.
- `--rebuild` validates/walks first, then replaces the derived projection for the
  active namespace. A failed rebuild restores prior live ids/hashes, keeps the
  prior completed-scan record, and marks health stale or partial.
- A transaction failure between observation and reconciliation creates no
  tombstones.
- Default retention tiers must never match `memory_card` items.
- Observable projection states: live, removed after completed scan, stale, and
  partial scan.

Scan receipts expose `scan_id`, `memory_namespace`, `created`, `updated`,
`unchanged`, `removed`, `skipped`, and `failed`. `unresolved_relations` is
counted after the final relation-resolution pass and must agree across crawl
JSON, the scan record, and health. Doctor/status expose capability/version, last
completed scan, canonical/live counts, hash divergence, unresolved relations,
malformed/skipped counts, and stale/partial state.

Size bounds:

- Cards larger than 2 MiB are skipped.
- Item text longer than 256 KiB is truncated with a trailing `[truncated]` marker.

## Built-In Crawlers

MiseLedger's user-facing crawlers generate the same `miseledger.adapter.v1` records internally and stream them through normal ingest:

```bash
miseledger crawl sessions --json
miseledger crawl memory <workspace> --json
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
miseledger crawl gmail --account "$GMAIL_ACCOUNT" --query "subject:miseledger" --json
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
