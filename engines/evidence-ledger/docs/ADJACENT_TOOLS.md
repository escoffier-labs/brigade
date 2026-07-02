# Adjacent Tools

MiseLedger should learn from adjacent tools without copying their product shape.

## Agent Sessions

Repository: https://github.com/jazzyalex/agent-sessions

Agent Sessions is adjacent, not a blocker and not a target to clone.

Agent Sessions is a macOS-first session browser and cockpit for many AI coding tools. Its public README describes a native Mac app for Codex, Claude, OpenCode, Cursor, GitHub Copilot CLI, Pi, Gemini CLI, Hermes, and OpenClaw histories. It focuses on browsing local session folders, transcript inspection, image browsing, saved-session recovery, resume commands, live Agent Cockpit behavior, rate or usage visibility, and macOS terminal integrations.

MiseLedger is different:

- MiseLedger is a portable CLI, server, and MCP-friendly normalized memory layer.
- MiseLedger spans crawler archives and agent sessions.
- MiseLedger's first product surface is the `miseledger` CLI and durable SQLite archive.
- MiseLedger's adapter boundary is `miseledger.adapter.v1` JSONL.
- MiseLedger normalizes source, collection, actor, item, event, artifact, and relation concepts across heterogeneous sources.
- MiseLedger is intended to become Brigade's evidence source and sink, where imported data is untrusted evidence rather than instructions.

Each source system is best at its native domain:

- Built-in `miseledger crawl sessions`: Codex, Claude, OpenClaw, OpenCode, Hermes, and Cursor local history
- Built-in `miseledger crawl docs/files/html/json/jsonl/gitlog`: local files, Markdown, HTML, JSON, JSONL, and git history
- `discrawl`: Discord messages
- `gitcrawl`: GitHub issues and pull requests
- `notcrawl`: Notion pages
- `slacrawl`, `graincrawl`, `mailcrawl`, and `telecrawl`: domain-specific archives that can emit adapter JSONL

## Boundary

Agent session scanning is in scope for MiseLedger through conservative native JSON/JSONL generators and `miseledger crawl sessions`.

The intended split is:

- MiseLedger owns built-in local harness parsing, local artifact crawling, adapter ingest, normalized SQLite storage, FTS, relations, scan manifests, search, show, and evidence bundles.
- External crawler binaries own their source-specific sync and query behavior, then hand MiseLedger adapter JSONL.

The earlier StationTrail and SourceHarvest repos were archived once their portable export paths were absorbed into MiseLedger. Existing adapter JSONL from those tools can still be imported.

Common paths:

```bash
miseledger crawl sessions --json
miseledger crawl docs ./notes --json
miseledger crawl gitlog . --json
miseledger import adapter discrawl.adapter.jsonl --source discrawl --json
```

The minimum proof remains:

1. Import a Discrawl-like crawler JSONL fixture.
2. Import a Codex/OpenClaw-like agent-session JSONL fixture.
3. Store both in the same normalized schema.
4. Search finds both.
5. Re-import does not duplicate counts.

## Non-goals

Do not turn MiseLedger into a worse Agent Sessions clone.

Do not build for this MVP:

- GUI or native macOS app behavior.
- Agent Cockpit live monitoring.
- terminal resume workflows.
- image-browser UI.
- usage-limit dashboards.
- perfect parsers for every harness.
- parity with Agent Sessions session browsing.
