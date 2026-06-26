# MiseLedger

<p>
  <a href="https://github.com/escoffier-labs/miseledger/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/escoffier-labs/miseledger/ci.yml?branch=master&style=for-the-badge&label=ci" alt="CI status"></a>
  <a href="https://github.com/escoffier-labs/miseledger/releases"><img src="https://img.shields.io/github/v/release/escoffier-labs/miseledger?style=for-the-badge&label=release" alt="Latest release"></a>
  <img src="https://img.shields.io/badge/go-1.22%2B-00ADD8?style=for-the-badge&logo=go&logoColor=white" alt="Go 1.22+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=for-the-badge" alt="MIT license"></a>
</p>

MiseLedger turns scattered AI work history into a local searchable evidence graph.

The MVP is a local-first CLI named `miseledger`. It crawls agent sessions and local project artifacts, imports `miseledger.adapter.v1` JSONL records into SQLite, preserves raw payload references, searches with SQLite FTS5, shows normalized items, exports Markdown, emits Brigade-ready evidence bundles, and allows read-only SQL inspection.

Each source system is best at its native domain:

- [StationTrail](https://github.com/escoffier-labs/stationtrail): Codex, Claude, OpenClaw, OpenCode, Hermes, and related local session logs
- [SourceHarvest](https://github.com/escoffier-labs/sourceharvest): local files, notes, generic exports, git history, and future crawler adapter exports
- `discrawl`: Discord messages
- `gitcrawl`: GitHub issues and pull requests
- `graincrawl`: Granola notes and transcripts
- `notcrawl`: Notion pages and databases
- `slacrawl`: Slack messages and threads
- `telecrawl`: Telegram Desktop archive data

MiseLedger is the normalized evidence layer above those systems, not a replacement for them.

<p align="center">
  <img src="docs/assets/miseledger-ledger.svg" alt="Recording: miseledger init creates a local ledger, import loads adapter records, search runs FTS5, and stats summarizes the evidence graph" width="760">
</p>

Initialize a local SQLite ledger, import `adapter.v1` records from your sources, then search them with FTS5 and roll them up with `stats` (or export Brigade-ready evidence bundles). Everything stays on your machine.

## How It Works

```mermaid
flowchart TB
    ADAPTER["<b>Adapter JSONL</b><br/>file, stdin, wrappers"]

    subgraph SOURCES [" source exporters "]
        STATIONTRAIL["<b>StationTrail</b><br/>agent-session logs"]
        SOURCEHARVEST["<b>SourceHarvest</b><br/>files, notes, exports, git"]
        NATIVE["<b>Native adapters</b><br/>Codex, OpenClaw, Claude, Hermes"]
    end

    STATIONTRAIL & SOURCEHARVEST & NATIVE --> ADAPTER

    subgraph INGEST [" ingest path "]
        PARSE["<b>Parse and validate</b><br/>miseledger.adapter.v1"]
        NORMALIZE["<b>Normalize records</b><br/>sources, collections, items, actors"]
        DEDUPE["<b>Deduplicate</b><br/>stable external IDs and raw refs"]
        INDEX["<b>Index evidence</b><br/>FTS5, relations, scan manifests"]
    end

    ADAPTER --> PARSE --> NORMALIZE --> DEDUPE --> INDEX

    ARCHIVE["<b>SQLite archive</b><br/>local evidence graph"]
    INDEX ==> ARCHIVE

    subgraph SURFACES [" reader surfaces "]
        SEARCH["<b>Search and show</b><br/>query, explain, SQL"]
        EXPORT["<b>Exports</b><br/>Markdown, evidence bundles"]
        API["<b>Local APIs</b><br/>HTTP loopback, stdio MCP"]
        MAINTAIN["<b>Archive care</b><br/>doctor, stats, compact, prune metadata"]
    end

    ARCHIVE --> SEARCH
    ARCHIVE --> EXPORT
    ARCHIVE --> API
    ARCHIVE --> MAINTAIN

    GUARD["<b>Evidence boundary</b><br/>imported text stays data, not instructions"]
    PARSE -. rejects malformed records .-> GUARD
    EXPORT -. marks untrusted context .-> GUARD

    classDef source fill:#eff6ff,stroke:#2563eb,color:#1e3a8a;
    classDef process fill:#ecfdf5,stroke:#059669,color:#064e3b;
    classDef archive fill:#2563eb,stroke:#1d4ed8,color:#fff;
    classDef surface fill:#f1f5f9,stroke:#94a3b8,color:#334155;
    classDef guard fill:#fff7ed,stroke:#ea580c,color:#7c2d12;
    class STATIONTRAIL,SOURCEHARVEST,NATIVE,ADAPTER source;
    class PARSE,NORMALIZE,DEDUPE,INDEX process;
    class ARCHIVE archive;
    class SEARCH,EXPORT,API,MAINTAIN surface;
    class GUARD guard;
```

MiseLedger follows one ingest path:

1. Receive `miseledger.adapter.v1` JSONL from a file, stdin, StationTrail, SourceHarvest, or a native compatibility adapter.
2. Parse and validate each adapter record.
3. Store normalized sources, collections, items, actors, artifacts, raw refs, tags, imports, warnings, and scan manifests in SQLite.
4. Deduplicate repeat records and preserve raw payload references for audit.
5. Maintain FTS5 search indexes and shallow relations.
6. Serve search, show, explain, export, HTTP, MCP, and evidence-bundle workflows from the local archive.

## Stack Map

```mermaid
flowchart TB
    MISELEDGER["<b>MiseLedger</b><br/><i>archive, search, evidence layer</i>"]
    SQLITE["<b>SQLite archive</b><br/>normalized records, FTS, relations, raw refs"]
    MISELEDGER -->|owns| SQLITE

    subgraph AGENTS [" agent-session scanners "]
        STATIONTRAIL["<b>StationTrail</b><br/>Codex, Claude, OpenClaw, OpenCode, Hermes"]
        COMPAT["<b>Compatibility adapters</b><br/>native MiseLedger imports"]
    end

    subgraph LOCAL [" local source exporters "]
        SOURCEHARVEST["<b>SourceHarvest</b><br/>Markdown, files, HTML, JSON, git history"]
        GENERIC["<b>Generic adapter records</b><br/>normalized source exports"]
    end

    subgraph CRAWLERS [" crawler tools "]
        DISCRAWL["discrawl"]
        GITCRAWL["gitcrawl"]
        GRAINCRAWL["graincrawl"]
        NOTCRAWL["notcrawl"]
        SLACRAWL["slacrawl"]
        TELECRAWL["telecrawl"]
    end

    STATIONTRAIL & COMPAT == adapter JSONL ==> MISELEDGER
    SOURCEHARVEST & GENERIC == adapter JSONL ==> MISELEDGER
    DISCRAWL & GITCRAWL & GRAINCRAWL & NOTCRAWL & SLACRAWL & TELECRAWL -. exports or snapshots .-> SOURCEHARVEST

    subgraph READERS [" reader workflows "]
        CLI["<b>miseledger CLI</b><br/>search, show, explain, export"]
        EVIDENCE["<b>Evidence bundles</b><br/>Brigade-ready context"]
        MCP["<b>Agent readers</b><br/>HTTP loopback, stdio MCP"]
        OPS["<b>Archive operations</b><br/>doctor, stats, compact"]
    end

    SQLITE --> CLI
    SQLITE --> EVIDENCE
    SQLITE --> MCP
    SQLITE --> OPS

    classDef core fill:#2563eb,stroke:#1d4ed8,color:#fff;
    classDef archive fill:#fff7ed,stroke:#ea580c,color:#7c2d12;
    classDef source fill:#eff6ff,stroke:#2563eb,color:#1e3a8a;
    classDef crawler fill:#ecfdf5,stroke:#059669,color:#064e3b;
    classDef reader fill:#f1f5f9,stroke:#94a3b8,color:#334155;
    class MISELEDGER core;
    class SQLITE archive;
    class STATIONTRAIL,COMPAT,SOURCEHARVEST,GENERIC source;
    class DISCRAWL,GITCRAWL,GRAINCRAWL,NOTCRAWL,SLACRAWL,TELECRAWL crawler;
    class CLI,EVIDENCE,MCP,OPS reader;
```

StationTrail owns local agent-session scanning. SourceHarvest owns non-agent local source export normalization. MiseLedger owns archive ingest, SQLite, FTS, relations, scan manifests, reader APIs, and evidence bundles.

Crawler tools keep their native sync/query behavior. Their local exports, snapshots, or databases should flow through SourceHarvest before entering MiseLedger.

## Build

```bash
go build -o bin/miseledger ./cmd/miseledger
go build -o bin/sessionfind ./cmd/sessionfind
```

You can also run commands with:

```bash
go run ./cmd/miseledger --help
```

First archive:

```bash
miseledger init
miseledger crawl sessions --json
miseledger crawl chatgpt-export ~/Downloads/chatgpt-export.zip --json
miseledger crawl claude-export ~/Downloads/claude-export.zip --json
miseledger crawl docs ./notes --json
miseledger crawl repo . --json
miseledger sessions search "release audit" --source codex --json
miseledger search "auth timeout" --json
miseledger evidence "auth timeout" --markdown
```

Install from a release:

```bash
curl -fsSL https://raw.githubusercontent.com/escoffier-labs/miseledger/HEAD/install.sh | sh
```

For a first archive and agent integration path, see [docs/QUICKSTART.md](docs/QUICKSTART.md). For MCP client configuration, see [docs/MCP.md](docs/MCP.md). For roadmap and cookbook material, see [docs/ROADMAP.md](docs/ROADMAP.md), [docs/EXAMPLES.md](docs/EXAMPLES.md), [docs/QUERY_COOKBOOK.md](docs/QUERY_COOKBOOK.md), [docs/STATIONTRAIL_PARITY.md](docs/STATIONTRAIL_PARITY.md), [docs/LIVE_DRY_RUN_CHECKLIST.md](docs/LIVE_DRY_RUN_CHECKLIST.md), and [docs/INSTALL_SMOKE.md](docs/INSTALL_SMOKE.md).

## Runtime Paths

MiseLedger uses XDG paths when present:

- config: `~/.config/miseledger/config.toml`
- data: `~/.local/share/miseledger/miseledger.db`
- cache: `~/.cache/miseledger/`

Directories and files created by the CLI use private permissions.

## Smoke Test

```bash
miseledger init
miseledger import adapter testdata/adapters/discrawl.fixture.jsonl --source discrawl
miseledger import adapter testdata/adapters/agent-session.fixture.jsonl --source codex
miseledger adapter codex testdata/harnesses/codex-session.fixture.jsonl --out -
miseledger adapter hermes testdata/harnesses/session_hermes-demo.fixture.json --out -
miseledger import codex testdata/harnesses/codex-session.fixture.jsonl --json
miseledger import openclaw testdata/harnesses/openclaw-session.fixture.jsonl --json
miseledger import claude testdata/harnesses/claude-project.fixture.jsonl --json
miseledger import hermes testdata/harnesses/session_hermes-demo.fixture.json --json
miseledger status --json
miseledger scans list --json
miseledger sources discover --json
miseledger search "adapter contract" --json
miseledger evidence "adapter contract" --json
miseledger evidence show <bundle-id> --json
miseledger explain "adapter contract" --json
miseledger show <returned-item-id> --json
miseledger export markdown --out /tmp/miseledger-md
miseledger relations backfill --json
miseledger stats --json
miseledger compact --json
miseledger prune imports --before 2026-01-01 --dry-run --json
miseledger sql "select count(*) as items from items" --json
miseledger doctor --json
miseledger doctor --mcp --json
miseledger doctor --archive --json
```

Re-running the same imports is idempotent and does not increase item counts.

## Native Session Adapters

Native adapter generators convert local session JSON and JSONL into `miseledger.adapter.v1` records:

```bash
miseledger adapter codex ~/.codex/sessions --out codex.adapter.jsonl --limit 100
miseledger adapter openclaw ~/.openclaw/agents --out openclaw.adapter.jsonl --since 2026-06-01
miseledger adapter claude ~/.claude/projects --out claude.adapter.jsonl --limit 100
miseledger adapter hermes ~/.hermes/sessions --out hermes.adapter.jsonl --limit 100
miseledger adapter cursor ~/.config/cursor --out cursor.adapter.jsonl
```

Native import commands stream generated adapter records into the same adapter ingest path:

```bash
miseledger import codex ~/.codex/sessions --json
miseledger import openclaw ~/.openclaw/agents --json
miseledger import claude ~/.claude/projects --json
miseledger import hermes ~/.hermes/sessions --json
miseledger import cursor ~/.config/cursor --json
miseledger import codex testdata/harnesses/malformed-unknown.fixture.jsonl --dry-run --json
miseledger import discovered --json
miseledger watch once --json
miseledger watch once --if-changed --json
```

For the normal user-facing archive-crawler lane, prefer:

```bash
miseledger crawl sessions --json
```

That imports discovered local session roots through the same idempotent archive path.

## Provider Exports

MiseLedger can also import official AI chat exports directly from a `.zip`, a directory containing `conversations.json`, or the JSON file itself:

```bash
miseledger crawl chatgpt-export ~/Downloads/chatgpt-export.zip --json
miseledger crawl claude-export ~/Downloads/claude-export.zip --json
miseledger import chatgpt-export conversations.json --json
miseledger import claude-export conversations.json --json
```

These imports normalize provider conversations into local `conversation` collections with `message` items tagged as `ai-chat`.

## Cursor Agent

MiseLedger imports local Cursor Agent CLI history from its config root (`$XDG_CONFIG_HOME/cursor` or `~/.config/cursor` on Linux, `~/.cursor` on macOS):

```bash
miseledger crawl cursor --json            # defaults to the standard Cursor root
miseledger crawl cursor ~/.config/cursor --json
miseledger import cursor ~/.config/cursor --json
```

Two stable, plain-JSON surfaces are indexed: `prompt_history.json` (the prompts you typed, the primary "what did I ask?" lookup) and each chat's `meta.json` (title, timestamps, workspace), which becomes an `agent_session` collection so it appears in `miseledger sessions`. Cursor message bodies live in a per-chat binary `store.db` and are intentionally not decoded; a chat that has a `store.db` but no `meta.json` is still surfaced as a resumable session with a warning. `miseledger crawl sessions` and `miseledger import discovered` pick Cursor up automatically alongside the other native sources.

## Session Locator

For harness-style "find the session I was working in" workflows, use the session-level commands:

```bash
miseledger sessions list --source codex --json
miseledger sessions search "release audit" --source codex --json
miseledger sessions search "auth timeout" --source claude --json
sessionfind list --source codex --json
sessionfind "release audit" --source codex --json
```

`sessions search` groups matching item hits by session/conversation collection and returns the source kind, collection ID, item count, match count, sample item ID, raw source path, raw ordinal, and a snippet. It is meant for quickly finding the local harness session to inspect or resume.

The scanners accept a file or directory, walk relevant JSON and JSONL files recursively, skip obvious backups and sidecars, preserve raw refs, and warn rather than crash on malformed or unknown events. Hermes native support covers `session_*.json` snapshots and trajectory JSONL under `~/.hermes/sessions`; Hermes `state.db` is not parsed directly.

## External StationTrail Scanner

StationTrail is the separate local agent-session scanner/exporter. It keeps source-specific harness parsing outside MiseLedger and emits the same `miseledger.adapter.v1` JSONL contract:

```bash
stationtrail discover --json
stationtrail doctor --json
stationtrail doctor --live --json
stationtrail codex ~/.codex/sessions --dry-run --json
stationtrail all --out - --redact paths,secrets | miseledger import adapter -
stationtrail claude ~/.claude/projects --out - | miseledger import adapter -
stationtrail openclaw ~/.openclaw/agents --out openclaw.adapter.jsonl
stationtrail hermes ~/.hermes/sessions --out - | miseledger import adapter -
miseledger import adapter openclaw.adapter.jsonl --json
```

When `stationtrail` is installed on `PATH`, MiseLedger can run it directly:

```bash
miseledger import stationtrail codex ~/.codex/sessions --json
miseledger import stationtrail claude ~/.claude/projects --json
miseledger import stationtrail openclaw ~/.openclaw/agents --json
miseledger import stationtrail opencode opencode-session.json --json
miseledger import stationtrail hermes ~/.hermes/sessions --json
```

The wrapper streams StationTrail output through adapter ingest and records StationTrail scan manifests from its summary output. For mixed-source imports, use `stationtrail all --out - | miseledger import adapter -`; each adapter record still carries its own `source.kind`.

MiseLedger native adapters remain available for compatibility. Long term, source-specific agent-session parser ownership should live in StationTrail while MiseLedger owns archive ingest, SQLite, FTS, relations, scan manifests, and evidence bundles.

## External SourceHarvest Scanner

SourceHarvest is the separate local source-system exporter for non-harness records such as notes, generic JSONL exports, local crawler outputs, and future domain harvesters:

```bash
sourceharvest jsonl export.jsonl --source notes --collection notes:local --out - | miseledger import adapter -
sourceharvest markdown ./notes --source notes --collection notes:local --out - | miseledger import adapter -
```

When `sourceharvest` is installed on `PATH`, MiseLedger can run it directly:

```bash
miseledger import sourceharvest markdown ./notes --source notes --collection notes:local --json
miseledger import sourceharvest files ./notes --source notes --collection notes:files --glob "*.md,*.txt" --json
miseledger import sourceharvest html ./site-export --source docs --collection docs:html --json
miseledger import sourceharvest gitlog . --source gitlog --collection repo:miseledger --json
miseledger import sourceharvest json export.json --source export --collection export:records --records-path records --json
```

The user-facing crawl shortcuts add defaults for common local archive sources:

```bash
miseledger crawl docs ./notes --json
miseledger crawl files ./logs --glob "*.md,*.txt,*.log" --json
miseledger crawl repo . --json
miseledger crawl json export.json --records-path records --json
miseledger crawl adapter export.adapter.jsonl --source export --json
```

Use StationTrail for agent-session logs. Use SourceHarvest for other local source-system exports. MiseLedger remains the archive, search, relation, and evidence layer for both.

Planned crawler adapter imports should keep this shape once SourceHarvest has real schema-backed adapters:

```bash
miseledger import sourceharvest discrawl ~/.local/share/discrawl/discrawl.db --json
miseledger import sourceharvest telecrawl ~/.local/share/telecrawl/telecrawl.db --json
```

## Scan Manifests

Native imports record which local source files MiseLedger has seen without exposing transcript text:

```bash
miseledger scans list --json
miseledger scans list --source codex --json
miseledger scans show <id-or-path> --json
miseledger scans diff <path> --json
miseledger scans changed --source codex --json
```

Manifest rows include source kind, path, size, mtime, content hash, generated adapter hash, first/last seen timestamps, last imported timestamp, generated record count, and warning count.

## Archive Operations

Archive maintenance commands are local-only:

```bash
miseledger stats --json
miseledger relations backfill --json
miseledger compact --json
miseledger prune imports --before 2026-01-01 --dry-run --json
miseledger prune scans --missing --dry-run --json
miseledger doctor --archive --json
```

`stats` summarizes archive contents by source, item kind, actor type, collection kind, and recent imports. `relations backfill` resolves stored `target_external_id` values after later imports add the target item. `compact` checkpoints, analyzes, vacuums, and optimizes the SQLite archive. `prune imports` removes old import metadata and warning rows only. `prune scans --missing` removes scan manifest rows for files no longer present. Neither prune command deletes normalized evidence items.

`doctor --archive` checks SQLite quick-check status, foreign keys, orphan rows, unresolved relations, FTS coverage, and missing scan paths. It reports counts and status only, not transcript content.

## Source Discovery

Discovery reports candidate roots and supported file counts only:

```bash
miseledger sources discover --json
```

It checks Codex sessions, OpenClaw agents, Claude projects, and Hermes session files without printing private transcript content.

## Browser UI

`miseledger serve` also hosts a single-page browser UI at the server root, a search box for sessions and conversations when terminal Ctrl+F is not an option:

```bash
miseledger serve --addr 127.0.0.1:8765
# open http://127.0.0.1:8765/
```

It offers live full-text search in two modes (Sessions, which groups hits by session and shows the raw path plus a harness resume hint; and Everything, which searches all imported items), a source filter, and a detail pane. Press `/` or Ctrl+F to focus the search box. The page is served from the same loopback binding and talks only to the local `/search`, `/sessions`, `/items/`, and `/status` endpoints.

## Local API and MCP

The local HTTP API binds to loopback only by default:

```bash
miseledger serve --addr 127.0.0.1:8765
curl "http://127.0.0.1:8765/search?q=auth+timeout"
curl "http://127.0.0.1:8765/sessions?q=auth+timeout&source=cursor"
curl "http://127.0.0.1:8765/items/<item-id>"
curl -X POST http://127.0.0.1:8765/evidence -d '{"query":"auth timeout","limit":10}'
```

The stdio MCP server exposes `search_evidence`, `show_item`, `create_evidence_bundle`, `show_evidence_bundle`, and `list_sources`:

```bash
miseledger mcp
miseledger doctor --mcp --json
```

Fixture smoke scripts exercise these surfaces without private transcript content:

```bash
scripts/bootstrap_local.sh
scripts/smoke_http.sh
scripts/smoke_mcp.sh
```

## Evidence

Brigade-facing evidence bundles are structured and explicitly untrusted:

```bash
miseledger evidence "auth timeout" --source discrawl --limit 20 --json
miseledger evidence "Claude native import" --project miseledger --json
miseledger evidence "adapter contract" --include-related --json
miseledger evidence "adapter contract" --include-artifact-text --json
miseledger evidence "adapter contract" --markdown
miseledger evidence show <bundle-id> --json
miseledger evidence list --json
miseledger explain "adapter contract" --source codex --json
```

Evidence output includes a stable bundle `id`, a `miseledger://evidence/<id>` resource URI, the query, filters, generated timestamp, result item IDs, snippets, FTS scores, source and collection context, actor context, raw refs, artifact refs, source grouping, optional related items, optional artifact text, and warnings. Evidence results dedupe repeated content hashes. Generated bundles are cached under MiseLedger's private cache directory and can be shown later with `miseledger evidence show`.

`explain` uses the same FTS path as `search` and reports the quoted FTS query, filters, result count, source and item-kind counts, and top result IDs/snippets.

## Relations

MiseLedger resolves shallow relations during import when the target item already exists in the same source:

- Codex function/tool call results link back to calls by `call_id`.
- Claude `tool_result` records link back to `tool_use` records by `tool_use_id`.
- OpenClaw session/run events preserve `belongs_to_session` and `belongs_to_run` relations when session or run identifiers are present.

If a target is not present yet, MiseLedger preserves `target_external_id` for later inspection.

## Privacy

MiseLedger does not make network calls for init, adapter generation, import, search, evidence, show, export, status, SQL inspection, MCP, HTTP serving, or doctor. Imported text is stored locally and treated as untrusted evidence, not executable instructions.
