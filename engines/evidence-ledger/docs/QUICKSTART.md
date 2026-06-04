# Quickstart

This path gets Logspine from a fresh install to a local evidence archive that agents can query.

## Install

Install Logspine:

```bash
curl -fsSL https://raw.githubusercontent.com/solomonneas/logspine/HEAD/install.sh | sh
spine version
```

Optional scanners:

```bash
curl -fsSL https://raw.githubusercontent.com/solomonneas/agenttrail/HEAD/install.sh | sh
curl -fsSL https://raw.githubusercontent.com/solomonneas/sourceharvest/HEAD/install.sh | sh
```

`agenttrail` exports local agent-session logs. `sourceharvest` exports local files, Markdown, HTML, JSON, JSONL, and git history. Logspine imports both through the same `logspine.adapter.v1` contract.

## Initialize

```bash
spine init
spine doctor --json
spine doctor --mcp --json
```

Logspine uses local XDG runtime paths and private permissions. The MCP doctor check validates protocol initialization and tool registration without reading transcript content.

## Import Agent Sessions

Native imports:

```bash
spine import codex ~/.codex/sessions --json
spine import openclaw ~/.openclaw/agents --json
spine import claude ~/.claude/projects --json
spine import hermes ~/.hermes/sessions --json
```

AgentTrail imports:

```bash
agenttrail all --out - --redact paths,secrets | spine import adapter - --json
spine import agenttrail codex ~/.codex/sessions --json
spine import agenttrail claude ~/.claude/projects --json
spine import agenttrail openclaw ~/.openclaw/agents --json
spine import agenttrail hermes ~/.hermes/sessions --json
```

Use `agenttrail all --out - | spine import adapter -` for mixed-source imports because each adapter record carries its own `source.kind`.

## Import Local Sources

SourceHarvest examples:

```bash
spine import sourceharvest markdown ./notes --source notes --collection notes:local --json
spine import sourceharvest files ./notes --source notes --collection notes:files --glob "*.md,*.txt" --json
spine import sourceharvest gitlog . --source gitlog --collection repo:logspine --json
spine import sourceharvest json export.json --source export --collection export:records --records-path records --json
```

Adapter JSONL examples:

```bash
spine import adapter discrawl.adapter.jsonl --source discrawl --json
sourceharvest jsonl export.jsonl --source notes --collection notes:local --out - | spine import adapter - --json
```

Re-running imports is idempotent. Growing files can be re-imported safely without duplicating existing items.

## Inspect Archive State

```bash
spine status --json
spine scans list --json
spine scans changed --json
spine sources discover --json
spine stats --json
spine relations backfill --json
spine compact --json
```

`sources discover` reports candidate roots, counts, and status only. It does not print transcript content.

## Search And Evidence

```bash
spine search "auth timeout" --json
spine show <item-id> --json
spine evidence "auth timeout" --project ops-deck --json
spine evidence "auth timeout" --include-related --json
spine evidence "auth timeout" --markdown
```

Evidence bundles include provenance, raw refs, source and collection context, actors, snippets, artifacts, warnings, and `untrusted_context: true`.

## Agent Access

Start the local stdio MCP server:

```bash
spine mcp
```

Validate the MCP surface:

```bash
spine doctor --mcp --json
scripts/smoke_mcp.sh
```

See [MCP.md](MCP.md) for configuration examples and tool details.
