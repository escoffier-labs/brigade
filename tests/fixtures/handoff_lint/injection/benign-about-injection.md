# Memory Handoff

## Type
decision

## Title
Add handoff injection heuristics for issue 477

## Summary
Document the decision to scan handoffs for prompt-injection payloads during content-guard lint.

## Recommended memory action
no-card

## Target document
.learnings/LEARNINGS.md

## Suggested document content
### Add handoff injection heuristics for issue 477

We added injection heuristics to `brigade handoff lint --content-guard` so instruction-shaped payloads are flagged with line numbers before ingest.
