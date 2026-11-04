# Quickstart

This path gets brigade-evidence from a fresh install to a local evidence archive that agents can query.

## Install

For the default Brigade-managed installation, install the exact-release engine set and run the brigade-evidence smoke:

```bash
brigade setup
brigade evidence doctor
```

`brigade setup` installs brigade-code-graph, `brigade-code-graph-mcp`, brigade-evidence, and SessionFind from a verified stable release manifest and runs its version smoke before recording the install. Beta CLI wheels reuse the latest verified stable engine manifest.

## One-release standalone compatibility

Use this only for an independent brigade-evidence installation. It is not the default path.

```bash
MISELEDGER_VERSION=v0.6.0
curl -fsSLO https://raw.githubusercontent.com/escoffier-labs/miseledger/v0.6.0/install.sh
cat install.sh  # review before running
MISELEDGER_VERSION="$MISELEDGER_VERSION" sh install.sh
miseledger version
```

The pinned standalone release installs the legacy `miseledger` executable, so commands on this path use that name. Run the commands below as `miseledger` instead of `brigade-evidence` on a standalone install.

Review `install.sh` before running it. The installer downloads release binaries and verifies their sha256 checksums against the release `checksums.txt`.

Mutable `HEAD` installer for independent development only, not the default path:

```bash
curl -fsSL https://raw.githubusercontent.com/escoffier-labs/miseledger/HEAD/install.sh | sh
```

Optional domain crawler binaries such as `discrawl`, `gitcrawl`, `slacrawl`, `graincrawl`, `notcrawl`, `mailcrawl`, and `telecrawl` can feed brigade-evidence through `brigade-evidence crawl <domain>`. Adapter-emitting crawlers can also produce files for `brigade-evidence import adapter`. Current Gitcrawl releases are read through `gitcrawl sync` plus `gitcrawl threads --json`. Session logs and provider chat exports have native crawl and import commands. Local file, Markdown, HTML, JSON, JSONL, and git-history crawl commands delegate to `sourceharvest` and require that binary on `PATH`.

## Initialize

```bash
brigade-evidence init
brigade-evidence doctor --json
brigade-evidence doctor --mcp --json
```

brigade-evidence uses local XDG runtime paths and private permissions. The MCP doctor check validates protocol initialization and tool registration without reading transcript content.

## Import Agent Sessions

User-facing archive crawl:

```bash
brigade-evidence crawl sessions --json
brigade-evidence crawl cursor --json
brigade-evidence crawl chatgpt-export ~/Downloads/chatgpt-export.zip --json
brigade-evidence crawl claude-export ~/Downloads/claude-export.zip --json
```

Native imports:

```bash
brigade-evidence import codex ~/.codex/sessions --json
brigade-evidence import openclaw ~/.openclaw/agents --json
brigade-evidence import claude ~/.claude/projects --json
brigade-evidence import pi ~/.pi/agent/sessions --json
brigade-evidence import opencode ~/.local/share/opencode --json
brigade-evidence import hermes ~/.hermes/sessions --json
brigade-evidence import cursor ~/.config/Cursor/User --json
brigade-evidence import grok ~/.grok/sessions --json
```

Already-generated adapter JSONL from older exporter tools can still be imported with `brigade-evidence import adapter`.

## Import Provider Chat Exports

brigade-evidence accepts official ChatGPT and Claude conversation exports as `.zip` files, directories containing `conversations.json`, or direct JSON files:

```bash
brigade-evidence crawl chatgpt-export ~/Downloads/chatgpt-export.zip --json
brigade-evidence crawl claude-export ~/Downloads/claude-export.zip --json
brigade-evidence import chatgpt-export conversations.json --json
brigade-evidence import claude-export conversations.json --json
```

Provider export imports are local-only and normalize conversations into searchable `ai-chat` message records.

## Find A Prior Session

Use `sessions` when the goal is to locate a resumable harness session rather than inspect individual evidence items:

```bash
brigade-evidence sessions list --source codex --json
brigade-evidence sessions list --project brigade-evidence --model gpt-5 --json
brigade-evidence sessions search "release audit" --project brigade-evidence --model gpt-5 --json
brigade-evidence sessions search "auth timeout" --source claude --model claude-sonnet --json
sessionfind list --source codex --json
sessionfind "release audit" --source codex --json
```

The project filter matches normalized project, workspace, workspace-directory, and working-directory metadata. The JSON `workspace` field prefers `workspace`, then `workspace_dir`, then `cwd`. An unmatched project value is omitted when a workspace-like value satisfied an active project filter. Session JSON also includes normalized model, harness, and source name fields when available.

## Import Local Sources

Local artifact crawl wrappers (all except `crawl adapter` require `sourceharvest` on `PATH`):

```bash
brigade-evidence crawl docs ./notes --json
brigade-evidence crawl files ./notes --glob "*.md,*.txt" --json
brigade-evidence crawl repo . --json
brigade-evidence crawl json export.json --records-path records --json
brigade-evidence crawl adapter export.adapter.jsonl --source export --json
```

Local artifact crawls with explicit source names:

```bash
brigade-evidence crawl docs ./notes --source notes --collection notes:local --json
brigade-evidence crawl files ./notes --source notes --collection notes:files --glob "*.md,*.txt" --json
brigade-evidence crawl gitlog . --source gitlog --collection repo:brigade-evidence --json
brigade-evidence crawl json export.json --source export --collection export:records --records-path records --json
```

