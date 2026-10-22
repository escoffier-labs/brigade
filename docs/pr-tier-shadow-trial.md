# PR test tier shadow trial

The `pr-tier-shadow` job observes test selection on pull requests for fourteen
days: **2026-10-23T00:00:00Z inclusive through 2026-11-06T00:00:00Z exclusive**.
Before activation, move both dates together if publication occurs later. Keep
the interval at fourteen days. Update the inline prefetch constants, runner
constants, boundary tests and this document together. This change does not
activate a scheduler or authorize a later rollout.

The job depends on the existing four Python 3.12 `test-shards` jobs and runs
with `always()` only on same-repository `pull_request` events. Before any step,
job admission requires nonempty repository identities and an exact match between
`github.event.pull_request.head.repo.full_name` and `github.repository`.
Fork PRs and events with missing repository identity skip the entire shadow job,
including prefetch, checkout, selected tests and upload. Ordinary required CI
still runs under its existing conditions. The shadow has read-only contents
and Actions permissions, does not persist checkout credentials, and uses
`continue-on-error: true`. No existing job depends on it. Required jobs and
`ci-gate` retain their existing definitions.

The first job step fetches full evidence using inline standard-library Python,
before checkout or any PR-controlled script, installation or tests. It runs
`python3 -I -S` to exclude Python environment paths and site initialization.
Only this step receives `GH_TOKEN`. No later step receives an API token. The
literal validates the repository, run ID and attempt from the environment and
checks the same UTC trial window before any API request. It also requires the
full shards' aggregate result to be success or failure. Requests have a
30-second timeout. A 240-second fetch deadline is checked before each request
and read. Pagination stops at 20 pages, metadata responses at 4 MiB and each
log response at 64 MiB. Log download
redirects use a fresh request without authorization headers and refuse further
redirects. Retrieval errors omit response bodies, URLs and exception messages.

The checked-out standard-library guard executes before Python setup or package installation.
Before the start and at or after expiry, the job records its status and uploads
evidence without installing dependencies, selecting files, running pytest, or
reading the Actions API. During the active window, setup, installation and test
work require the full shards' aggregate result to be success or failure.
Cancelled or skipped shards leave an incomplete report without starting that
work. Finalization and upload still use `always()`.
Cancellation can prevent even an `always()` upload.
A missing artifact is an evidence gap that requires an explanation.

Prefetch and the checked-out guard sample separate UTC clocks. If prefetch runs
just before the trial starts and the guard runs at or after the start, selected
tests can run with prefetch disabled. That observation remains incomplete.
Record it as a trial-start clock boundary gap.

## Selection

The checked-out SHA must equal the workflow's PR merge SHA. Its second parent
must equal the event's PR head. Selection diffs its first parent against that
merge, so changes already on the current base do not enter the PR diff.

`scripts/pr_test_tiers.json` contains the curated tiers from the supplied
659-row CSV: eighteen A files plus five invariant files, and 39 C files. Every
other test file, including new and unknown files, defaults to B. The five
invariants cover module size, mypy overrides, POSIX facades, phase 257 facades
and the verify skill. They address the two module-size misses in the supplied
200-PR offline replay, which caught 15 of 17 confirmed real failures before
this adjustment. The replay is prior analysis, not a measurement of this trial.

Selection starts with A, directly changed tests, and statically importing tests
outside C. It uses `scripts/windows_affected_tests.py` import matching and shared
path rules, without adding directory siblings. Changes to the tier manifest or
shadow scripts also count as shared infrastructure. A shared change or **more
than 250 initially selected files** falls back to every A+B file plus directly
changed C tests. Static imports never admit C on their own. Deleted tests are
absent. Selection records sorted inputs, reasons, tiers and the manifest hash.

## Retained evidence

Each run attempts to upload `pr-tier-shadow-<run-id>-<attempt>` for 30 days:

- `report.json` and `summary.md`: trial status, identity, comparison, gaps and
  timing. Finalization publishes one readable job summary after intermediate
  report updates have been retained.
- `identity.json`: tested merge SHA, current first-parent base, PR head,
  changed paths, run ID and attempt.
- `selection.json`, `selected.txt` and `pr_test_tiers.json`: reproducible
  selection, reasons and the exact curated membership used.
