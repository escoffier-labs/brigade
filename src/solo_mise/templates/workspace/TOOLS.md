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
solo-mise ingest --target . --dry-run                          # preview
solo-mise ingest --target . --promote-cards --route-documents  # apply
```

Wrap in a cron or end-of-day script. See `memory/cards/memory-scanner.md`.

## Chat Surface Crawlers

If chat surfaces feed memory, list their commands here. Examples:

```bash
# Discord (discrawl)
discrawl sync           # full-history fetch
discrawl tail           # live event stream
discrawl search '<q>'   # local FTS

# Other surfaces follow the same shape (slackcrawl, tgcrawl, whatsappcrawl, mailcrawl)
```

See `memory/cards/chat-surface-crawlers.md` for the full pattern.

## Publish Guard

```bash
solo-mise scrub --target . --dry-run               # repo policy
solo-mise scrub --target . --policy public-content # stricter, for blog/social
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
