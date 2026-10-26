# Memory Handoff

## Type

bugfix

## Title

Retain hook artifact attribution across stale state writes

## Summary

A successful skill Read could record attribution while a Bash hook held an older state snapshot. The later full save erased the newer artifact or restored an obsolete specific artifact, and immediate guidance could also read a stale caller-local value. Merge the locked current ID/kind pair for same-task full saves, then apply any explicit routed-capture request. Synchronize the caller pair only after persistence succeeds.

## Durable facts

- Full snapshots are not artifact observations. Preserve the current specific artifact and its kind within the same task epoch; Read continues using its existing field-patch path.
- Forward the actual requested capture ID from routed verification. A boolean changed flag would miss deliberate same-ID/same-kind captures; forwarding the stale local pair would mishandle generic requests that left an older specific ID in the snapshot.
- Apply explicit intent through the existing specificity rule after merging current attribution, so generic fallback cannot replace the latest specific artifact. Deliberate specific changes remain effective.
- A capture request names an ID, not a replacement kind. Preserve the merged kind for a same-ID request, including whitespace normalization; a different ID retains the existing default skill kind.
- Existing latch and epoch guards remain intact. New-task resets do not inherit attribution, stale-task writes are rejected, and failed lock/write operations do not mutate caller attribution.
- Tests exercise real Read/Bash hook interleavings and then run a harmless tiny Python verifier through Brigade using the generated capture choice. They assert actual receipt outcome and harness-session attribution, rather than only a helper dictionary.

## Evidence

- Before the fix, receipt `20261026-163653-work-verify-5dd48b` produced six failures and five passes: missing/generic/stale-specific Read/Bash races, a generic-capture race, and two ID/kind preservation cases failed. The explicit same-ID control was subsequently strengthened to seed the same kind as well.
- Focused gate `20261026-163849-work-verify-ee184c`: 231 passed, covering the new race module and runtime, latch-race, and unseen-dependent suites. Independent Astra working-tree review found no actionable findings.
- Initial full gate `20261026-164204-work-verify-d290ef`: 47,393 passed, 141 failed, 209 skipped, zero errors, 733 passing subtests. This is historical validation before the review correction, not a full-suite pass.
- Base `1b7226f2166abb4909174a84057dc9bfbaebb606` exactly matches reviewed `fab138ee4fc8dbebc0f7ba5f7d0dfb3e55507e8d` tree `f5bf6805a20575175c32b3641086edbf49227bd9`; preserved full receipt `7a7917` supplies 143 exact-source baseline failure IDs. All 141 candidate failures are in that set, with no candidate-only failures. The known hook-timing and PID-file fixture failures passed this run; no fix for those tests is claimed.
- CodeRabbit reviewed initial head `b8ce40c7` and found that a same-ID routed capture reset a retained runbook kind to skill. Red receipt `20261026-174820-work-verify-509d49` reproduced three failures with fourteen passes, including normalized IDs, caller state, and the real routed hook path. The correction preserves the merged kind only for the same normalized ID; different-ID controls remain skill.
- Refreshed focused receipt `20261026-175024-work-verify-550581`: 237 passed. Receipt `f041ea` stopped at formatting before tests; formatting was corrected before this passing run.
- Final full gate `20261026-175143-work-verify-876c36`: 47,399 passed, 141 failed, 209 skipped, zero errors, 733 passing subtests. All 141 failures remain in the exact-base 143-failure set, with no candidate-only failures. The two unrelated timing cases passed this run; no fixes for them are claimed. The full gate remains failed.
- Source and tests were staged before the final full run and did not change afterward. Only this handoff was updated after verification. The changelog entry uses a separate existing Fixed location from the pending Windows-path PR.
- Raw evidence is preserved under `/workspace/scratch/astra-batch/logs/1924-*`; failed IDs are in `/workspace/scratch/astra-batch/1924-failure-nodes.json`.

## Recommended memory action

no-card

## Target document

.learnings/LEARNINGS.md

## Suggested document content

Preserving concurrent attribution requires both persisted state and the caller's post-save view to agree. Distinguish an explicit capture request from a stale full snapshot by carrying the requested value into the locked merge; preserve specificity, paired kind, task boundaries, and failed-save behavior.
