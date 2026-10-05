# Windows per-file pytest sweep

The `windows-pytest` CI job runs each discovered `tests/**/test_*.py` file in
its own process and uploads the logs and `summary.json`. It remains advisory
(`continue-on-error: true`). Required checks retain their existing policy.

## Failure accounting

An unallowlisted observed `failed` or per-file `timeout` result is a product
regression. Allowlisted failures remain visible in the results and summary.
Passing allowlisted files are reported as removal candidates. The driver
does not modify `tests/windows_pytest_allowlist.json`.

`unstarted`, `deadline-exceeded`, `launch-failure`, `console-interrupt`,
`driver-abort`, and `cleanup-unconfirmed` describe infrastructure or incomplete
coverage. These rows cannot be forgiven by the allowlist and do not enter the product
regression count. Any incomplete coverage makes the driver exit nonzero.
Setup errors and interruptions also exit nonzero, including when no file
measurements exist. A natural result from a finished worker is preserved
when the aggregate deadline expires. A natural test failure followed by a
cleanup error keeps its failure status and exit code. Its `cleanup_error` field
also makes it an infrastructure error, even when the failure is allowlisted.
The envelope separately flags aggregate budget exhaustion, which exits
nonzero even if every worker has already returned a measured result.

Pytest has sole ownership of each `logs/<test-name>.log` stream. Driver
diagnostics live in the result's `diagnostic` field. A synthetic row can name
a log that does not exist because its process never launched.

## CI and local budgets

The first workflow step captures a UTC wall-clock epoch into `GITHUB_ENV`
before checkout, Python setup, installation, and base-ref fetching. The
driver receives that value through `--job-started-at`, with these settings:

| Option | CI value | Purpose |
| --- | --- | --- |
| `--job-limit` | 3600 seconds | Job allowance from the captured epoch |
| `--startup-reserve` | 120 seconds | Conservative allowance for startup before the first step |
| `--finalize-reserve` | 300 seconds | Conservative allowance for cleanup, record writing, and artifact upload |
| `--job-timeout` | 3300 seconds | Local driver execution cap |
| Driver step timeout | 55 minutes | Backup for a stuck driver |

At `main()` entry the driver computes:

```text
remaining = job_limit - (entry_wall_clock - job_started_at)
            - startup_reserve - finalize_reserve
deadline = entry_monotonic + min(job_timeout, remaining)
```

Discovery, validation, and driver setup consume that same monotonic budget.
Wall-clock changes after entry do not extend it. A file's wait is capped at
the aggregate deadline as well as its per-file timeout. An exhausted budget
launches nothing and records discovered files as `unstarted`. Invalid,
nonfinite, negative, or future epochs and negative or nonfinite reserves are
configuration errors.

The first-step timestamp approximates job start. The startup reserve covers
the unmeasured interval, and the job's 60-minute timeout remains the outer
limit. These reserves are conservative allowances, not measured guarantees
of startup or artifact-upload duration.

For local use, omit `--job-started-at`. Then `--job-timeout` alone caps driver
execution from entry, including driver setup. The default per-file timeout
is 900 seconds and the default parallel worker count is six.

## Isolation phase

CI explicitly selects `test_runs_serve.py` and `test_aboyeur.py` with repeated
`--serial` arguments. The driver validates portable names, discovery
membership, and duplicates before launching any child. Nested names such as
`nested/test_example.py` are supported.

Selected files run first, one at a time, with no parallel workers active.
The bounded parallel phase starts after the isolated phase has drained.
If the budget expires during either phase, remaining files are incomplete.
This scheduling guarantees isolation from other file workers. It has not
been shown to fix flakiness or improve duration for these two files.

## Record format

`brigade.windows_pytest.v2` preserves the existing `results` fields (`name`,
`status`, `returncode`, `seconds`, `log`) and adds optional `diagnostic` and `cleanup_error` text.
The envelope contains:

| Field | Meaning |
| --- | --- |
| `status` | `running`, `complete`, `incomplete`, `interrupted`, or `error` |
| `expected_files` | Discovered names, or `null` before discovery completes |
| `expected_count` | Number of discovered files, or `null` when unknown |
| `completed_count` | Observed passes, failures, and per-file timeouts |
| `accounted_count` | All result rows, including synthetic incomplete rows |
| `aggregate_deadline_exceeded` | Aggregate budget exhausted, independently of preserved measurements |
| `driver_error` | Present on setup, recording, cleanup, or driver errors |
| `retained_temp_root` | Present when a temporary target remains for inspection |

