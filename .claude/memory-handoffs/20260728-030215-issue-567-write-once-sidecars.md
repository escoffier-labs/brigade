# Memory Handoff

## Type

bugfix

## Title

Write-once run sidecar revisions

## Summary

Resume and patch-reference updates previously replaced worker and synthesis sidecars in place. They now append private, immutable revisions while retaining the legacy files as current compatibility projections.

## Durable facts

- Initial emissions and every update append six-digit JSON revisions below `revisions/worker-results/` and `revisions/synthesis/`.
- A first update of a legacy run imports the compatibility projection as revision 000001 before appending the new revision.
- A failed compatibility projection write must not replace the old projection after immutable evidence has been written.
- Revision files use mode 0600. POSIX writes fsync each new directory entry, the revision file, and the leaf directory before updating the projection.
- Existing readers continue loading `worker-results.json` and `synthesis.json`; no receipt schema or dependency changed.

## Evidence

- files changed: `src/brigade/aboyeur.py`, `src/brigade/run_resume.py`, `tests/test_aboyeur.py`, `tests/test_run_resume.py`, `tests/test_runs_cmd.py`
- RED receipts: `20260728-030303-work-verify-9ae890`, `20260728-031100-work-verify-052751`, `20260728-031433-work-verify-ca2331`, `20260728-031552-work-verify-cf2e04`
- GREEN receipts: `20260728-031646-work-verify-d12faa`, `20260728-031656-work-verify-8bfb8d`, `20260728-031822-work-verify-4cb89a`, `20260728-031829-work-verify-d68b11`

## Recommended memory action

no-card

## Target document

.learnings/LEARNINGS.md

## Suggested document content

### Write-once run sidecar revisions

Worker and synthesis evidence is append-only under `revisions/<sidecar>/`, while the original sidecar paths remain current compatibility projections. On the first update of a legacy run, archive the exact old bytes before appending the updated document. Persist revision evidence before replacing the projection, and retain the new revision if projection replacement fails. On POSIX, directory-entry fsync ordering is part of the durability contract.
