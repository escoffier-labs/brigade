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
| adapter_error | worker-results.json ok:false, missing/zero adapter exit, or a structured capacity-exhausted response | environmental — apparatus fault | terminal → retry-eligible (never when failure_reason=capacity-exhausted) | --retry-transient | 3 |
| grader_error | ≥1 grader failed to run (bad pattern, unreadable expected-file) | environmental — apparatus fault | terminal → regradeable | regrade only, no seat re-run | 3 |

Deterministic = a real statement about the seat. Environmental = a measurement-apparatus fault.
Consumers MUST NOT read an environmental state as seat performance.

## measurement_failures
summary.json reports measurement_failures (= adapter_error + grader_error + execution_error with
failure_reason=timeout) separately from rejected.

## failure_reason (additive)
Optional machine-readable string on the cell payload; eval_cell.v1 stays valid. Canonical
values: timeout (canonical transient), transport_drop / provider_5xx (adapter faults),
capacity-exhausted (provider quota or capacity stop, see below). Absent
when not applicable.

## Partial grader outcomes
A cell with ≥1 grader_error resolves to grader_error at the cell level (fail safe). summary.json
counts the other graders' real scores as partial, labeled as such — not silently folded into
the headline score stats.

## Process exit
0 = only accepted/unscored. 1 = ≥1 deterministic non-pass (rejected or non-timeout
execution_error), no measurement failures. 3 = any measurement failure; dominates 1 (matches
brigade run exit-3). 4 = the trial stopped before finishing every cell (≥1 `not_run` cell, or ≥1 cell
left `running` by a killed runner) and had no measurement failures. It dominates 1 and 0 so an incomplete trial never reads as a pass.

## Regrade
brigade model trial regrade <output-dir> re-runs graders against stored run/final.txt (verified
against output_digest) without re-running seats. Resume treats grader_error as regradeable.

## grader_result.v1: exit_code removed
The exit_code field (hardcoded 0/null) is REMOVED. If command-based graders arrive they add an
honestly-populated field under a new schema revision.

## Export privacy
cell.json inlines the full prompt and absolute run_dir. Any export/projection path MUST
strip/digest prompts and relativize paths by default. Enforced by test.

## Retry (--retry-transient)
`brigade model trial run|resume --retry-transient` is opt-in and off by default. It re-runs only
transient cells as new attempts: adapter_error, and execution_error with failure_reason=timeout.
Rejected, unscored, grader_error, and non-timeout execution_error cells are never retried.

On `run` a failing attempt is retried immediately. On `resume` a stored adapter_error or timeout
cell is re-run, which is the only way a terminal measurement failure is re-attempted. Each retry
is a new `attempts/attempt-NNN/` directory. The failed attempt stays on disk and is summarized in
the cell's additive `prior_attempts` list (attempt, state, failure_reason, exit_code,
duration_seconds, run_dir). The cell's own fields describe the latest attempt.

Caps, counted as retries after the first attempt per cell (`model_trials.RETRY_CAPS`):

| Latest failure | Max retries |
|---|---|
| adapter_error | 2 |
| execution_error with failure_reason=timeout | 1 |

A cell that reaches its cap stays in its failed state and keeps counting in
measurement_failures.

Retries have no backoff: a retry starts as soon as the failed attempt is recorded. A
provider_5xx or transport_drop that persists for more than a few seconds usually fails the retry
the same way and uses up the cap. Wait and use `resume --retry-transient` instead.

The cap also holds across a killed runner. A cell left `running` keeps its `prior_attempts`, and
the attempt that was in flight is added to them with `state: interrupted`, so it counts toward the
cap on resume. A `not_run` record written over a `running` cell keeps the same history.

## Drain (not_run)
A running trial can be told to stop launching cells without losing work in flight:

- `brigade model trial drain <output-dir>` writes `drain.json` into the artifact directory, or
- the first SIGINT or SIGTERM delivered to the runner process requests the same drain, whether
  it arrives between cells or while a cell runs. A terminal Ctrl-C and `kill -TERM <runner-pid>`
  both work. On POSIX each seat process starts in its own session (`start_new_session`), so the
  terminal's SIGINT reaches only the runner, never the seat, and the in-flight seat keeps running.
  A second signal hard-stops the process: a second SIGTERM during a cell records that run as
  `canceled` and exits 143, and a second Ctrl-C raises KeyboardInterrupt.

The runner checks before each launch and before each retry. The cell in flight finishes, is graded,
and is recorded normally. Every cell that has not started is written as `state: not_run` with
`not_run_reason: drained`. `not_run` is not one of the six terminal states and is not a failure:
it has no run_dir, no failure_reason, and no score, it is counted under its own key in summary.json,
and it is not part of measurement_failures. `resume` launches `not_run` cells (and cells left
`running` by a killed runner). A cell that already has a finished record is never overwritten with
`not_run`, and a retry that is due when the drain arrives is not started. A stale `drain.json` is
removed when the next `run` or `resume` starts.

## Quota and capacity stop (capacity-exhausted)
A structured provider quota, rate, or capacity response is the worker-failure class
`capacity-exhausted` from the [seat health and worker failure taxonomy](proposals/2026-07-26-seat-health-probe-and-worker-failure-taxonomy.md).
The trial runner recognizes it only from structured data in worker-results.json: a
`brigade.worker_failure.v1` payload of that class, or the legacy adapter kind `rate-limit-error`
when its `failure_phase` is not `output-validation`. Final-output validation assigns
`rate-limit-error` by matching the seat's answer text (for example `Error: rate limit exceeded`),
so that origin is graded as a normal model result: it is not `capacity-exhausted` and it does not
stop the seat. When a seat's attempt ends with a recognized capacity response:

- the cell is `adapter_error` with `failure_reason: capacity-exhausted`, even when the seat exited
  nonzero. It is a measurement failure (exit 3), not a deterministic `execution_error`, so it is
  not scored against the model.
- it is never retried: `--retry-transient` skips it, in the same run and on resume.
- the runner launches no further cells for that seat in this invocation. Each remaining cell of the
  seat is written as `not_run` with `not_run_reason: capacity-exhausted` and `stopped_by` set to the
  coordinate of the cell that hit the limit. Other seats keep running.

`resume` starts a fresh probe: it launches the seat's `not_run` cells (stopping again at the next
capacity response), while the original capacity-exhausted cell stays terminal.
