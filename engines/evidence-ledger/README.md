# Logspine

Logspine turns scattered AI work history into a local searchable evidence graph.

The MVP is a local-first CLI named `spine`. It imports `logspine.adapter.v1` JSONL records into SQLite, preserves raw payload references, searches with SQLite FTS5, shows normalized items, exports Markdown, emits Brigade-ready evidence bundles, and allows read-only SQL inspection.

Each source system is best at its native domain:

- `discrawl`: Discord messages
- `gitcrawl`: GitHub issues and pull requests
- `notcrawl`: Notion pages
- `agenttrail`: Codex, Claude, OpenClaw, OpenCode, Hermes, and related local session logs

Logspine is the normalized evidence layer above those systems, not a replacement for them.

## Build

```bash
go build -o bin/spine ./cmd/spine
```

You can also run commands with:

```bash
go run ./cmd/spine --help
```

Install from a release:

```bash
curl -fsSL https://raw.githubusercontent.com/solomonneas/logspine/master/install.sh | sh
```

## Runtime Paths

Logspine uses XDG paths when present:

- config: `~/.config/logspine/config.toml`
- data: `~/.local/share/logspine/logspine.db`
- cache: `~/.cache/logspine/`

Directories and files created by the CLI use private permissions.

## Smoke Test

```bash
spine init
spine import adapter testdata/adapters/discrawl.fixture.jsonl --source discrawl
spine import adapter testdata/adapters/agent-session.fixture.jsonl --source codex
spine adapter codex testdata/harnesses/codex-session.fixture.jsonl --out -
spine import codex testdata/harnesses/codex-session.fixture.jsonl --json
spine import openclaw testdata/harnesses/openclaw-session.fixture.jsonl --json
spine import claude testdata/harnesses/claude-project.fixture.jsonl --json
spine status --json
spine scans list --json
spine sources discover --json
spine search "adapter contract" --json
spine evidence "adapter contract" --json
spine show <returned-item-id> --json
spine export markdown --out /tmp/logspine-md
spine sql "select count(*) as items from items" --json
spine doctor --json
```

Re-running the same imports is idempotent and does not increase item counts.

## Native Session Adapters

Native adapter generators convert local session JSONL into `logspine.adapter.v1` records:

```bash
spine adapter codex ~/.codex/sessions --out codex.adapter.jsonl --limit 100
spine adapter openclaw ~/.openclaw/agents --out openclaw.adapter.jsonl --since 2026-06-01
spine adapter claude ~/.claude/projects --out claude.adapter.jsonl --limit 100
```

Native import commands stream generated adapter records into the same adapter ingest path:

```bash
spine import codex ~/.codex/sessions --json
spine import openclaw ~/.openclaw/agents --json
spine import claude ~/.claude/projects --json
spine import codex testdata/harnesses/malformed-unknown.fixture.jsonl --dry-run --json
spine import discovered --json
spine watch once --json
spine watch once --if-changed --json
```

The scanners accept a file or directory, walk JSONL files recursively, skip obvious backups and sidecars, preserve raw refs, and warn rather than crash on malformed or unknown events.

## External AgentTrail Scanner

AgentTrail is the separate local agent-session scanner/exporter. It keeps source-specific harness parsing outside Logspine and emits the same `logspine.adapter.v1` JSONL contract:

```bash
agenttrail discover --json
agenttrail doctor --json
agenttrail doctor --live --json
agenttrail codex ~/.codex/sessions --dry-run --json
agenttrail all --out - --redact paths,secrets | spine import adapter -
agenttrail claude ~/.claude/projects --out - | spine import adapter -
agenttrail openclaw ~/.openclaw/agents --out openclaw.adapter.jsonl
agenttrail hermes ~/.hermes/sessions --out - | spine import adapter -
spine import adapter openclaw.adapter.jsonl --json
```

When `agenttrail` is installed on `PATH`, Logspine can run it directly:

```bash
spine import agenttrail codex ~/.codex/sessions --json
spine import agenttrail claude ~/.claude/projects --json
spine import agenttrail openclaw ~/.openclaw/agents --json
spine import agenttrail opencode opencode-session.json --json
spine import agenttrail hermes ~/.hermes/sessions --json
```

