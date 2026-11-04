# StationTrail Parity Archive

As of brigade-evidence v0.3.0, the StationTrail parity effort was archived. The local agent-session export paths that StationTrail proved out now live in brigade-evidence's built-in `crawl sessions`, native `import`, and `adapter` commands.

StationTrail was a separate local harness exporter. Its adapter JSONL output remains compatible with `brigade-evidence import adapter`, but new documentation should point users to brigade-evidence's built-in surfaces.

## Support Matrix

| Source | Archived StationTrail parity | brigade-evidence native | Recommended path |
| --- | --- | --- | --- |
| Codex sessions | covered | yes | Use `brigade-evidence crawl sessions` or `brigade-evidence import codex`. |
| Pi sessions | not covered | yes | Use `brigade-evidence crawl sessions` or `brigade-evidence import pi`. |
| Claude project logs | covered | yes | Use `brigade-evidence crawl sessions` or `brigade-evidence import claude`. |
| OpenClaw sessions and trajectories | covered | yes | Use `brigade-evidence crawl sessions` or `brigade-evidence import openclaw`. |
| OpenCode sessions | covered | yes | Use `brigade-evidence crawl sessions` or `brigade-evidence import opencode` with sanitized export JSON. |
| Hermes sessions | covered | yes | Use `brigade-evidence crawl sessions` or `brigade-evidence import hermes`. brigade-evidence does not parse Hermes `state.db` directly. |
| Cursor sessions | partial historical coverage | yes | Use `brigade-evidence crawl cursor` for the current conversation search database or a legacy Cursor Agent root. |
| Grok sessions | not covered | yes | Use `brigade-evidence crawl sessions` or `brigade-evidence import grok`. |
| Future harnesses | historical reference | sample-gated | Add native support only after redacted sample shapes exist. |

## Practical Split

The old StationTrail work covered:

- discover local harness roots
- inspect live source shapes without transcript content
- dry-run scanner coverage
- redact paths or secret-like values during export
- export harness logs as `miseledger.adapter.v1`

Use brigade-evidence when the task is:

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
brigade-evidence sources discover --json
brigade-evidence crawl sessions --dry-run --json
brigade-evidence crawl sessions --json
brigade-evidence crawl cursor --json
```

brigade-evidence compatibility imports:

```bash
brigade-evidence import codex ~/.codex/sessions --json
brigade-evidence import claude ~/.claude/projects --json
brigade-evidence import pi ~/.pi/agent/sessions --json
brigade-evidence import openclaw ~/.openclaw/agents --json
brigade-evidence import opencode ~/.local/share/opencode --json
brigade-evidence import hermes ~/.hermes/sessions --json
brigade-evidence import cursor ~/.config/Cursor/User --json
brigade-evidence import grok ~/.grok/sessions --json
```

Archived StationTrail adapter JSONL:

```bash
brigade-evidence import adapter stationtrail.adapter.jsonl --json
```

## Non-Goals

brigade-evidence should not chase session browser parity, resume workflows, GUI features, or every harness parser. Native parsers should stay conservative and sample-gated.
