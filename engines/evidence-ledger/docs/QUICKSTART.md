# Quickstart

This path gets MiseLedger from a fresh install to a local evidence archive that agents can query.

## Install

Install MiseLedger:

```bash
curl -fsSL https://raw.githubusercontent.com/escoffier-labs/miseledger/HEAD/install.sh | sh
miseledger version
```

Optional scanners:

```bash
curl -fsSL https://raw.githubusercontent.com/escoffier-labs/stationtrail/HEAD/install.sh | sh
curl -fsSL https://raw.githubusercontent.com/escoffier-labs/sourceharvest/HEAD/install.sh | sh
```

`stationtrail` exports local agent-session logs. `sourceharvest` exports local files, Markdown, HTML, JSON, JSONL, and git history. MiseLedger imports both through the same `miseledger.adapter.v1` contract.

## Initialize

```bash
miseledger init
miseledger doctor --json
miseledger doctor --mcp --json
```

MiseLedger uses local XDG runtime paths and private permissions. The MCP doctor check validates protocol initialization and tool registration without reading transcript content.

## Import Agent Sessions

User-facing archive crawl:

```bash
miseledger crawl sessions --json
miseledger crawl cursor --json
miseledger crawl chatgpt-export ~/Downloads/chatgpt-export.zip --json
miseledger crawl claude-export ~/Downloads/claude-export.zip --json
```

Native imports:

```bash
miseledger import codex ~/.codex/sessions --json
miseledger import openclaw ~/.openclaw/agents --json
miseledger import claude ~/.claude/projects --json
miseledger import hermes ~/.hermes/sessions --json
miseledger import cursor ~/.config/cursor --json
```

StationTrail imports:

```bash
stationtrail all --out - --redact paths,secrets | miseledger import adapter - --json
miseledger import stationtrail codex ~/.codex/sessions --json
miseledger import stationtrail claude ~/.claude/projects --json
miseledger import stationtrail openclaw ~/.openclaw/agents --json
miseledger import stationtrail hermes ~/.hermes/sessions --json
```

Use `stationtrail all --out - | miseledger import adapter -` for mixed-source imports because each adapter record carries its own `source.kind`.

## Import Provider Chat Exports

MiseLedger accepts official ChatGPT and Claude conversation exports as `.zip` files, directories containing `conversations.json`, or direct JSON files:

```bash
miseledger crawl chatgpt-export ~/Downloads/chatgpt-export.zip --json
miseledger crawl claude-export ~/Downloads/claude-export.zip --json
miseledger import chatgpt-export conversations.json --json
miseledger import claude-export conversations.json --json
```

Provider export imports are local-only and normalize conversations into searchable `ai-chat` message records.

## Find A Prior Session

Use `sessions` when the goal is to locate a resumable harness session rather than inspect individual evidence items:

```bash
miseledger sessions list --source codex --json
miseledger sessions search "release audit" --source codex --json
miseledger sessions search "auth timeout" --source claude --json
sessionfind list --source codex --json
sessionfind "release audit" --source codex --json
```

The search output is grouped by session/conversation and includes raw source path, raw ordinal, sample item ID, match count, and snippet.

## Import Local Sources

User-facing local artifact crawls:

```bash
miseledger crawl docs ./notes --json
miseledger crawl files ./notes --glob "*.md,*.txt" --json
miseledger crawl repo . --json
miseledger crawl json export.json --records-path records --json
miseledger crawl adapter export.adapter.jsonl --source export --json
```

SourceHarvest examples:

```bash
miseledger import sourceharvest markdown ./notes --source notes --collection notes:local --json
miseledger import sourceharvest files ./notes --source notes --collection notes:files --glob "*.md,*.txt" --json
miseledger import sourceharvest gitlog . --source gitlog --collection repo:miseledger --json
miseledger import sourceharvest json export.json --source export --collection export:records --records-path records --json
```

Adapter JSONL examples:

```bash
miseledger import adapter discrawl.adapter.jsonl --source discrawl --json
sourceharvest jsonl export.jsonl --source notes --collection notes:local --out - | miseledger import adapter - --json
```

Re-running imports is idempotent. Growing files can be re-imported safely without duplicating existing items.

## Inspect Archive State

```bash
miseledger status --json
miseledger scans list --json
miseledger scans changed --json
miseledger sources discover --json
miseledger stats --json
miseledger relations backfill --json
miseledger compact --json
miseledger doctor --archive --json
miseledger prune imports --before 2026-01-01 --dry-run --json
miseledger prune scans --missing --dry-run --json
```

`sources discover` reports candidate roots, counts, and status only. It does not print transcript content.

## Search And Evidence

```bash
miseledger search "auth timeout" --json
miseledger show <item-id> --json
miseledger evidence "auth timeout" --project ops-deck --json
miseledger evidence "auth timeout" --include-related --json
miseledger evidence "auth timeout" --markdown
miseledger evidence show <bundle-id> --json
miseledger explain "auth timeout" --project ops-deck --json
```

Evidence bundles include a stable bundle ID, `miseledger://evidence/<id>` URI, provenance, raw refs, source and collection context, actors, snippets, artifacts, warnings, and `untrusted_context: true`.

## Browser UI

```bash
miseledger serve --addr 127.0.0.1:8765
# open http://127.0.0.1:8765/ and search; press / or Ctrl+F to focus the box
```

The served page is a loopback-only search box for sessions and conversations, with Sessions and Everything modes, a source filter, and a detail pane that shows the raw path and a harness resume hint.

## Agent Access

Start the local stdio MCP server:

```bash
miseledger mcp
```

Validate the MCP surface:

```bash
miseledger doctor --mcp --json
scripts/smoke_mcp.sh
```

See [MCP.md](MCP.md) for configuration examples and tool details.
