---
title: Run Lock Wait Queue
tags: [run, lock, concurrency]
description: Waiters join a fair per-worktree queue instead of a fixed 600s timeout.
---

# Run Lock Wait Queue

`brigade run --wait` proceeds in arrival order. Stale holders and dead queue tickets are reclaimed. Bare `--wait` has no fixed ceiling; `--wait=SECONDS` bounds; `--wait=0` fails fast when a workspace default wait is set.
