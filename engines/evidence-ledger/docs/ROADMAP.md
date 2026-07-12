# Roadmap

MiseLedger is usable now as a local archive, search, and evidence layer for normalized source records.

## Usable Now

- Import `miseledger.adapter.v1` JSONL from source-specific exporters.
- Import native Codex, OpenClaw, Claude, OpenCode, Hermes, and Cursor session fixtures and local logs.
- Crawl local agent sessions with `miseledger crawl sessions`.
- Crawl Markdown, files, HTML, JSON, JSONL, and git history with built-in local artifact crawlers.
- Search one SQLite archive across crawler records, local source exports, and agent-session logs.
- Produce evidence bundles with `untrusted_context: true`, raw refs, snippets, actors, collections, artifacts, and warnings.
- Cache evidence bundles with stable local `miseledger://evidence/<id>` references.
- Serve local loopback HTTP and stdio MCP surfaces for agent consumption.
- Track scan manifests so agents can see what source files MiseLedger has seen.
- Run archive doctor, stats, relation backfill, compact, and conservative metadata prune commands.
- Clear one-line diagnostics when wrapper import tools are missing, with `doctor` reporting wrapper tool availability (v0.5.0).
- Redacted fixtures for every supported harness, including Cursor (v0.5.0).
- Idempotent release publishing with checksums and build provenance attestations, plus a pinned checksum-first documented install path (v0.5.0).

## Easy To Recommend

The v0.5.0 hardening pass closed the previous items in this section (install
smoke, per-harness fixtures, missing-tool diagnostics). Next candidates before
recommending MiseLedger broadly:

- Extend the docs-drift CI check beyond MCP tools to the command table and README counts.
- Exercise provenance attestation end to end on the first post-v0.5.0 release and document the result.

## Later

- Optional read-only local API auth for multi-user hosts.
- More external crawler domain wrappers as real local export shapes appear.
- Direct Hermes `state.db` support only after real redacted samples and a stable schema need exist.
- Native support for any future harness only after observed samples exist.

## Non-Goals

- No GUI.
- No hosted service requirement.
- No macOS-only behavior.
- No network calls from archive, import, search, evidence, MCP, or HTTP commands.
- No parser parity chase with session-browser tools.
- No imported text treated as instructions.
