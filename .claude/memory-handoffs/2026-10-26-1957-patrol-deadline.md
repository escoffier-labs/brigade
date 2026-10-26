# Memory Handoff

## Type

bugfix

## Title

Keep timed-out Hub inspections from delaying patrol process exit

## Summary

Issue #1957 returned an on-time patrol observation but left ThreadPoolExecutor workers registered for interpreter shutdown. A blocked claim inspection could therefore delay the one-shot CLI after its shared deadline. Bounded daemon workers now execute queued inspections through Futures without the executor's shutdown join.

## Durable facts

- The absolute deadline includes claim fetching and inspection. At most eight inspection workers start, and pending jobs are canceled after the wait budget expires. Recheck immediately before transport after marking the Future running, so a scheduler delay during dequeue cannot start an expired request.
- Successful completed rows remain available when another inspection is slow or fails. In-flight read-only calls retain their transport timeout but cannot hold the CLI process open.
- A public CLI subprocess regression checks actual interpreter exit. Timing only the return from `_snapshot` would miss the original defect.
- This change was rebased onto main `18a75556548159c407558798af4df06b41245b31` after PR #2078 merged. Its own diff contains only the deadline fix, tests, changelog bullet, and this handoff.

## Evidence

- files changed: `src/brigade/runs_patrol_hub.py`, `tests/test_runs_patrol_reconciliation.py`, `CHANGELOG.md`
- original regression: `subprocess.TimeoutExpired` after five seconds despite a 0.05-second report budget; queued-work control passed.
- CodeRabbit identified a scheduling gap after Future transition; a deterministic clock-advance regression failed before the recheck (`['example-repo'] != []`) and passed after it. Independent Astra review found no actionable issues in the correction.
- Review-fix focused command: `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_runs_patrol_reconciliation.py","tests/test_runs_patrol.py","tests/test_claude_hooks_runtime.py::test_hook_run_process_exits_within_timeout_when_operation_hangs"]' --capture brigade-work`; receipt `20261026-120319-work-verify-213f49`, 137 passed.
- Restored-environment focused receipt `20261026-123001-work-verify-76b03c`: 239 passed, including patrol, both timing controls, npm feasibility, Obsidian adapter, and affected publish/ranking selectors.
- Final corrected-environment full receipt `20261026-123458-work-verify-a42b85`: 47,368 passed, 142 failed, 209 skipped, zero errors. All 141 failures reproduced on current main `18a75556` (receipt `20261026-123235-work-verify-83bc65`) remain; one additional hook process timing assertion measured 2.1785 seconds against a two-second limit. It passed serially on current main and the candidate; its full-load failure remains unclassified. There are no patrol failures. This is not a full-suite pass.
- The current-main reproduction selected the original 157 failed nodes plus two timing controls: 141 failed, 18 passed. Sixteen earlier care/memory/topology failures no longer reproduce in this run; do not label all original 157 as current baseline failures.
- standalone full receipt: `20261026-111633-work-verify-abbb5f`; `Aggregate: 47322 passed, 158 failed, 209 skipped, 0 errors`. This is not a full-suite pass.
- All 157 previously reproduced base failures remain. One additional hook timing assertion measured 2.465 seconds against a two-second limit under full load. It passed serially on untouched base (receipt `20261026-114135-work-verify-76598b`) and in the final focused candidate run; its full-load failure remains unclassified, not a proven baseline failure.
- Preliminary combined-tree gate `20261026-110015-work-verify-b06c37` was canceled before rebasing this independent change. It is excluded from final validation.
- Review-fix full receipt `20261026-120405-work-verify-2a00c9` had 47,302 passed, 199 failed, 218 skipped after an incorrectly replaced PATH hid installed Node. This environment-invalid run is retained as diagnostic evidence, not a baseline or a pass. Restoring inherited PATH with the checkout's own venv prepended fixed the affected focused selectors.

## Recommended memory action

create-card

## Target card

patrol-hub-deadline.md

## Suggested card content

```markdown
---
topic: patrol-hub-deadline
category: project-context
tags: [patrol, concurrency, deadlines]
---

# Patrol Hub deadlines

A one-shot CLI deadline includes interpreter shutdown. ThreadPoolExecutor.shutdown(wait=False) does not prevent its running workers from being joined at process exit. Patrol uses bounded daemon inspection workers, one absolute fetch-and-inspect deadline, and Futures to preserve partial successful observations. Test this with a subprocess that must exit while a fake transport remains blocked; an in-process return-time assertion is insufficient.
```
