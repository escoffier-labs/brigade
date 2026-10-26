# Memory Handoff

## Type

bugfix

## Title

Publish only the selected live-control endpoint

## Summary

The app-server dispatch record advertised a Unix socket before control-server startup, even when the existing planner selected loopback TCP. Early polling consumers could retain that impossible endpoint. Preannounce the legacy path only for planned Unix transport; the complete TCP descriptor is published after binding.

## Durable facts

- This addresses only issue #1767 slice 4. Other performance slices remain outside this change, and the broader issue must stay open.
- Source changes are confined to `aboyeur/orchestrator.py`. Transport selection, authentication, serialization, cleanup, and CLI retry rules are unchanged. This does not add a CLI startup wait.
- Deterministic tests inspect the initial dispatch record inside the actual control server's pre-bind boundary, exercising an overlong path and missing AF_UNIX. They then verify the real bound TCP metadata and shutdown.
- The existing live-steering test unconditionally required Unix despite supported TCP fallback. After independent agreement, it now checks exact descriptor metadata and transport-specific legacy-path presence. All steering, interruption, worker-result, and cleanup assertions remain intact.
- Ownership was reported before edits. Old matching performance branches were confirmed merged through specific PR reads; broader ownership enumeration remains limited by unavailable reads. The claim is scoped to slice 4.

## Evidence

- Unchanged-source red receipt `20261026-172202-work-verify-f7dffe`: five failed. Both new startup regressions exposed the false Unix path; all three existing issue-linked CLI cases reproduced stale-endpoint timeouts. Earlier receipt `d34cff` stopped at formatting and is not behavioral evidence.
- Focused receipt `20261026-172301-work-verify-54ba39`: all 21 run-control tests passed, with lint, format, type, version, and snapshot gates passing.
- Full receipt `20261026-172345-work-verify-750d3a`: 47,387 passed, 138 failed, 209 skipped, zero errors, 733 passing subtests. This is not a full-suite pass.
- Base `1b7226f2166abb4909174a84057dc9bfbaebb606` has the exact reviewed #2080 tree `f5bf6805a20575175c32b3641086edbf49227bd9`. Preserved receipt `7a7917` supplies 143 exact-source baseline failure IDs. All 138 candidate failures belong to that set, with no candidate-only failures. The three targeted run-control failures now pass. The other two baseline-only cases are hook timing and a PID-file fixture race; no fixes for those are claimed.
- Raw evidence: `/workspace/scratch/astra-batch/logs/1767-*`; failed IDs: `/workspace/scratch/astra-batch/1767-failure-nodes.json`.
- No source or test changes followed the full run. Only this handoff and a reviewer-requested changelog wording correction were added afterward. The wording correction removes an inaccurate implication that CLI startup waiting was added.

## Recommended memory action

no-card

## Target document

.learnings/LEARNINGS.md

## Suggested document content

Publish startup metadata that agrees with the selected transport. An early legacy endpoint can remain cached by polling consumers even after a later record contains the correct descriptor. Preserve exact transport-specific assertions without assuming Unix when loopback TCP is supported.
