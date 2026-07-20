# StationTrail Parity Archive

As of MiseLedger v0.3.0, the StationTrail parity effort was archived. The local agent-session export paths that StationTrail proved out now live in MiseLedger's built-in `crawl sessions`, native `import`, and `adapter` commands.

StationTrail was a separate local harness exporter. Its adapter JSONL output remains compatible with `miseledger import adapter`, but new documentation should point users to MiseLedger's built-in surfaces.

## Support Matrix

| Source | Archived StationTrail parity | MiseLedger native | Recommended path |
| --- | --- | --- | --- |
| Codex sessions | covered | yes | Use `miseledger crawl sessions` or `miseledger import codex`. |
| Claude project logs | covered | yes | Use `miseledger crawl sessions` or `miseledger import claude`. |
| OpenClaw sessions and trajectories | covered | yes | Use `miseledger crawl sessions` or `miseledger import openclaw`. |
| OpenCode sessions | covered | yes | Use `miseledger crawl sessions` or `miseledger import opencode` with sanitized export JSON. |
| Hermes sessions | covered | yes | Use `miseledger crawl sessions` or `miseledger import hermes`. MiseLedger does not parse Hermes `state.db` directly. |
| Cursor sessions | partial historical coverage | yes | Use `miseledger crawl cursor` for the current conversation search database or a legacy Cursor Agent root. |
| Grok sessions | not covered | yes | Use `miseledger crawl sessions` or `miseledger import grok`. |
| Future harnesses | historical reference | sample-gated | Add native support only after redacted sample shapes exist. |

## Practical Split

The old StationTrail work covered:

- discover local harness roots
- inspect live source shapes without transcript content
- dry-run scanner coverage
- redact paths or secret-like values during export
- export harness logs as `miseledger.adapter.v1`

Use MiseLedger when the task is:

- import `miseledger.adapter.v1` JSONL
- track scan manifests
- search across crawlers, local source exports, and agent sessions
- show normalized items
- resolve relations
- create stable evidence bundles for Brigade or agents
- serve local HTTP or MCP
- run archive maintenance and doctor checks

## Commands

Built-in session crawl:

```bash
miseledger sources discover --json
miseledger crawl sessions --dry-run --json
miseledger crawl sessions --json
miseledger crawl cursor --json
```

MiseLedger compatibility imports:

```bash
miseledger import codex ~/.codex/sessions --json
miseledger import claude ~/.claude/projects --json
miseledger import openclaw ~/.openclaw/agents --json
miseledger import opencode ~/.local/share/opencode --json
miseledger import hermes ~/.hermes/sessions --json
miseledger import cursor ~/.config/Cursor/User --json
miseledger import grok ~/.grok/sessions --json
```

Archived StationTrail adapter JSONL:

```bash
miseledger import adapter stationtrail.adapter.jsonl --json
```

## Non-Goals

MiseLedger should not chase session browser parity, resume workflows, GUI features, or every harness parser. Native parsers should stay conservative and sample-gated.
