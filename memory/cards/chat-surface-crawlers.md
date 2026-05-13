---
topic: chat-surface-crawlers
category: foundation
tags: [chat-archives, discord, slack, whatsapp, telegram, sqlite, ingest]
---

# Chat Surface Crawlers

If you let your agent operate across messaging surfaces (Discord, WhatsApp, Slack, Telegram, etc.), you need each surface mirrored into a queryable local store. Native search on those platforms is inconsistent, rate-limited, and disappears when the platform decides to. A local crawl gives you durable history and feeds the [memory-scanner](memory-scanner.md).

## Pattern

```text
chat platform        local mirror             searchable store
   (Discord,           (bot or auth-token        (SQLite + FTS5,
    Slack,              with read access)         or vector index)
    WhatsApp,
    Telegram,
    iMessage)
                              |
                              v
                   crawler tail/sync (live + periodic repair)
                              |
                              v
              memory scanner reads recent archive ranges
                              |
                              v
              .claude/memory-handoffs/ or direct card writes
```

Each crawler is platform-specific (auth differs, intents differ, rate limits differ), but they share the same shape:

- **Live tail.** Long-running process that receives new messages as they arrive.
- **Periodic full sync.** Repair pass that catches anything the live tail missed.
- **Local SQLite store.** Messages, threads, members, mentions, attachments.
- **FTS5 search index** (or equivalent) so the scanner can query without ranking on the platform's API.
- **Read-only export.** Other machines or readers consume a snapshot; only the canonical host runs live sync.

## Surfaces and tools

| Platform | Crawler (suggested name) | Status |
|----------|--------------------------|--------|
| Discord | [`discrawl`](https://github.com/solomonneas/discrawl) | Available. Bot-token, SQLite + FTS5, git-snapshot read mode. |
| Slack | `slackcrawl` | Pattern-name. Bot-token + RTM events; same SQLite shape. |
| WhatsApp | `whatsappcrawl` | Pattern-name. Likely via WhatsApp Business API or Multi-Device session bridge. |
| Telegram | `tgcrawl` | Pattern-name. Bot API + MTProto for history backfill. |
| iMessage | `imescrawl` | Pattern-name. macOS-only; reads from local `chat.db`. |
| Signal | `signalcrawl` | Pattern-name. Signal-cli session export. |
| Email (Gmail/IMAP) | `mailcrawl` | Pattern-name. IMAP IDLE for tail, full-folder sync for repair. |

The names above follow the `<platform>crawl` convention used by `discrawl`. Swap them for whatever tool you actually use - the contract matters, not the binary name.

## Discrawl reference (the tested one)

`discrawl` is the canonical implementation:

- mirrors Discord guilds into SQLite
- FTS5 search across all archived content
- offline member directory from archived profile payloads
- structured mention/role/attachment indexing
- Gateway event tail for live updates, periodic repair sync
- private git-backed snapshot publish for org-wide read access without bot credentials

Use it as the template for other surfaces. New crawlers should expose:

1. A `sync` verb that fetches history.
2. A `tail` verb that streams new messages.
3. A read-only SQL or HTTP query surface.
4. Read-only consumption that does not require platform credentials.

## How the memory scanner consumes archives

The [memory-scanner](memory-scanner.md) does not read raw transcripts. It queries the crawler's archive for recent ranges and asks the underlying model to extract durable facts.

Typical pattern:

```bash
# scanner pseudocode
since=$(date -d 'yesterday' -u +%Y-%m-%dT%H:%MZ)
discrawl query --since "$since" --channel "#decisions" --format json |
  memory-scan extract --to-handoff .claude/memory-handoffs/
```

The scanner writes one Memory Handoff per durable finding. The conservative ingester routes those handoffs into cards / runbooks / learnings the same way it routes anything else.

## Privacy boundary

Chat archives are *intimate*. The crawler's local SQLite often contains:

- DMs the agent should not summarize back to a group
- Private mentions of other people who never consented to AI processing
- Credentials, addresses, financial details that landed in a chat once

Rules:

- The scanner produces summaries, never quotes. Original messages stay in the crawl archive.
- Cards promoted from chat archives must not include third-party PII unless the user explicitly approved.
- Run `solo-mise scrub --policy public-content` over any export of crawl-derived content before publishing.
- The publish gate (`hooks/pre-push` + content-guard) catches accidental leaks at the repo boundary.

## Not connected to a chat surface?

That is fine. The memory scanner works with just daily session logs and `.learnings/*.md`. Crawlers are additive - they expand the surface from which durable facts get distilled.