A `complete` sweep has a completed measurement for every expected file and
finishes within its aggregate budget.
It can still contain product regressions and exit nonzero. Accounted rows
alone cannot prove completion, and an unknown expected count cannot prove
zero tests complete. v1 records do not establish coverage completeness.

An initial `running` snapshot is written before launching children, followed
by incremental snapshots and a final status. Each write uses a same-directory
temporary file, flush, fsync, and atomic replacement. Windows permission
errors during replacement receive four attempts with short bounded delays.
Failed replacement preserves the previous valid JSON and removes the
temporary file. Abrupt driver death leaves the last `running` snapshot.

## Process containment and cleanup

Each test launches atomically into its own unnamed Windows Job Object through
`CreateProcessW`, `STARTUPINFOEXW`, and `PROC_THREAD_ATTRIBUTE_JOB_LIST`.
This requires Windows 10 or newer, or Windows Server 2016 or newer, as documented
by [Microsoft](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute).
The job has only the kill-on-close limit and is explicitly noninheritable.
The child receives an explicit handle list containing a temporary log handle
and NUL stdin. The job handle and unrelated inheritable handles are excluded.

One lifecycle mutex covers job registration, temporary handle creation,
attribute setup, process creation, publication, and temporary handle cleanup.
Shutdown takes the same mutex. It cannot terminate an empty job and then
allow a process to launch into it. The tracker alone owns job handles, and
workers own process handles. Job unregister and close are atomic and
idempotent. Waiting never holds the lifecycle mutex.

File timeout, aggregate deadline, and driver abort use distinct nonzero exit
codes. The first termination cause is immutable. A natural exit code keeps
its pass or failure meaning even when shutdown races its observation.
Normal root exit still terminates orphan descendants before returning.
`leaked_descendants=<count>` in a diagnostic records active descendants seen
after normal root exit. Abrupt driver death closes the last job handle in the
kernel, which triggers kill-on-close for descendants whose parent exited.

Aggregate shutdown initiates termination of every active job before waiting
for any job. Job emptiness, root waits, and worker finalization share one
15-second cleanup deadline. Every job closes once, even after query or
termination failures. Unconfirmed emptiness, failed kernel operations, or
unfinished workers are infrastructure errors and retain the temporary target.
The coordinator finalizes its record after bounded cleanup. Workers do not
publish records, so late worker completion cannot replace the final snapshot.

Launch errors fail closed with the Win32 error value. Unsupported job-list
APIs, incompatible ambient job restrictions, invalid handles, or executable
errors cannot trigger an uncontained fallback. Breakaway attempts are denied.
Tests requiring incompatible process limits need an explicit compatibility
disposition. The driver does not loosen containment or allowlist entries.

The existing required `windows-native-acceptance` job selects
`tests/test_windows_job.py::TestNativeContainment` and rejects any skip.
It exercises orphan cleanup, abrupt owner death, atomic assignment failure,
pre-execution descendant containment, venv redirectors, inheritance isolation,
breakaway denial, synchronized lifecycle races, and atomic records with real
Windows processes. Linux injected API tests verify lifecycle and error handling.
Linux skips provide no native Windows proof. Candidate Windows execution and
the full coverage gate remain separate checks.

## Disposition of the failure comments

The historical [CI run 37226637884](https://github.com/escoffier-labs/brigade/actions/runs/37226637884)
[result artifact 11314391762](https://github.com/escoffier-labs/brigade/actions/runs/37226637884/artifacts/11314391762) contained 485 rows:
313 passed, 171 failed, and one timed out. All nonpasses were in the existing
allowlist. `test_runs_serve.py` timed out after 900.125 seconds with an empty
log. The Grokbot assertion expected `invalid_request` and observed
`unavailable`. The work-graph concurrent-claim test had an assertion mismatch.
These historical observations do not establish native results for the current
implementation.

Different heads and changing failure sets do not establish randomness.
The evidence does not justify a `known_flaky` exemption, allowlist membership
changes, or promotion of this advisory sweep. Observed failures remain
visible. Grokbot feature changes are outside this change.