- `pytest-input.json`, `pytest.log` and `pytest-results.json`: selected paths,
  local outcomes, collection errors, process exit, pytest subprocess wall time
  and summed durations from actual setup/call/teardown reports. Child subtest
  durations are retained but excluded from the sum because the parent call
  already includes them. Failure nodeids include failed subtests' parents.
- `.coverage`: scratch coverage data from the selected test process.
- `full/workflow-run.json`, `full/jobs-page-*.json`, `full/job-*.log` and
  `full/fetch-errors.json`: raw metadata, four full shard logs and retrieval gaps.

Files are written as their inputs become available. Installation or checkout
failure can leave only an incomplete report. Expired runs deliberately contain
no selection or pytest results. An absent result is never interpreted as a pass.

The prefetch GETs only the requested workflow run **and explicit attempt**, then
that attempt's jobs and their logs through the
[Actions jobs API](https://docs.github.com/en/rest/actions/workflow-jobs?apiVersion=2022-11-28#list-jobs-for-a-workflow-run-attempt).
It never fetches a different run or attempt
as a fallback. API metadata can identify the PR head instead of the merge, so
each log must independently contain checkout's exact full merge SHA and the
expected shard index. The four distinct expected job names must all be completed
with success or failure conclusions. Skipped, cancelled, timed-out, duplicate
or absent jobs, API errors, collection errors, missing summaries and
unattributable outcomes leave the comparison incomplete. A failed job with a
complete pytest summary and attributable failures can still be compared.

Prefetch writes raw metadata, logs and `fetch-errors.json` beneath
`$RUNNER_TEMP/pr-tier-full`. A failed prefetch leaves the selected tests and
reporting steps enabled. Finalization accepts that directory through
`--full-evidence`, retains its files under `full/`, and reads only the cache.
It never authenticates or falls back to a live request. Missing cache files,
unfinished pagination and retained fetch errors keep the candidate count null.
The upload also retains prefetched files if checkout or installation failed.
Before copying, retention removes only the known output `full` entry and creates
a fresh directory. Existing files cannot mix with the cache. A `full` symlink
is unlinked without following its target, including when it points at the
prefetch cache. The workflow refuses a symlink at `.pr-tier-shadow` itself.
Retention does not remove the prefetch cache.

The parser attributes terminal summaries to the primary Actions execution step
and the known grokbot auxiliary step. It reads failures from the final short
test summary section, excluding captured output and traceback text. Passing
runs can omit that section. Missing or ambiguous terminal evidence remains
incomplete. Pytest 9 SUBFAILED reports count individually when checking the
failure total, then map to unique parent nodeids for comparison.

The shard-zero grokbot wrapper appends `ERROR: grokbot MCP tests failed` or
`ERROR: grokbot MCP tests were skipped; install the grokbot extra` after the
pytest summary on failure or skips. The parser requires a terminal pytest
summary, so those wrapper outcomes leave the comparison incomplete even when
pytest supplied failure nodeids. Record this as a grokbot wrapper evidence gap.

`full_only_candidates` are full-log FAILED/ERROR/SUBFAILED nodeids absent from the shadow
failure set. They are **unconfirmed candidates**. On incomplete evidence,
`full_only_candidate_count` is null even if some candidates can be retained.
Only complete evidence can report zero candidates. `confirmed_real_miss_count`
always remains null here. The job never classifies candidates as real, inherited
or flaky and never asserts zero real misses.

The trial assumes the author trust boundary of same-repository PRs. Those
authors can already edit repository workflows. This job provides no sandbox
for adversarial in-repository code, including executable replacement through
`GITHUB_PATH` or detached writers that race validation and upload.
The observation is self-reported by PR-controlled tests and scripts, which can
forge its evidence after prefetch. The read-only token is used before those
processes start and is absent from their environment.
The final tokenless scan rejects root and nested symlinks without following
them. PR-controlled writers can change the tree after this check.
Adversarial changes require independent reproduction in a trusted environment
before these observations can support a decision.

Full logs expose pytest elapsed seconds, not summed testcase durations.
`full_pytest_elapsed_seconds_sum` sums the four primary shard elapsed times.
Auxiliary pytest runs are retained separately. `full_job_wall_seconds_sum`
includes installation and other job steps. `full_parallel_wall_seconds` spans
the earliest shard start to the latest completion, including scheduling skew.
Selected subprocess wall time and summed actual testcase durations are separate
fields. Full testcase durations and historical estimates remain null because
neither is available to this job. CSV timings from the prior analysis are
historical estimates and must not be presented as measured trial runtime.

Both the selected process and full primary shards use `--cov=brigade
--cov-report=` with `COVERAGE_CORE=sysmon`. The selected process sets its
`COVERAGE_FILE` under the output directory before importing pytest and explicitly
uses `--cov-fail-under=0`, so a partial selection has no whole-package coverage
floor. Matching instrumentation improves cost comparability. Collection,
process startup, reporting and selected-worker instrumentation still differ.

## Reproduce and evaluate

Download the artifact and check identity before interpreting a comparison. Use
the recorded merge SHA and first-parent base, manifest and changed list to replay
selection. In an isolated checkout with the existing development dependencies:

```bash
python - <<'PY'
import json
from pathlib import Path
import subprocess
import sys

from scripts.pr_tier_selection import select_tests

record = json.loads(Path("../shadow-evidence/identity.json").read_text())
selection = select_tests(Path("."), record["changed"])
print(json.dumps(selection, indent=2))
Path("../shadow-evidence/pytest-input.json").write_text(json.dumps({"selected": selection["selected"]}))
subprocess.run(
    [sys.executable, "scripts/pr_tier_shadow.py", "pytest", "--output", "../shadow-evidence"],
    check=True,
)
PY
```

Keep downloaded evidence in `../shadow-evidence/` outside the checkout.
The runner's `--now` option exists for offline boundary reproduction. It must
not extend the live workflow window. Retained metadata and logs can also be
passed to `compare` offline without network access.

For every candidate, reproduce the exact nodeid at the recorded merge SHA and
at the recorded first-parent base in separate temporary checkouts, with matching
Python and dependencies. Record commands, outcomes and reruns. A failure newly
introduced by the merge that shadow did not expose is a confirmed real miss.
A matching base failure supports inherited classification. A flaky label needs
repeat-run evidence. An unreproducible result stays unconfirmed. Do not silently
replace the recorded run, attempt or SHA with a more convenient example.

After the window, inventory all PR runs and attempts. Report fork PRs and
missing-identity events separately as admission exclusions. A skipped shadow
job supplies no comparison evidence and must never count as a pass or a
zero-candidate observation. Show exclusion counts beside the eligible
same-repository population and do not generalize its results to fork PRs.

Review all eligible trial runs and attempts, including missing or cancelled
observations. Report complete and incomplete counts, each gap's cause,
all candidates and their reproduction evidence. The decision criteria are:

1. Zero **confirmed real** full-only failures, with every candidate resolved.
2. No unexplained evidence gaps. Explain exclusions and retain their denominator.
3. Median comparable test-time cost below 25 percent: selected pytest subprocess
   wall seconds divided by the sum of the four primary full-shard pytest elapsed
   seconds, using only complete observations with positive denominators. This is
   a coverage-instrumented elapsed-time cost proxy. It does not measure CPU
   usage, billing or the whole-job cost including setup and installation.

Tabulate complete and incomplete observations separately for fallback and
non-fallback selections. Count selected-run timeouts, prefetch timeouts,
full-shard timeouts, cancellation or missing artifacts, API or identity gaps,
parse gaps, grokbot wrapper gaps and trial-start clock boundary gaps. An
observation can have several gap causes. Keep incomplete observations in the
trial denominator and report any retained selected wall time. Incomplete
fallback timeouts must be counted separately so excluding long unfinished
runs from the comparable-cost median cannot conceal expensive fallbacks.
Missing cost ratios remain unknown.

Separately report median selected wall time, summed full job wall time and full
parallel span. The four full shards run in parallel, while selected files run
in one process. A cost ratio cannot be read as the same latency improvement.
Report fallback frequency and the distribution of selected file counts so a
small median does not conceal expensive shared changes. Unknown results remain
unknown. Meeting these criteria supplies evidence for a separately approved
decision and does not turn this job into a gate.

## Removal

At expiry the guard disables installation and test work. After the evaluation
and evidence export, remove only the `pr-tier-shadow` job, its three scripts,
two test files and this document. Remove its entry from the expected job
inventory in `tests/test_ci_workflow.py`. Keep required jobs and `ci-gate` untouched.
Do not add a schedule or modify branch protection as part of removal. Export
artifacts before the 30-day retention period ends.
