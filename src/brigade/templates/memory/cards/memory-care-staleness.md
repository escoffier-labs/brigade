---
topic: memory-care-staleness
category: foundation
tags: [memory, staleness, decay, refresh, maintenance]
---

# Memory Care Staleness

Memory needs care after it is written. Durable cards can become wrong when services move, workflows change, models are renamed, or project priorities expire. A staleness checker gives the memory owner a queue of cards that need refresh, without letting an agent rewrite sensitive or judgment-heavy knowledge blindly.

## Reference Loop

```text
memory/cards/*.md
        |
        v
card decay scanner
        |
        v
memory/cards/decay/scan-latest.json
memory/cards/decay/refresh-queue.json
        |
        v
safe refresh agent or manual review
```

## Scanner Output

Store scan state under `memory/cards/decay/`:

- `scan-latest.json` - latest full scan, counts, card statuses, decay ratios, and refresh queue size.
- `refresh-queue.json` - small queue of cards that are stale enough to review.

The scanner should report at least total cards, fresh count, aging count, stale count, critical count, and refresh queue size.

`brigade init --depth workspace` also installs `.brigade/memory-care.example.json` as a local wiring contract for whatever scanner you use. Adapt that file for your scheduler or memory owner, but keep the output paths stable unless you also update the doctor integration.

Minimal `scan-latest.json` shape:

```json
{
  "scan_date": "2026-05-26",
  "counts": {
    "total": 12,
    "fresh": 9,
    "aging": 2,
    "stale": 1,
    "critical": 0
  },
  "refresh_queue_size": 1
}
```

Minimal `refresh-queue.json` shape:

```json
{
  "cards": [
    {
      "file": "memory/cards/example.md",
      "reason": "source-of-truth changed"
    }
  ]
}
```

`brigade doctor` treats missing decay output as advisory, stale scans older than 7 days as a warning, and corrupt scan or queue JSON as a failure.

## Safe Refresh Rules

Only auto-refresh cards when current facts are grounded in local source-of-truth files read during the run. Good sources include `TOOLS.md`, `MEMORY.md`, recent `memory/YYYY-MM-DD.md`, local project docs, local scripts, and repo health reports.

Do not auto-refresh cards that require human judgment, personal context, career decisions, school work, business strategy, or outside research. Put those in a manual queue.

Never refresh by only bumping an `updated:` date. The content must change because a current source proves it should change.

## Suggested Schedule

- Daily scanner during quiet hours.
- Safe auto-refresh shortly after the scanner, capped to a small number of cards.
- Weekly deep report for manual review of sensitive or repeatedly skipped stale cards.

## Verification

```bash
test -f memory/cards/decay/scan-latest.json
test -f memory/cards/decay/refresh-queue.json
jq '.counts, .refresh_queue_size' memory/cards/decay/scan-latest.json
```

If the queue grows for several days, either the refresh agent is too conservative, the source-of-truth files are stale, or the cards need manual pruning.
