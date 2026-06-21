# Harness Memory Contract

Any harness that wants to plug into `brigade` must answer six questions.

| Contract field | Meaning |
|----------------|---------|
| `bootstrap_files` | Files loaded into the agent's starting context |
| `memory_owner` | System responsible for canonical durable memory |
| `handoff_inbox` | Directory where side harnesses write handoff markdown |
| `routing_targets` | Allowed memory outputs: cards, tools, user prefs, rules, learnings |
| `doctor_checks` | Commands that prove the harness can see the expected files |
| `publish_gate` | Content-guard or equivalent scan before public output leaves the repo |

## Reference mapping (OpenClaw)

| Field | Value |
|-------|-------|
| `bootstrap_files` | `AGENTS.md`, `CLAUDE.md`, `SOUL.md`, `USER.md`, `TOOLS.md`, `MEMORY.md`, `IDENTITY.md`, `HEARTBEAT.md`, `SAFETY_RULES.md`, `INSTALL_FOR_AGENTS.md` |
| `memory_owner` | OpenClaw workspace at `~/.openclaw/workspace/` |
| `handoff_inbox` | `.claude/memory-handoffs/` in each repo |
| `routing_targets` | `memory/cards/*.md`, `TOOLS.md`, `USER.md`, `rules/*.md`, `.learnings/*.md` |
| `doctor_checks` | Existence of bootstrap files + jq queries against `openclaw.json` |
| `publish_gate` | `content-guard` via `hooks/pre-push` |

## Reference mapping (Hermes)

Same shape; map `memory_owner` to your Hermes config root and `doctor_checks` to whatever Hermes exposes.

## Reference mapping (generic / no orchestrator)

If you do not have an orchestrator yet, the contract still works:

| Field | Value |
|-------|-------|
| `memory_owner` | this repo until you wire one |
| `handoff_inbox` | `.claude/memory-handoffs/` |
| `routing_targets` | same |
| `doctor_checks` | `brigade doctor --target . --harness generic` |
| `publish_gate` | `brigade scrub` and `hooks/pre-push` |

`brigade ingest --target .` is enough on its own; you do not need OpenClaw or Hermes installed to run the loop.
