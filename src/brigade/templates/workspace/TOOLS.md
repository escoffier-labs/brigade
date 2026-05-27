# TOOLS.md - Local Notes

The local runbook. Commands, services, ports, scripts. No secrets.

> Rule: if you built an API for it, use the API. Do not re-derive what a local service already answers.

## Code Search (use this first)

If you have a local code-search service, hit it before grepping or spawning a coder subagent.

```bash
curl -s -X POST http://<host>:<port>/api/search \
  -H "Content-Type: application/json" \
  -d '{"query": "<your query>", "mode": "hybrid"}'
```

Replace `<host>:<port>` with your actual service. Common patterns: a local Ollama embedding model + SQLite FTS, or a hosted code-intel service.

## Memory Handoff Ingest

```bash
brigade ingest --target . --dry-run                          # preview
brigade ingest --target . --promote-cards --route-documents  # apply
```

Wrap in a cron or end-of-day script. See `memory/cards/memory-scanner.md`.

If you administer multiple agent homes, pull remote handoffs into staging directories on the canonical owner before ingesting. See `memory/cards/multi-workspace-handoff-admin.md`.

## Memory Care

```bash
test -f memory/cards/decay/scan-latest.json
jq '.counts, .refresh_queue_size' memory/cards/decay/scan-latest.json
```

Use a staleness scan once the card set is operationally important. See `memory/cards/memory-care-staleness.md`.

## TokenJuice Output Compaction

```bash
tokenjuice --version
tokenjuice stats
tokenjuice doctor hooks
tokenjuice wrap -- git status --short
tokenjuice wrap --raw -- git status --short
```

Use TokenJuice to compact noisy terminal output before it is fed back into the agent context. If exact raw output matters, use `tokenjuice wrap --raw -- <command>` or the harness's raw-output escape hatch.

Claude Code note: when the official PostToolUse adapter still relies on appended context instead of command replacement, use a trusted local PreToolUse wrapper that rewrites Bash commands to `tokenjuice wrap -- ...`. Document that wrapper in `CLAUDE.md` so agents treat the TokenJuice footer as local metadata, not prompt injection.

Codex note: use the normal hook integration and run `tokenjuice doctor hooks`. Some versions used `codex_hooks`; newer versions use `hooks`. Trust the doctor output over stale setup notes.

## Chat Surface Crawlers

If chat surfaces feed memory, list their commands here. Examples:

```bash
# Discord (discrawl)
discrawl sync           # full-history fetch
discrawl tail           # live event stream
discrawl search '<q>'   # local FTS

# Other surfaces follow the same shape (slackcrawl, tgcrawl, whatsappcrawl, mailcrawl)

# ClickClack
clickclack send --channel general "status"
clickclack messages list --channel general
clickclack export --out .brigade/chat-memory-sweeps/clickclack-export.json

# Sweep output
test -f .brigade/chat-memory-sweeps/latest.json
jq '.sessions, .issues' .brigade/chat-memory-sweeps/latest.json
brigade work import chat-sweep --input .brigade/chat-memory-sweeps/latest.json
brigade work import triage
```

See `memory/cards/chat-surface-crawlers.md` for the full pattern.

Stagger crawler repair, memory ingest, and updater cron jobs. Avoid placing memory ingest jobs inside known OpenClaw update windows.

## Publish Guard

```bash
brigade scrub --target . --dry-run               # repo policy
brigade scrub --target . --policy public-content # stricter, for blog/social
git push                                            # pre-push hook runs content-guard
```

Bypass `git push --no-verify` only if you have explicitly accepted the risk.

## Local Services

Replace the table with your actual local services. Use placeholders here for examples; real ports live in your private config.

| Service | Port | Purpose |
|---------|------|---------|
| `<code-search>` | `<port>` | semantic + grep search |
| `<prompt-library>` | `<port>` | reusable prompt store |
| `<agent-intel>` | `<port>` | research index |
| `<embedding-server>` | `<port>` | local embeddings for memory search |

## Hosts

Replace with your actual machines. Each entry: hostname, role, how to reach it.

| Host | Role | SSH |
|------|------|-----|
| `<workspace>` | canonical memory owner | `ssh <alias>` |
| `<homelab>` | LXC + services | `ssh <alias>` |
| `<workstation>` | personal daily driver | `ssh <alias>` |

## Common Checks

```bash
git status --short
git log --oneline -10
rg -n '<pattern>' .
jq '.' <file.json>
systemctl --user status <service>
journalctl --user -u <service> --since "-15min"
```

## Notes

- Keep this file current. Stale ports cause silent failures more than typos do.
- Do not store tokens, passwords, or OAuth material here. Use env files or a platform credential manager.
- Move runbook detail into `memory/cards/` when sections grow past one screen.
