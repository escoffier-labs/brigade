# Memory Handoff

## Type

bugfix

## Title

Complete Windows home-path detection across mixed separators

## Summary

The Windows home-path detector recognized only backslashes. Forward separators could be missed or partially claimed by the earlier Unix-home detector, leaving the drive prefix or spaced components unredacted. Recognize forward slashes and raw or doubled backslashes independently at each boundary, and run the complete Windows detector before the generic home detector.

## Durable facts

- Existing quote, filename-extension, and trailing-prose boundaries remain unchanged. This does not define the ambiguous unquoted terminal-directory policy tracked in #1911.
- The regression matrix covers 1,080 quoted/unquoted separator combinations, complete redaction, public-policy blocking, and surrounding prose. Safe negatives cover alternate roots, invalid drive prefixes, and empty user components.
- The newly recognized Windows rule now governs complete drive-qualified paths; standalone Unix-home paths keep their detector. No engine, policy configuration, or baseline trust changes were made.
- Strict tracked auditing initially exposed five existing synthetic redaction fixtures/assertions in `tests/test_harness_conformance_probe.py`. Add only five line-specific Windows-rule allow comments; values and assertions remain unchanged. Independent Astra review checked each tag's line and following-line scope and found no unrelated content suppressed.
- Before those annotations, the same file scanned with unchanged base detector and fixture blobs, the public policy, and pinned baseline had zero residual blockers. This was a file-level comparison, not a full base audit.

## Evidence

- Before the fix, the expanded two existing test methods produced 720 failed and 360 passed subtests, receipt `20261026-154801-work-verify-279023`. An initial focused attempt then found two missing explicit zip strict arguments; these were fixed with `strict=True` before successful validation.
- Full guard focused gate: 21,286 tests and 1,600 subtests passed, receipt `20261026-154951-work-verify-580fda`.
- Refreshed rules/conformance focused gate: 154 tests and 1,532 subtests passed, receipt `20261026-155613-work-verify-170d5f`.
- Strict tracked audit after fixture annotations passed: 2,778 files, 682 nonblocking findings, zero blocking findings. The pinned baseline blob remains `20ef5be9d9b20d026b6ce54ca80b70d701c3d6aa`.
- Full gate receipt `20261026-155739-work-verify-e3f09b`: 47,369 passed, 142 failed, 209 skipped, zero errors, 1,805 subtests passed. All 142 failed test IDs exactly match the preserved baseline; there are no candidate-only or base-only failures. This is not a full-suite pass.
- Tested base `dbd52c57d49c2fbef417f86efabf5c103fe3028c` has the same source tree as audited `0ffde2a0e5e2a8b8e4cd8e7e0e3741eec384f51a`: `0440a35b93332f1c76d0b0ebc8e403871bae76ec`. No source or test changes followed the full gate; only this handoff was added and the unchanged changelog bullet moved into Unreleased Fixed to avoid an adjacent-entry conflict.
- The unbaselined whole-file scan of the conformance fixture still reports four pre-existing synthetic findings outside the changed lines; applying the unchanged pinned baseline removes all four. No unrelated allow tags were added. Other changed-file scans passed; introduced-history scanning validates the published additions.
- Raw logs: `/workspace/scratch/astra-batch/logs/1913-*`; exact failed test IDs: `/workspace/scratch/astra-batch/1913-failure-nodes.json`.

## Recommended memory action

no-card

## Target document

.learnings/LEARNINGS.md

## Suggested document content

More specific Windows path detection must precede the generic Unix-home rule so a partial interior match cannot suppress complete redaction. Expand separator coverage without silently choosing a new whitespace boundary policy. Preserve the pinned audit baseline and annotate only reviewed synthetic fixtures when stronger detection exposes them.
