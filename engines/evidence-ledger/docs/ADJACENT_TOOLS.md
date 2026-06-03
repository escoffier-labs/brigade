# Adjacent Tools

Logspine should learn from adjacent tools without copying their product shape.

## Agent Sessions

Repository: https://github.com/jazzyalex/agent-sessions

Agent Sessions is adjacent, not a blocker and not a target to clone.

Agent Sessions is a macOS-first session browser and cockpit for many AI coding tools. Its public README describes a native Mac app for Codex, Claude, OpenCode, Cursor, GitHub Copilot CLI, Pi, Gemini CLI, Hermes, and OpenClaw histories. It focuses on browsing local session folders, transcript inspection, image browsing, saved-session recovery, resume commands, live Agent Cockpit behavior, rate or usage visibility, and macOS terminal integrations.

Logspine is different:

- Logspine is a portable CLI, server, and MCP-friendly normalized memory layer.
- Logspine spans crawler archives and agent sessions.
- Logspine's first product surface is the `spine` CLI and durable SQLite archive.
- Logspine's adapter boundary is `logspine.adapter.v1` JSONL.
- Logspine normalizes source, collection, actor, item, event, artifact, and relation concepts across heterogeneous sources.
- Logspine is intended to become Brigade's evidence source and sink, where imported data is untrusted evidence rather than instructions.

Each source system is best at its native domain:

- `discrawl`: Discord messages
- `gitcrawl`: GitHub issues and pull requests
- `notcrawl`: Notion pages
- `aicrawl`: chat exports
- agent session scanners: Codex, Claude, OpenClaw, and related logs

## Boundary

Agent session scanning is in scope for Logspine, but the MVP starts with generic normalized adapter fixtures and conservative native JSONL generators rather than perfect per-harness parsers.

AgentTrail is the sibling tool for portable local agent-session export. It scans agent harness logs such as Codex, Claude project logs, and OpenClaw sessions, then emits `logspine.adapter.v1` JSONL.

The intended split is:

- AgentTrail owns source-specific local harness parsing and privacy-conscious export.
- Logspine owns adapter ingest, normalized SQLite storage, FTS, relations, scan manifests, search, show, and evidence bundles.

Logspine may keep native adapters as compatibility wrappers, but it should not become the long-term home for every harness parser.

The minimum proof remains:

1. Import a Discrawl-like crawler JSONL fixture.
2. Import a Codex/OpenClaw-like agent-session JSONL fixture.
3. Store both in the same normalized schema.
4. Search finds both.
5. Re-import does not duplicate counts.

## Non-goals

Do not turn Logspine into a worse Agent Sessions clone.

Do not build for this MVP:

- GUI or native macOS app behavior.
- Agent Cockpit live monitoring.
- terminal resume workflows.
- image-browser UI.
- usage-limit dashboards.
- perfect parsers for every harness.
- parity with Agent Sessions session browsing.
