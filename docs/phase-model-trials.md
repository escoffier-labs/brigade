# Model Trials

## Goal

Add a dependency-free, resumable model experiment surface that runs named
cases against roster seats and keeps execution, grading, and acceptance states
separate.

## Scope

- JSON manifests using `brigade.eval_manifest.v1`.
- Stable cell IDs from normalized case, seat, trial, grader, and schema data.
- An optional `execution.mode` set to `read-only` (the default) or
  `writable-worktree`. Writable cells each run in a fresh Brigade-created
  detached worktree that is removed after grading.
- `brigade model trial plan|run|resume|show|summary`.
- Existing direct-worker execution path for every cell.
- Exit-status, exact-output, regex-output, JSON-field, file-existence,
  diff-constraint, and verification-receipt graders.
- Append-only attempts with stale-cell reporting.
- Current-plan-only summaries with separate counts for stale stored cells.
- Raw values plus count, mean, median, min, max, and population standard
  deviation.

## Adversarial fixture packs

Shipped under `src/brigade/templates/evals/adversarial/` as
`brigade.eval_manifest.v1` manifests:

- `prompt-injection` (3 cases; first case encodes the handoff-lint content-guard regression)
- `untrusted-evidence` (2 cases)
- `unsafe-command` (2 cases)
- `failed-verification` (2 cases)

Run with `brigade model trial plan|run` against a manifest path and a roster whose
`worker` seat is a direct CLI worker.

## Verification

- [x] Prove stable identity and changed-condition invalidation.
- [x] Prove mechanical grader zero scores are distinct from grader errors.
- [x] Prove run and resume behavior with a mocked direct-worker boundary.
- [x] Prove summary state counts and statistics.
- [x] Run focused tests and `./scripts/verify` through Brigade.