The wrapper streams AgentTrail output through adapter ingest and records AgentTrail scan manifests from its summary output. For mixed-source imports, use `agenttrail all --out - | spine import adapter -`; each adapter record still carries its own `source.kind`.

Logspine native adapters remain available for compatibility. Long term, source-specific agent-session parser ownership should live in AgentTrail while Logspine owns archive ingest, SQLite, FTS, relations, scan manifests, and evidence bundles.

## External SourceHarvest Scanner

SourceHarvest is the separate local source-system exporter for non-harness records such as notes, generic JSONL exports, and future domain harvesters:

```bash
sourceharvest jsonl export.jsonl --source notes --collection notes:local --out - | spine import adapter -
sourceharvest markdown ./notes --source notes --collection notes:local --out - | spine import adapter -
```

When `sourceharvest` is installed on `PATH`, Logspine can run it directly:

```bash
spine import sourceharvest markdown ./notes --source notes --collection notes:local --json
spine import sourceharvest files ./notes --source notes --collection notes:files --glob "*.md,*.txt" --json
spine import sourceharvest html ./site-export --source docs --collection docs:html --json
spine import sourceharvest gitlog . --source gitlog --collection repo:logspine --json
spine import sourceharvest json export.json --source export --collection export:records --records-path records --json
```

Use AgentTrail for agent-session logs. Use SourceHarvest for other local source-system exports. Logspine remains the archive, search, relation, and evidence layer for both.

## Scan Manifests

Native imports record which local source files Logspine has seen without exposing transcript text:

```bash
spine scans list --json
spine scans list --source codex --json
spine scans show <id-or-path> --json
spine scans diff <path> --json
spine scans changed --source codex --json
```

Manifest rows include source kind, path, size, mtime, content hash, generated adapter hash, first/last seen timestamps, last imported timestamp, generated record count, and warning count.

## Source Discovery

Discovery reports candidate roots and JSONL counts only:

```bash
spine sources discover --json
```

It checks Codex sessions, OpenClaw agents, Claude projects, and Hermes session files without printing private transcript content.

## Local API and MCP

The local HTTP API binds to loopback only by default:

```bash
spine serve --addr 127.0.0.1:8765
curl "http://127.0.0.1:8765/search?q=auth+timeout"
curl "http://127.0.0.1:8765/items/<item-id>"
curl -X POST http://127.0.0.1:8765/evidence -d '{"query":"auth timeout","limit":10}'
```

The stdio MCP server exposes `search_evidence`, `show_item`, `create_evidence_bundle`, and `list_sources`:

```bash
spine mcp
```

Fixture smoke scripts exercise these surfaces without private transcript content:

```bash
scripts/smoke_http.sh
scripts/smoke_mcp.sh
```

## Evidence

Brigade-facing evidence bundles are structured and explicitly untrusted:

```bash
spine evidence "auth timeout" --source discrawl --limit 20 --json
spine evidence "Claude native import" --project logspine --json
spine evidence "adapter contract" --include-related --json
spine evidence "adapter contract" --include-artifact-text --json
spine evidence "adapter contract" --markdown
```

Evidence output includes the query, filters, generated timestamp, result item IDs, snippets, FTS scores, source and collection context, actor context, raw refs, artifact refs, source grouping, optional related items, optional artifact text, and warnings. Evidence results dedupe repeated content hashes.

## Relations

Logspine resolves shallow relations during import when the target item already exists in the same source:

- Codex function/tool call results link back to calls by `call_id`.
- Claude `tool_result` records link back to `tool_use` records by `tool_use_id`.
- OpenClaw session/run events preserve `belongs_to_session` and `belongs_to_run` relations when session or run identifiers are present.

If a target is not present yet, Logspine preserves `target_external_id` for later inspection.

## Privacy

Logspine does not make network calls for init, adapter generation, import, search, evidence, show, export, status, SQL inspection, MCP, HTTP serving, or doctor. Imported text is stored locally and treated as untrusted evidence, not executable instructions.
