---
title: Archive Candidate Threshold
tags: [memory-care, archive, staleness]
description: Cards older than twice the stale TTL surface as archive candidates, never auto-deleted.
---

# Archive Candidate Threshold

`memory care status` lists archive candidates when `age_days > 2 * stale_after_days`. The view is approval-gated and read-only. Age anchors to the saved scan date, not "today" at status time.
