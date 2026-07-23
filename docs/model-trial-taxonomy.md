# Model-trial outcome taxonomy (brigade.eval_cell.v1 / grader_result.v1)

Status: frozen for the stable cut. Sibling: #434 (cell identity + resume). This document is the
contract every downstream consumer (scorecard, outcome capture, evidence export) keys on.

## The six terminal states

| State | Set when | Class | Resume | Retry | Exit bucket |
|---|---|---|---|---|---|
| accepted | exit 0, every grader at score_max | deterministic — real result | terminal | no | 0 |
| rejected | exit 0, ≥1 grader below score_max | deterministic — real result | terminal | no | 1 |
| unscored | exit 0, no graders defined | deterministic — coverage gap | terminal | no | 0 |
| execution_error | seat exited nonzero | deterministic — real result, unless failure_reason=timeout (then environmental) | terminal | only --retry-transient when failure_reason=timeout | 1 deterministic / 3 timeout |
| adapter_error | worker-results.json ok:false, missing/zero adapter exit | environmental — apparatus fault | terminal → retry-eligible | --retry-transient | 3 |
| grader_error | ≥1 grader failed to run (bad pattern, unreadable expected-file) | environmental — apparatus fault | terminal → regradeable | regrade only, no seat re-run | 3 |

Deterministic = a real statement about the seat. Environmental = a measurement-apparatus fault.
Consumers MUST NOT read an environmental state as seat performance.

## measurement_failures
summary.json reports measurement_failures (= adapter_error + grader_error + execution_error with
failure_reason=timeout) separately from rejected.

## failure_reason (additive)
Optional machine-readable string on the cell payload; eval_cell.v1 stays valid. Canonical
values: timeout (canonical transient), transport_drop / provider_5xx (adapter faults). Absent
when not applicable.

## Partial grader outcomes
A cell with ≥1 grader_error resolves to grader_error at the cell level (fail safe). summary.json
counts the other graders' real scores as partial, labeled as such — not silently folded into
the headline score stats.

## Process exit
0 = only accepted/unscored. 1 = ≥1 deterministic non-pass (rejected or non-timeout
execution_error), no measurement failures. 3 = any measurement failure; dominates 1 (matches
brigade run exit-3).

## Regrade
brigade model trials regrade <output-dir> re-runs graders against stored run/final.txt (verified
against output_digest) without re-running seats. Resume treats grader_error as regradeable.

## grader_result.v1: exit_code removed
The exit_code field (hardcoded 0/null) is REMOVED. If command-based graders arrive they add an
honestly-populated field under a new schema revision.

## Export privacy
cell.json inlines the full prompt and absolute run_dir. Any export/projection path MUST
strip/digest prompts and relativize paths by default. Enforced by test.

## Deferred (post-freeze, additive)
--retry-transient: opt-in re-run of adapter_error and timeout-reason cells as new attempts,
failed attempt kept on disk. Default off. Not required for the freeze.
