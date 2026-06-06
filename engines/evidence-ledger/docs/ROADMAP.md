# Roadmap

Logspine is usable now as a local archive, search, and evidence layer for normalized source records.

## Usable Now

- Import `logspine.adapter.v1` JSONL from source-specific exporters.
- Import native Codex, OpenClaw, Claude, and Hermes session fixtures and local logs.
- Import StationTrail exports for agent-session harnesses.
- Import SourceHarvest exports for Markdown, files, HTML, JSON, JSONL, and git history.
- Search one SQLite archive across crawler records, local source exports, and agent-session logs.
- Produce evidence bundles with `untrusted_context: true`, raw refs, snippets, actors, collections, artifacts, and warnings.
- Cache evidence bundles with stable local `logspine://evidence/<id>` references.
- Serve local loopback HTTP and stdio MCP surfaces for agent consumption.
- Track scan manifests so agents can see what source files Logspine has seen.
- Run archive doctor, stats, relation backfill, compact, and conservative metadata prune commands.

## Easy To Recommend

These are the next hardening steps before recommending Logspine broadly:

- Keep release install smoke checks passing for Logspine, StationTrail, and SourceHarvest.
- Add more real redacted fixture shapes for each supported harness.
- Add clearer diagnostics for missing external tools in wrapper imports.
- Define item-level retention policies for long-running local stores. Current prune commands only remove old import metadata and missing scan manifests.

## Later

- Optional read-only local API auth for multi-user hosts.
- More SourceHarvest domain exporters as real local export shapes appear.
- Direct Hermes `state.db` support only after real redacted samples and a stable schema need exist.
- Native support for any future harness only after observed samples exist.

## Non-Goals

- No GUI.
- No hosted service requirement.
- No macOS-only behavior.
- No network calls from archive, import, search, evidence, MCP, or HTTP commands.
- No parser parity chase with session-browser tools.
- No imported text treated as instructions.
