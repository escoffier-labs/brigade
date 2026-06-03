# Logspine

Logspine turns scattered AI work history into a local searchable evidence graph.

The MVP is a local-first CLI named `spine`. It imports `logspine.adapter.v1` JSONL records into SQLite, preserves raw payload references, searches with SQLite FTS5, shows normalized items, exports Markdown, emits Brigade-ready evidence bundles, and allows read-only SQL inspection.

Each source system is best at its native domain:

- `discrawl`: Discord messages
- `gitcrawl`: GitHub issues and pull requests
- `notcrawl`: Notion pages
- `aicrawl`: chat exports
- agent session scanners: Codex, Claude, OpenClaw, and related logs

Logspine is the normalized evidence layer above those systems, not a replacement for them.

## Build

```bash
go build -o bin/spine ./cmd/spine
```

You can also run commands with:

```bash
go run ./cmd/spine --help
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
spine status --json
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
```

Native import commands reuse the adapter contract internally:

```bash
spine import codex ~/.codex/sessions --json
spine import openclaw ~/.openclaw/agents --json
spine import codex testdata/harnesses/malformed-unknown.fixture.jsonl --dry-run --json
```

The scanners accept a file or directory, walk JSONL files recursively, skip obvious backups and sidecars, preserve raw refs, and warn rather than crash on malformed or unknown events.

## Evidence

Brigade-facing evidence bundles are structured and explicitly untrusted:

```bash
spine evidence "auth timeout" --source discrawl --limit 20 --json
spine evidence "adapter contract" --markdown
```

Evidence output includes the query, filters, generated timestamp, result item IDs, snippets, source and collection context, actor context, raw refs, artifact refs, and warnings.

## Privacy

Logspine does not make network calls for init, adapter generation, import, search, evidence, show, export, status, SQL inspection, or doctor. Imported text is stored locally and treated as untrusted evidence, not executable instructions.
