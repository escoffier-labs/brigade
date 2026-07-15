# All Crawlers and Session Ingestion

## Goal

MiseLedger must ingest the local Grok and current Cursor session formats, and every documented external crawler command must have an end-to-end contract test against the command it invokes.

## Definition of working

A native source is working when a synthetic fixture passes through adapter generation, archive import, and search. An external crawler wrapper is working when a synthetic exporter or public JSON command passes through the wrapper, archive import, and search. A count-only dry run against a configured local archive supplies an extra compatibility check but is not required in CI.

Missing binaries, accounts, credentials, or source configuration are environment states. MiseLedger must report them before opening its archive. They do not justify embedding GitHub, Slack, Granola, Notion, Gmail, or Discord API clients in this repository.

## Native Grok sessions

Add `internal/sources/grok`. The default root is `~/.grok/sessions`.

The generator reads two observed, plain-text surfaces:

- `**/summary.json` supplies the session title, summary, model, timestamps, git metadata, workspace, and session counts.
- `**/chat_history.jsonl` supplies system, user, assistant, reasoning, and tool-result messages through the stable `type` and `content` fields.

Each session directory becomes an `agent_session` collection with external ID `grok:session:<session-id>`. The session ID is the leaf directory name. The encoded workspace directory above it is URL-decoded when possible. Summary and chat items use stable IDs derived from the session ID, ordinal, event type, and content hash. Empty or unsupported events become warnings instead of records.

Generated records use source kind `grok`, tags `agent-session` and `grok`, and metadata fields for `harness`, `session_id`, `event_type`, `model`, `workspace`, `source_file`, and `ordinal` when available. Imported content remains untrusted evidence and passes through the existing redaction pipeline.

Expose Grok through:

- `miseledger crawl sessions`
- `miseledger import discovered`
- `miseledger import grok <path>`
- `miseledger adapter grok <path> --out <file|->`
- `miseledger sources discover`
- `miseledger watch once|daemon`

## Current Cursor history

Keep the existing parser for legacy `prompt_history.json`, `chats/*/meta.json`, and `acp-sessions/*/meta.json` inputs.

The default Cursor root changes to the current IDE user-data directory:

- Linux: `$XDG_CONFIG_HOME/Cursor/User`, falling back to `~/.config/Cursor/User`
- macOS: `~/Library/Application Support/Cursor/User`
- Windows: `%APPDATA%/Cursor/User`

When the input is a Cursor user-data directory or a direct `conversation-search.db` path, read `globalStorage/conversation-search.db` in SQLite read-only mode. Join `conversations` to `conversation_fts` by `fts_rowid` and emit one `agent_session` collection and one searchable message item per conversation. Store title, body, updated timestamp, archive state, scope, and root fingerprint. Ignore unknown added columns.

An active Cursor database may keep new rows in `conversation-search.db-wal`. Scan-manifest decisions must include the WAL state. The generator uses the WAL file as its scan target when it exists and the main database otherwise. The raw reference still points to `conversation-search.db`.

The parser must reject databases that lack the required `conversations` and `conversation_fts` surfaces with a clear warning. It must not write, checkpoint, or migrate Cursor's database.

## External crawler wrappers

Keep these source-owned adapter commands:

| MiseLedger command | Binary command |
|---|---|
| `crawl discord` | `discrawl export adapter --out -` |
| `crawl github` | `gitcrawl export adapter --out -` |
| `crawl slack` | `slacrawl export adapter --out -` |
| `crawl granola` | `graincrawl export adapter --out -` |
| `crawl notion` | `notcrawl export adapter --out -` |
| `crawl gmail` | `mailcrawl gmail export --out -` |

Telecrawl 0.1.0 does not expose `export adapter`, but it does expose a stable read-only JSON command. `crawl telegram` therefore invokes `telecrawl --json messages`, converts the returned message array into `miseledger.adapter.v1` records, and streams those records through the normal importer. `--limit`, `--chat`, and `--after` pass through. MiseLedger's `--since` maps to Telecrawl's `--after`. `--dry-run` counts converted records without opening the MiseLedger archive.

Telegram records use source kind `telegram`, a `telegram:chat:<chat-jid>` collection, a stable message ID from `message_id` or `source_pk`, sender actors, timestamp, text, message and media metadata, and `telecrawl://messages` as the raw source reference. Empty text is retained only when media title or media type supplies searchable evidence.

Tests must exercise all seven wrapper command lines. The fake binaries must reject incorrect argument order, emit at least one synthetic record, and prove the record is searchable after import. Telegram tests use the public JSON message shape rather than a fictional adapter command.

`doctor --json` continues to report whether wrapper binaries exist. Runtime configuration and authentication errors remain the external binary's diagnostics, prefixed with the MiseLedger crawl source.

## Failure handling

- Native malformed JSON produces an attributed warning and continues with later files or lines.
- Source database open and query errors fail that source without corrupting prior imported data.
- External non-zero exits preserve the crawler's stderr in one MiseLedger diagnostic.
- Missing tools are detected before the MiseLedger archive is opened.
- Dry runs never open or mutate the MiseLedger archive.
- Limits apply to emitted adapter records, not files or sessions.

## Tests and verification

Add synthetic Grok JSON and JSONL fixtures and a synthetic Cursor SQL fixture under `testdata/harnesses`. Tests cover record fields, search text, malformed input, limits, date filtering, scan manifests, default roots, direct imports, discovered imports, and dry runs.

Required verification after the final edit:

```text
go vet ./...
go test ./...
scripts/smoke_archive.sh
```

The archive smoke covers import and search behavior. HTTP and MCP behavior do not change, so their smoke scripts are outside this change.

## Documentation

Update the README, adapter contract, quickstart, examples, installation smoke guide, adjacent-tools comparison, StationTrail parity note, roadmap, and changelog. Counts and source lists must name Grok and current Cursor storage. Telegram documentation must describe its JSON compatibility path. Do not claim local Gitcrawl, Slack, or Gmail configuration exists when it does not.

## Out of scope

- Installing or configuring Gitcrawl, Slacrawl, Mailcrawl, or provider credentials
- Network synchronization for any crawler
- Cursor cloud-only background-agent history
- Decoding Cursor's private binary stores outside the local conversation-search database
- Grok event telemetry, system prompts, resource state, rewind points, or other non-chat session files
- Changes to external crawler repositories
