# Adjacent Tools

brigade-evidence should learn from adjacent tools without copying their product shape.

## Agent Sessions

Repository: https://github.com/jazzyalex/agent-sessions

Agent Sessions is adjacent, not a blocker and not a target to clone.

Agent Sessions is a macOS-first session browser and cockpit for many AI coding tools. Its public README describes a native Mac app for Codex, Claude, OpenCode, Cursor, GitHub Copilot CLI, Pi, Gemini CLI, Hermes, and OpenClaw histories. It focuses on browsing local session folders, transcript inspection, image browsing, saved-session recovery, resume commands, live Agent Cockpit behavior, rate or usage visibility, and macOS terminal integrations.

brigade-evidence is different:

- brigade-evidence is a portable CLI, server, and MCP-friendly normalized memory layer.
- brigade-evidence spans crawler archives and agent sessions.
- brigade-evidence's first product surface is the `brigade-evidence` CLI and durable SQLite archive.
- brigade-evidence's adapter boundary is `miseledger.adapter.v1` JSONL.
- brigade-evidence normalizes source, collection, actor, item, event, artifact, and relation concepts across heterogeneous sources.
- brigade-evidence is intended to become Brigade's evidence source and sink, where imported data is untrusted evidence rather than instructions.

Each source system is best at its native domain:

- Built-in `brigade-evidence crawl sessions`: Codex, Claude, Pi, OpenClaw, OpenCode, Hermes, Cursor, and Grok local history
- `brigade-evidence crawl docs/files/html/json/jsonl/gitlog`: wrappers around the external `sourceharvest` binary for local files, Markdown, HTML, JSON, JSONL, and git history
- `discrawl`: Discord messages
- `gitcrawl`: GitHub issues and pull requests
- `notcrawl`: Notion pages
- `slacrawl`, `graincrawl`, and `mailcrawl`: domain-specific archives that emit adapter JSONL
- `telecrawl`: Telegram archive queried through its public JSON message command, then converted by brigade-evidence

## Boundary

Agent session scanning is in scope for brigade-evidence through conservative native JSON/JSONL generators and `brigade-evidence crawl sessions`.

The intended split is:

- brigade-evidence owns built-in local harness parsing, local artifact crawl wrappers, adapter ingest, normalized SQLite storage, FTS, relations, scan manifests, search, show, and evidence bundles.
- External crawler binaries own their source-specific sync and query behavior, then hand brigade-evidence adapter JSONL.

The earlier StationTrail and SourceHarvest repos are archived. Native session parsers live in brigade-evidence, but local artifact crawl wrappers still execute `sourceharvest`. Existing adapter JSONL from those tools can still be imported.

Common paths:

```bash
brigade-evidence crawl sessions --json
brigade-evidence crawl docs ./notes --json
brigade-evidence crawl gitlog . --json
brigade-evidence crawl github --repo escoffier-labs/miseledger --json
brigade-evidence crawl telegram --chat "brigade-evidence" --json
brigade-evidence import adapter discrawl.adapter.jsonl --json
```

The minimum proof remains:

1. Import a Discrawl-like crawler JSONL fixture.
2. Import a Codex/OpenClaw-like agent-session JSONL fixture.
3. Store both in the same normalized schema.
4. Search finds both.
5. Re-import does not duplicate counts.

## Non-goals

Do not turn brigade-evidence into a worse Agent Sessions clone.

Do not build for this MVP:

- Native desktop app behavior.
- Agent Cockpit live monitoring.
- Launching terminals or resuming harness sessions automatically.
- image-browser UI.
- usage-limit dashboards.
- perfect parsers for every harness.
- parity with Agent Sessions session browsing.
