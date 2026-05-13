# HEARTBEAT.md

The heartbeat is a low-cost periodic check-in from the harness. Reply with the configured acknowledgment (often `HEARTBEAT_OK`) unless something needs immediate attention.

## Default behavior

- Reply with the ack token. No body, minimum tokens.
- Do not run health checks here (a dedicated nightly job handles that).
- Do not read memory files or do background work on heartbeats unless explicitly configured.

## When to break the ack-only rule

Heartbeat replies should stay quiet **except** when:

- Urgent inbound (message, calendar event imminent, alert).
- Something interesting surfaced since last contact.
- It has been too long since you last surfaced anything useful.

Even then, keep it short. One useful sentence beats a paragraph.

## Light periodic work (optional)

Some setups use the heartbeat to update a small state file (rate limits, queue depth, recent failures). If you wire this in, keep it cheap:

```bash
# example pattern: write a tiny state file from a cheap status command
session_status > ~/.<workspace>/data/rate-limits.json
```

Keep the work bounded and predictable. The heartbeat is a pulse, not a maintenance window.

## Heartbeat vs cron

- **Heartbeat** for batching loose periodic checks with conversational context (the user can reply mid-flow).
- **Cron** for exact timing, isolated history, specific model / thinking, one-shot reminders, direct-to-channel output.

## Exclude from heartbeats

- Private identifiers in public destinations.
- Raw logs unless requested.
- Speculative status. Only report what you verified.
