# Quickstart

This path gets MiseLedger from a fresh install to a local evidence archive that agents can query.

## Install

Install MiseLedger:

```bash
MISELEDGER_VERSION=v0.5.0
curl -fsSLO https://raw.githubusercontent.com/escoffier-labs/miseledger/v0.5.0/install.sh
cat install.sh  # review before running
MISELEDGER_VERSION="$MISELEDGER_VERSION" sh install.sh
miseledger version
```

Review `install.sh` before running it. The installer downloads release binaries and verifies their sha256 checksums against the release `checksums.txt`.

Convenience alternative, mutable `HEAD` installer:

```bash
curl -fsSL https://raw.githubusercontent.com/escoffier-labs/miseledger/HEAD/install.sh | sh
```

Optional domain crawler binaries such as `discrawl`, `gitcrawl`, `slacrawl`, `graincrawl`, `notcrawl`, `mailcrawl`, and `telecrawl` can feed MiseLedger through `miseledger crawl <domain>`. Adapter-emitting crawlers can also produce files for `miseledger import adapter`. Current Gitcrawl releases are read through `gitcrawl sync` plus `gitcrawl threads --json`. Session logs, local files, Markdown, HTML, JSON, JSONL, and git history are covered by built-in crawl and import commands.

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
miseledger import opencode ~/.local/share/opencode --json
miseledger import hermes ~/.hermes/sessions --json
miseledger import cursor ~/.config/Cursor/User --json
miseledger import grok ~/.grok/sessions --json
```

Already-generated adapter JSONL from older exporter tools can still be imported with `miseledger import adapter`.

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

Local artifact crawls with explicit source names:

```bash
miseledger crawl docs ./notes --source notes --collection notes:local --json
miseledger crawl files ./notes --source notes --collection notes:files --glob "*.md,*.txt" --json
miseledger crawl gitlog . --source gitlog --collection repo:miseledger --json
miseledger crawl json export.json --source export --collection export:records --records-path records --json
```

Adapter JSONL examples:

```bash
miseledger import adapter discrawl.adapter.jsonl --json
miseledger crawl jsonl export.jsonl --source notes --collection notes:local --json
```

Re-running imports is idempotent. Growing files can be re-imported safely without duplicating existing items.

Leave `--source` off adapter imports when the file already carries a source kind (crawler exports do). Deduplication keys on the source kind, so overriding it forks a second copy of every record: a file imported with `--source discrawl` will never dedupe against the same messages ingested by `crawl discord`, which records them under the kind `discord`. If you must override, match the wrapper's kind (`discord`, `github`, `slack`, `granola`, `notion`, `gmail`, `telegram`), never the binary name.

## Already Running OpenClaw And The Crawlers

If you have an existing OpenClaw install and crawler archives, everything imports without re-crawling anything:

```bash
miseledger init
miseledger import openclaw ~/.openclaw/agents        # native, no exporter needed
miseledger crawl sessions                            # every harness with local logs
miseledger crawl discord                             # reads discrawl's existing archive
miseledger import adapter old-export.adapter.jsonl   # any adapter JSONL you exported before
```

Rules that keep re-ingestion clean when the same records can arrive by more than one path:

1. Pick one canonical path per source and keep using it. `crawl <provider>` pins the source kind; a historical adapter file from the same crawler carries the same kind, so the two paths deduplicate against each other. Items are content-addressed (source kind, collection, external id, normalized text), and re-runs skip known items.
2. Do not rename kinds with `--source` (see above). This is the one way to create duplicates from a single crawler.
3. Pick one redaction posture per source. The content hash is part of the item identity, so importing the same session once raw and once with `--redact paths,secrets` stores two near-identical items. Redact everywhere or nowhere for a given source.
4. Wiring Brigade next to MiseLedger adds no dedupe risk: Brigade's memory cards and handoffs are a separate store, and its evidence bundles only read from the archive.
5. `crawl discord|github|slack|granola|notion|gmail` needs a crawler build with the `export adapter` subcommand. `crawl telegram` uses `telecrawl --json messages`, including Telecrawl 0.1.0.

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
miseledger prune --policy default --dry-run --json
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
