# Memory Handoff

## Type

research

## Title

OpenCode Go routing after the five-model security matrix

## Summary

The screen covered 23 task/model cells across 9 OpenCode Go models and
confirmed GLM-5.2 plus MiniMax M3 in a normal Brigade run. Promote MiniMax M3
after 20 shadow runs, use GLM-5.2 for security review, and reserve Kimi K3 for
higher-cost cross-file canaries.

## Durable facts

- GLM-5.2 `high` found 5/5 findings, obeyed 3/3 contracts, and averaged
  13.586 seconds. Kimi K3 `max` matched 5/5 and 3/3 at 15.200 seconds with a
  higher catalog price.
- Kimi K2.7 Code, DeepSeek V4 Pro, and Qwen3.7 Max found all expected issues
  but obeyed 1/3, 0/3, and 0/3 strict output contracts.
- MiniMax M3 is the cheap primary candidate: 3/3 findings, 2/2 contracts, and
  a 6.419-second mean. It has no security cell.
- A normal Brigade run with GLM-5.2 orchestrating MiniMax M3 completed in
  14.620 seconds, returned `WIRING_OK`, and changed 0 files.
- Brigade 0.25.1 cannot enforce OpenCode read-only mode. Use disposable
  worktrees and reject changed-file receipts. `max_workers` applies per run, so
  a host-wide process limit needs an external dispatcher.

## Evidence

- files changed:
  `benchmarks/model-ratings-2026-07/`
- three-task receipt: `20260729-194155-work-verify-f0f8e3`, MiseLedger
  `dc8c9568260c63269750f666`
- multi-seat receipt: `20260729-201417-work-verify-f959da`, MiseLedger
  `fbb5c10d67e793cfb8050057`
- Brigade and OpenCode exposed no per-call token or cost values.

## Recommended memory action

update-card

## Target card

opencode-go-brigade-routing.md

## Suggested card content

```markdown
---
topic: OpenCode Go Brigade routing
category: tools
tags: [brigade, opencode, models, routing]
---

# OpenCode Go Brigade routing

### Recommended seats

After 20 shadow runs, use MiniMax M3 `thinking` for short read-only analysis.
Use GLM-5.2 `high` for security or final structured review. Use Kimi K3 `max`
only as a cross-file canary before Claude or GPT-5.6 handles final high-risk
review.

### Safety and fallback

Brigade 0.25.1 cannot enforce OpenCode read-only mode. Use disposable
worktrees and require zero changed files. `max_workers` applies per run, so a
host-wide limit needs an external dispatcher. Retry one transient provider or
short rate-limit failure, but not auth, entitlement, timeout, or invalid output.

### Evidence

The 2026-07-29 screen covered 23 cells across 9 models. Three-task evidence:
`dc8c9568260c63269750f666`. Earlier two-task evidence:
`ec4e976c7c7689a39f91ae97`. Multi-seat evidence:
`fbb5c10d67e793cfb8050057`.
```