Adapter JSONL examples:

```bash
brigade-evidence import adapter discrawl.adapter.jsonl --json
brigade-evidence crawl jsonl export.jsonl --source notes --collection notes:local --json
```

Re-running imports is idempotent. Growing files can be re-imported safely without duplicating existing items.

Leave `--source` off adapter imports when the file already carries a source kind (crawler exports do). Deduplication keys on the source kind, so overriding it forks a second copy of every record: a file imported with `--source discrawl` will never dedupe against the same messages ingested by `crawl discord`, which records them under the kind `discord`. If you must override, match the wrapper's kind (`discord`, `github`, `slack`, `granola`, `notion`, `gmail`, `telegram`), never the binary name.

## Already Running OpenClaw And The Crawlers

If you have an existing OpenClaw install and crawler archives, everything imports without re-crawling anything:

```bash
brigade-evidence init
brigade-evidence import openclaw ~/.openclaw/agents        # native, no exporter needed
brigade-evidence crawl sessions                            # every harness with local logs
brigade-evidence crawl discord                             # reads discrawl's existing archive
brigade-evidence import adapter old-export.adapter.jsonl   # any adapter JSONL you exported before
```

Rules that keep re-ingestion clean when the same records can arrive by more than one path:

1. Pick one canonical path per source and keep using it. `crawl <provider>` pins the source kind. A historical adapter file from the same crawler carries the same kind, so the two paths deduplicate against each other. Items are content-addressed (source kind, collection, external id, normalized text), and re-runs skip known items.
2. Do not rename kinds with `--source` (see above). This is the one way to create duplicates from a single crawler.
3. Pick one redaction posture per source. The content hash is part of the item identity, so importing the same session once raw and once with `--redact paths,secrets` stores two near-identical items. Redact everywhere or nowhere for a given source.
4. Wiring Brigade next to brigade-evidence adds no dedupe risk: Brigade's canonical memory cards and handoffs remain separate. `brigade evidence crawl memory <workspace>` writes a namespaced projection into brigade-evidence. Evidence retrieval reads that archive and caches bundles.
5. `crawl discord|slack|granola|notion` needs a crawler build with `export adapter`. Gmail uses `mailcrawl gmail export`, GitHub uses `gitcrawl sync` plus `gitcrawl threads --json`, and Telegram uses `telecrawl --json messages`.

## Schedule Repeated Crawls

Create a private config such as `~/.config/brigade/evidence/schedule.toml`:

```toml
interval = "15m"

[[jobs]]
name = "discord"
command = "crawl"
args = ["discord", "--limit", "100", "--json"]

[[jobs]]
name = "sessions"
command = "crawl"
args = ["sessions", "--json"]
```

Run the jobs once:

```bash
brigade-evidence schedule run ~/.config/brigade/evidence/schedule.toml --json
```

Or keep them running in the foreground for a service wrapper to supervise:

```bash
brigade-evidence schedule daemon ~/.config/brigade/evidence/schedule.toml --interval 15m --json
```

Jobs are not shell commands. `command` must be one of brigade-evidence's scheduled command allowlist: `crawl`, `import`, `watch`, `adapter`, `relations`, or `compact`.

## Inspect Archive State

```bash
brigade-evidence status --json
brigade-evidence scans list --json
brigade-evidence scans changed --json
brigade-evidence sources discover --json
brigade-evidence stats --json
brigade-evidence relations backfill --json
brigade-evidence compact --json
brigade-evidence doctor --archive --json
brigade-evidence prune --policy default --dry-run --json
brigade-evidence prune imports --before 2026-01-01 --dry-run --json
brigade-evidence prune scans --missing --dry-run --json
```

`sources discover` reports candidate roots, counts, and status only. It does not print transcript content.

## Search And Evidence

```bash
brigade-evidence search "auth timeout" --json
brigade-evidence show <item-id> --json
brigade-evidence evidence "auth timeout" --project ops-deck --json
brigade-evidence evidence "auth timeout" --include-related --json
brigade-evidence evidence "auth timeout" --markdown
brigade-evidence evidence show <bundle-id> --json
brigade-evidence explain "auth timeout" --project ops-deck --json
```

Evidence bundles include a stable bundle ID, `miseledger://evidence/<id>` URI, provenance, raw refs, source and collection context, actors, snippets, artifacts, warnings, and `untrusted_context: true`.

New imports are quarantined with pending injection status. Search and show suppress their bodies and snippets, and evidence bundles return only ineligible stubs. After inspecting an item, an operator can use `brigade evidence trust review miseledger:item:<item-id> --content-hash <current-content-digest> --mark-injection-clean`. Moving the trust label alone does not clear pending injection status. See [SCHEMA.md](SCHEMA.md#read-verification).

## Browser UI

```bash
brigade-evidence serve --addr 127.0.0.1:8765
# open http://127.0.0.1:8765/ and search; press / or Ctrl+F to focus the box
```

The served page is a loopback-only search box for sessions and conversations, with Sessions and Everything modes, a source filter, and a detail pane that shows the raw path and a harness resume hint.

## Agent Access

Start the local stdio MCP server:

```bash
brigade-evidence mcp
```

Validate the MCP surface:

```bash
brigade-evidence doctor --mcp --json
scripts/smoke_mcp.sh
```

See [MCP.md](MCP.md) for configuration examples and tool details.
