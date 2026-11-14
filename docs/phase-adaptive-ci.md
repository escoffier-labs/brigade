# Adaptive CI campaign

Tracked design and log for making Brigade's CI cheaper and faster without
weakening what it protects. Every claim in this campaign is measured with
`scripts/ci_timing_report.py`, run the way the Method section describes, so a
later reader can repeat the number.

## Why

Most of the test growth in this repository was written by coding agents. The
suite is now larger than the product it tests, and the pull request path pays
for it.

Size at commit `3510da921d64` (`docs/adaptive-ci/baseline-inventory-2026-11-09.json`):

| measure | value |
|---|---|
| test files (`tests/**/test_*.py`) | 830 |
| `def test_` functions | 19,501 |
| pytest items collected | 56,878 |
| test LOC (all `.py` under `tests/`) | 534,657 |
| source LOC (all `.py` under `src/brigade/`) | 429,182 |
| test LOC per source LOC | 1.25 |

LOC is physical lines, blank lines and comments included, for both sides.

Two costs show up in the CI timings below:

- **The Windows tail.** A pull request run usually finishes in about 15
  minutes, but its p90 is 26.6 minutes and the worst run took 33.9. The long
  runs are the `windows-affected-tests` job: median 143 seconds, p90 1,567
  seconds, max 1,986 seconds. It is short when the selection is small and long
  when it is large.
- **The merge queue repeats work.** Every merge group runs the Rust hub suite
  (`hub-rust`) on two toolchains of about 12 to 15 minutes each, whatever the
  change touched, plus the full Linux and Windows jobs. A queue entry costs a
  median of 123.6 runner minutes against 75.7 for a pull request run.

The goal is a smaller, better targeted suite and cheaper gates, with each
saving measured against the baseline below and each safety net kept or replaced
by something at least as strong.

## Metrics

All definitions match `scripts/ci_timing_report.py`. Its module docstring is the
source if the two ever disagree.

- **Window.** Runs whose `created_at` is at or after `--since` and before
  `--until` (UTC, half-open `[since, until)`, so adjacent windows never share a
  run) and whose first attempt has completed, for one workflow file: a
  `run_attempt` 1 run with status `completed`, or any run with a later attempt
  (attempt 1 is complete by definition, even while a re-run is in progress,
  which leaves `latest_conclusion` null).
- **First attempt.** Every per-run metric describes the first attempt of the
  run, so wall time, runner time and conclusion come from one attempt and show
  what a developer waited for on the first try. For a run whose `run_attempt`
  is above 1 the script reads `/runs/{id}/attempts/1` for the timestamps and
  conclusion and `/runs/{id}/attempts/{n}/jobs` for the jobs. The report also
  stores the latest `run_attempt`, `reran` and `latest_conclusion` per run, and
  counts `reruns` per event.
- **Successful view.** Runs whose first attempt concluded `success`. This is the
  headline view. A run that failed first and passed on a re-run is therefore in
  the all view only. **All view.** Every completed run, including `failure` and
  `cancelled`. Cancelled runs are common here because a newer push cancels the
  older run.
- **Wall minutes.** `run_started_at` to `updated_at` of the first attempt. It
  excludes time queued before the run started.
- **Runner minutes.** Sum of `completed_at - started_at` over the first
  attempt's jobs that were not skipped and that held a runner. Cancelled jobs
  count when they ran, because they held a runner, and each matrix job counts.
  A job held a runner when the jobs API gives it a non-zero `runner_id` or a
  non-empty `runner_name`. A job cancelled while queued can carry both
  timestamps with `runner_id` 0 and an empty `runner_name`, so its span is queue
  wait. Those jobs are excluded from every runner metric (runner
  minutes, Windows runner minutes, all-attempt cost and the job families) and
  counted per run as `unassigned_jobs`. The runner name itself is never stored.
  Zero-second jobs whose name still holds an unexpanded `${{` expression
  (cancelled before the matrix expanded) are dropped. Wall minutes are not
  affected. The sum measures cost and wall minutes measure latency. Re-run cost
  is not in this number.
- **Runner minutes, all attempts.** The same sum over every attempt of the run,
  reported separately as `runner_minutes_all_attempts`. The jobs API lists a
  job that a later attempt did not re-run again under that attempt with its
  original timestamps, so a job whose name and timestamps match an earlier
  record counts once. It equals runner minutes for a run that was not re-run.
- **Windows runner minutes.** The same sum restricted to jobs whose first
  runner label starts with `windows`. Reported apart because GitHub weights
  Windows minutes at twice Linux when minutes are billed. The report also keeps
  a median per OS (`linux`, `windows`, `macos`, `self-hosted`, `other`).
- **Percentiles.** Linear interpolation between order statistics (numpy's
  default method). `p75`, `p90` and `max` are over runs. Job-family
  percentiles are over job instances, not per run, so four shards of one family
  add four samples per run.
- **Job family.** Matrix names are normalized: a trailing integer shard index is
  dropped (`test shard (3.12, 1)` becomes `test shard 3.12`) and a free-text
  argument group is dropped (`install-from-source (x, --depth y)` becomes
  `install-from-source`). Other names stay whole, so the two `hub-rust` toolchain
  jobs remain separate. For each family the report records how many runs it ran
  in (`runs_with`, `ran_fraction`), its job instances, and median, p90 and max
  seconds.
- **Warnings.** A page count that disagrees with `total_count`, a re-run whose
  wall time is shorter than its longest job, a run still being re-run at capture
  time (its all-attempt cost is partial), and a redacted job name are
  printed to stderr and stored in the `warnings` list of the JSON. A search
  slice that reaches the 1,000 row cap is split and retried, and the script
  exits non-zero if a one second slice still reaches it.
- **Privacy.** The report stores run ids, head SHAs (which anyone can trace to
  a commit and its author through the GitHub API), events, job names, runner
  labels and numbers. It does not store user names, branch names or PR titles.
  A runner label is kept only when it is a GitHub-hosted image label
  (`ubuntu-*`, `windows-*`, `macos-*`) or exactly `self-hosted`. Any other label
  is stored as its OS bucket. A job name carrying the run's branch name or
  actor login is stored as `redacted`. The match is case-insensitive and on
  whole words (the name is split on non-alphanumerics): a name of several words
  must appear as that exact word sequence, and a single word must be at least
  five characters, so a branch called `lint` or `docs` does not rewrite every
  lint or docs job. A lone name of four characters or fewer therefore cannot be
  detected in a job name.

Test-suite size (`--inventory`) is the commit, test file count, `def test_`
count, test and source LOC, and the pytest collected item count read from a
saved `pytest --collect-only -q` output.

## Baseline

Captured 2026-11-09 for `ci.yml`.

### Choosing the window

The window starts after the last material change to the CI definition and ends
2026-11-09T12:00:00Z. The files that decide CI timing were last changed as
follows (`git log origin/main`):

| file | last change | what changed |
|---|---|---|
| `scripts/ci_pytest_shard.py` | 2026-11-01 13:13Z (#2195) | each shard runs on four xdist workers |
| `scripts/windows_affected_tests.py`, `scripts/windows_pytest.py` | 2026-10-22 to 2026-10-25 | not changed in the last 7 days |
| `.github/workflows/ci.yml` | 2026-11-05 14:47Z (#2304), then only path-filter additions on 11-05, 11-06 and 11-07 | #2304 added `hub-rust` to the merge queue and `ci-gate` |
| `.github/workflows/hub.yml` | 2026-11-08 20:49Z (#2308) and 21:51Z (#2398) | #2398 added a `linux-arm64` job and more steps to the reusable hub workflow |

CI therefore changed materially inside the last seven days, and a clean seven
day window does not exist. Two windows are recorded:

1. **Baseline (strict).** 2026-11-08T22:00:00Z to 2026-11-09T12:00:00Z, 14
   hours, starting after #2398. Everything the campaign touches is in its final
   shape. It is short, so n is small.
2. **Extended (supplementary).** 2026-11-05T15:00:00Z to 2026-11-09T12:00:00Z,
   about 3.9 days, starting after #2304. It mixes runs from before and after
   #2308 and #2398, so only the `hub-rust` jobs are affected by the mix. Use it
   to see the spread of the unchanged jobs. It is not the comparison baseline.

Later changes are compared against the strict baseline, or against a fresh
window taken the same way.

### Strict baseline numbers

Files: `docs/adaptive-ci/baseline-ci-2026-11-09.json` (raw, per-run and
per-job) and `.md` (summary). Window 2026-11-08T22:00:00Z to
2026-11-09T12:00:00Z (half-open), 123 runs. Times are
minutes, first attempt of each run.

| event | view | n | wall median | wall p75 | wall p90 | wall max | runner median | runner p90 | Windows runner median | Windows runner p90 |
|---|---|---|---|---|---|---|---|---|---|---|
| merge_group | successful | 20 | 14.9 | 15.2 | 15.4 | 15.8 | 123.6 | 130.3 | 12.2 | 13.5 |
| merge_group | all | 29 | 14.9 | 15.2 | 15.4 | 15.8 | 126.7 | 133.9 | 12.4 | 13.6 |
| pull_request | successful | 19 | 14.7 | 17.2 | 26.6 | 33.9 | 75.7 | 83.8 | 9.8 | 31.9 |
| pull_request | all | 74 | 10.4 | 14.6 | 18.0 | 33.9 | 55.1 | 78.9 | 10.8 | 22.3 |
| push | all | 20 | 11.0 | 32.3 | 134.3 | 509.5 | 79.5 | 110.8 | 7.7 | 8.9 |

All 20 `push` runs in the window were cancelled, so there is no `push`
baseline. Each holds a `trusted-runner-canary` job that was cancelled in the
queue (no runner assigned), which the report counts as `unassigned_jobs` and
leaves out of runner minutes. The `all` rows include cancelled runs, which end early, so they read
lower than the `successful` rows. Compare successful runs. The `successful`
view holds runs whose first attempt passed, so a run that failed first and
passed on a re-run is in the `all` rows only (see the re-run caveat).

Job families that dominate runner time in successful runs (median seconds,
p90 seconds, share of runs the job ran in). Percentiles are over job instances,
not per run:

`pull_request`, successful runs, n=19:

| family | runner | ran in | median s | p90 s |
|---|---|---|---|---|
| test shard 3.12 | ubuntu-latest | 19/19 | 472 | 580 |
| hub-rust / hub (Rust 1.85.0) | ubuntu-latest | 9/19 | 856 | 894 |
| hub-rust / hub (Rust stable) | ubuntu-latest | 9/19 | 761 | 836 |
| windows-native-acceptance | windows-latest | 19/19 | 307 | 336 |
| windows-affected-tests | windows-latest | 19/19 | 143 | 1567 |
| engine-rename-installer | ubuntu-latest | 19/19 | 95 | 107 |
| content-guard | ubuntu-latest | 19/19 | 68 | 81 |
| quickstart-smoke (windows-latest) | windows-latest | 19/19 | 56 | 59 |
| lint | ubuntu-latest | 19/19 | 46 | 52 |

`merge_group`, successful runs, n=20:

| family | runner | ran in | median s | p90 s |
|---|---|---|---|---|
| hub-rust / hub (Rust 1.85.0) | ubuntu-latest | 20/20 | 860 | 892 |
| hub-rust / hub (Rust stable) | ubuntu-latest | 20/20 | 734 | 838 |
| test shard 3.12 | ubuntu-latest | 20/20 | 500 | 582 |
| test shard 3.11 | ubuntu-latest | 20/20 | 412 | 486 |
| windows-native-acceptance | windows-latest | 20/20 | 280 | 331 |
| Windows (stable, default features) | windows-latest | 20/20 | 202 | 224 |
| Fleet Hub container (linux/arm64) | ubuntu-latest | 20/20 | 156 | 184 |
| engine-rename-installer | ubuntu-latest | 20/20 | 94 | 112 |
| build-and-test | ubuntu-latest | 20/20 | 90 | 100 |

### Extended numbers

Files: `docs/adaptive-ci/baseline-ci-2026-11-09-extended.json` (aggregates
only, no per-run list) and `.md`.

Window 2026-11-05T15:00:00Z to 2026-11-09T12:00:00Z (half-open), 497 runs. Times are minutes, first attempt of each run.

| event | view | n | wall median | wall p75 | wall p90 | wall max | runner median | runner p90 | Windows runner median | Windows runner p90 |
|---|---|---|---|---|---|---|---|---|---|---|
| merge_group | successful | 70 | 11.5 | 13.4 | 15.1 | 15.8 | 120.5 | 125.3 | 12.6 | 13.9 |
| merge_group | all | 88 | 11.6 | 13.9 | 15.1 | 15.8 | 120.5 | 129.1 | 12.6 | 13.8 |
| pull_request | successful | 111 | 11.7 | 14.2 | 19.3 | 71.7 | 70.5 | 87.3 | 9.2 | 24.7 |
| pull_request | all | 342 | 11.0 | 13.4 | 19.0 | 71.7 | 61.4 | 86.1 | 9.2 | 24.0 |
| push | all | 67 | 35.4 | 88.9 | 254.7 | 638.5 | 93.4 | 108.7 | 8.0 | 9.3 |

`pull_request`, successful runs, n=111:

| family | runner | ran in | median s | p90 s |
|---|---|---|---|---|
| test shard 3.12 | ubuntu-latest | 110/111 | 479 | 583 |
| hub-rust / hub (Rust 1.85.0) | ubuntu-latest | 80/111 | 636 | 761 |
| hub-rust / hub (Rust stable) | ubuntu-latest | 80/111 | 612 | 707 |
| windows-native-acceptance | windows-latest | 110/111 | 316 | 351 |
| windows-affected-tests | windows-latest | 110/111 | 144 | 1124 |
| engine-rename-installer | ubuntu-latest | 110/111 | 93 | 107 |
| content-guard | ubuntu-latest | 111/111 | 70 | 79 |
| quickstart-smoke (windows-latest) | windows-latest | 110/111 | 56 | 73 |
| lint | ubuntu-latest | 110/111 | 44 | 51 |

`merge_group`, successful runs, n=70:

| family | runner | ran in | median s | p90 s |
|---|---|---|---|---|
| hub-rust / hub (Rust 1.85.0) | ubuntu-latest | 70/70 | 659 | 886 |
| hub-rust / hub (Rust stable) | ubuntu-latest | 70/70 | 628 | 776 |
| test shard 3.12 | ubuntu-latest | 70/70 | 506 | 589 |
| test shard 3.11 | ubuntu-latest | 70/70 | 408 | 483 |
| windows-native-acceptance | windows-latest | 70/70 | 314 | 337 |
| Windows (stable, default features) | windows-latest | 70/70 | 200 | 236 |
| Fleet Hub container (linux/arm64) | ubuntu-latest | 70/70 | 171 | 186 |
| build-and-test | ubuntu-latest | 70/70 | 92 | 113 |
| engine-rename-installer | ubuntu-latest | 70/70 | 96 | 110 |

### Reproducibility check

The timing script was run twice over the strict window and twice over the
extended window, each pair with the final script. Within each pair the JSON was
identical apart from `generated_at` (`diff` after removing that line, zero
differing lines), and the extended pair used `--aggregates-only`. No capture
printed a warning.

A window's run list does not depend on the status the API reports now. The
script lists runs without a status filter, because the API's status is the
latest attempt's and a run being re-run at capture time would otherwise drop out
of its window although its first attempt finished long before. It keeps a
`run_attempt` 1 run when its status is `completed` and any later attempt
unconditionally. Listing without the filter also returned two completed
first-attempt pull request runs (ids 42265815042 and 42513224413) that the
`status=completed` listing omitted, which is why the extended window holds 497
runs here and 495 in the earlier capture (the strict window is 123 either way).

Two things can still move a number between captures. A window is only complete
once every run created in it has finished its first attempt, so end a window
well before the capture and treat `push` numbers as unstable: `push` runs wait
on the self-hosted `trusted-runner-canary` job for hours. And
`runner_minutes_all_attempts` for a run that is being re-run at capture time is
partial, which the script reports as a warning. GitHub allows re-runs for 30
days, so recapturing an old window later can change that one field and
`latest_conclusion`, never the first-attempt metrics.

### Caveats

- n is small in the strict window: 19 successful pull request runs and 20
  successful merge group runs.
- 2026-11-09 is a Monday and the window is mostly overnight in US time, so the
  mix of pull request sizes may differ from a weekday afternoon. This affects
  `windows-affected-tests`, whose duration follows the size of the selection.
- Re-runs are measured on their first attempt. 5 of the 74 pull request runs
  in the strict window were re-run. All 5 failed on the first attempt and passed
  later, so none is in the `successful` view. Their first-attempt wall times are
  10.2, 11.2, 14.6, 15.4 and 26.5 minutes. Counting every attempt they cost 97.7
  more runner minutes than their first attempts alone, and the headline runner
  minutes leave that out. `runner_minutes_all_attempts` holds the figure: the
  pull request `all` view has a median of 55.1 runner minutes on first attempts
  against 56.6 over every attempt. A flaky test therefore lowers the `successful` n and raises the `all` cost
  instead of appearing as a slow successful run. In the
  extended window 12 of 342 pull request runs were re-run.
- `trusted-runner-canary` runs on a self-hosted runner and waits hours for one.
  A run cancelled in that queue has the job's timestamps set but no runner
  assigned, so the report counts it in `unassigned_jobs` and leaves its span out
  of runner minutes. Across the strict window 194 jobs in 54 runs were
  unassigned (all cancelled): 128 in 34 pull request runs, almost all with a
  near-zero span, and 66 in the 20 `push` runs, 20 of them the canary and
  together about 1,061 queue minutes that an earlier capture had counted as
  runner time. It does not appear in successful `pull_request` or `merge_group`
  runs, so those numbers are unaffected. It still dominates `push` wall time,
  which is not changed.
- Hosted runner speed varies between runs. Compare medians and p90 across
  several runs, never a single run.
- The inventory's pytest count was collected on a clean checkout of the same
  commit as the CI baseline's merge base, without this PR's own test file.
- The Actions runs API returns at most 1,000 runs per query. The script slices
  the window into six hour queries, splits a slice that reaches the cap and
  retries it, and compares the rows it collected with `total_count`. No warning
  was printed for either window.

## After window, 2026-11-13

Files: `docs/adaptive-ci/after-ci-2026-11-13.json` (aggregates only) and `.md`.
Window 2026-11-09T19:15:00Z to 2026-11-13T12:00:00Z (half-open), 311 runs,
starting after the last campaign PR (#2439) merged at 19:14:39Z.

### Choosing the window

Two commits inside the window touched the files that decide CI timing. Neither
is material, so the window was not shortened:

- #2462 (2026-11-10 00:32Z) pinned the actions in `hub.yml` and `publish.yml` to
  commit SHAs of the same versions.
- #2468 (2026-11-10 12:51Z) added one step to `hub.yml` that starts the hub
  binary on setup's output, and two hub paths (`ops/fleet-hub/bin/hub-setup.py`,
  `ops/fleet-hub/r1-models.json`) to `scripts/ci_hub_paths.yml`.

The hub test flake fixes (#2459) merged at 2026-11-09T23:43:50Z, about 4.5 hours
into the window. One failed `merge_group` run before that is one of the flakes
it fixed.

### Reproducibility

The timing script ran twice over the window, the first time with the per-run
list and the second with `--aggregates-only`. The aggregates were identical
apart from `generated_at` (zero differing lines), the Markdown summaries were
identical apart from the generated line, and neither run printed a warning.

### Before and after

Successful view, first attempts, minutes. Baseline is the strict window.

| event | metric | baseline | after |
|---|---|---|---|
| merge_group | n | 20 | 46 |
| merge_group | wall median / p75 / p90 / max | 14.9 / 15.2 / 15.4 / 15.8 | 14.1 / 15.4 / 15.6 / 16.1 |
| merge_group | runner median / p90 | 123.6 / 130.3 | 127.2 / 133.4 |
| merge_group | runner mean (total / n) | 124.3 | 116.6 |
| merge_group | Windows runner median / p90 | 12.2 / 13.5 | 12.9 / 13.9 |
| merge_group | runs where `hub-rust` ran | 20 of 20 (100%) | 32 of 46 (70%) |
| pull_request | n | 19 | 92 |
| pull_request | wall median / p75 / p90 / max | 14.7 / 17.2 / 26.6 / 33.9 | 13.3 / 15.4 / 16.2 / 18.4 |
| pull_request | runner median / p90 | 75.7 / 83.8 | 78.5 / 115.8 |
| pull_request | runner mean (total / n) | 70.9 | 76.0 |
| pull_request | Windows runner median / p90 | 9.8 / 31.9 | 8.9 / 39.1 |

Job families, seconds:

| family | event | baseline median / p90 / max | after median / p90 / max |
|---|---|---|---|
| `hub (Rust 1.85.0)` | merge_group | 860 / 892 / 907 | 813 / 914 / 924 |
| `hub (Rust stable)` | merge_group | 734 / 838 / 860 | 813 / 884 / 903 |
| Windows affected tests, slowest job per run | pull_request | 143 / 1,567 / 1,986 (one job, n=19) | 362 / 783 / 1,032 (slowest shard, n=74) |

The Windows row compares the single `windows-affected-tests` job of the
baseline with the slowest `windows-affected-shard` job of each run after #2440,
computed from the per-run list of the first capture. Runs whose plan selected
nothing have no shard job and are not in that row.

Flakes and re-queue cost:

| measure | baseline | after |
|---|---|---|
| `merge_group` runs not successful (all n minus successful n) | 9 of 29 | 3 of 49 |
| their runner minutes (all total minus successful total) | 1,181.7 | 340.1 |
| `reruns`, `merge_group` / `pull_request` | 0 / 5 | 0 / 5 |
| `pull_request` all-attempt minus first-attempt runner minutes | 97.7 | 38.6 |

The three failed `merge_group` runs after: the row-budget sweep flake on #2446
before #2459 merged (run 42693841937), the hub test binary SIGSEGV on #2462
(run 42727257188, issue #2470, fixed by #2494), and a Python test,
`test_fleet_action_adapter_bounds.py::test_cleanup_stops_signal_ignoring_descendant_holding_pipes[overflow]`,
on #2508 (run 43111993967).

### What the numbers say

- **Pull request tail: improved.** p90 wall fell from 26.6 to 16.2 minutes and
  the worst run from 33.9 to 18.4. The slowest Windows affected job per run fell
  from a p90 of 1,567 to 783 seconds. This is #2440's sharding.
- **Pull request cost: worse.** Runner median rose from 75.7 to 78.5, runner p90
  from 83.8 to 115.8, and Windows runner p90 from 31.9 to 39.1. `hub-rust`
  also ran in a larger share of successful pull requests (48 of 92, against 9
  of 19). The breakdown below shows this is a change in which pull requests
  ran, not a higher cost for the same pull request.
- **Merge queue cost: lower on average, not at the median.** The hub suite was
  skipped in 14 of 46 successful groups (2 of them documentation-only). A
  skipped group had a median of 11.0 wall and 100.9 runner minutes, against
  15.2 and 129.2 for a group that ran it. The three hub jobs' medians add up
  to 1,688 seconds, so a skip saves about 28 runner minutes and the 14 skips
  saved about 394 over the window (about 396 from the difference of the two
  group medians). Because 70% of groups still ran the hub
  suite, the median group is a hub group and the runner median did not move
  down (123.6 to 127.2). The mean fell from 124.3 to 116.6.
- **Failed queue runs: fewer.** 3 of 49 against 9 of 29, costing 340 runner
  minutes against 1,182. The baseline window's failures were not classified,
  so this records the change without attributing it.

### Why pull request runner p90 rose

Successful `pull_request` runs, split by whether `hub-rust` ran and whether
the Windows affected-test jobs used more than 15 runner minutes. Computed from
the per-run job list of the same capture (not in the committed aggregates).

| `hub-rust` ran | Windows affected over 15 min | baseline n | baseline runner median | after n | after runner median |
|---|---|---|---|---|---|
| no | no | 6 | 53.7 | 32 | 55.0 |
| no | yes | 4 | 76.1 | 12 | 69.9 |
| yes | no | 7 | 78.0 | 29 | 82.8 |
| yes | yes | 2 | 91.0 | 19 | 116.0 |

- Three of the four rows moved by 6 minutes or less. The share of runs with
  both expensive suites went from 2 of 19 (11%) to 19 of 92 (21%), and those
  runs are the top of the distribution.
- Sharding is not the cause. The shortest shard in the window took 71 seconds,
  so an extra shard costs at most about 1.2 minutes of setup. A full
  three-shard selection used 30 to 37 Windows minutes in total, against 26 to
  33 for the single job on a broad selection in the baseline.
- The five most expensive runs were all created after 02:00Z on 2026-11-13 and
  each spent about 40 minutes outside the hub, Windows and test-shard jobs,
  against about 17 normally. The extra was path-selected jobs (mesh, the Fleet
  Hub container, feature configuration) on pull requests that touched those
  areas.
- The last row's baseline median is from two runs, so 91.0 against 116.0 is
  weak evidence on its own.
- Run records do not carry the number of selected tests, so a wider selection
  for the same diff is not ruled out directly.

The hub suite itself is the larger lever. In three pull request runs on
2026-11-12 and 2026-11-13 the `hub (Rust 1.85.0)` job took 14.0 to 15.5
minutes and repeated
what `hub (Rust stable)` runs: `cargo test` (4.9 to 5.7 minutes) and the
Python integration steps (8.2 to 8.8 minutes). Workstream 6 replaces it with
a compile on 1.85.0 outside the nightly.

### Caveats

- The baseline is 14 hours overnight and the after window 3.7 days of weekday
  work, so n and the change mix differ. Much of the work merged in the after
  window edited `memory_hub_*` and `fleet_client*` files, which are hub paths.
  The skip rate depends on that mix.
- Runner p90 and mean move with the share of runs that include the hub suite
  and with selection size, which this campaign does not control.
- The Windows row compares one job with the slowest of up to three shards. It
  measures what a pull request waits for, not total Windows time.

## Workstreams

Each lane adds its own file under `docs/adaptive-ci/` and one row to the Results
log. Add the PR link and move the status when a lane lands or stops.

| # | workstream | status | lane file |
|---|---|---|---|
| 1 | Test-audit pruning pilot on the grokbot subsystem | merged 2026-11-09 16:30Z ([#2438](https://github.com/escoffier-labs/brigade/pull/2438)) | [`docs/adaptive-ci/test-audit-grokbot.md`](adaptive-ci/test-audit-grokbot.md) |
| 2 | Windows affected-tests tail | merged 2026-11-09 17:07Z ([#2440](https://github.com/escoffier-labs/brigade/pull/2440)), measured 2026-11-13 (see After window) | [`docs/adaptive-ci/windows-tail.md`](adaptive-ci/windows-tail.md) |
| 3 | `hub-rust` path filtering in the merge queue | merged 2026-11-09 16:55Z ([#2436](https://github.com/escoffier-labs/brigade/pull/2436)), live, measured 2026-11-13 (see After window) | [`docs/adaptive-ci/hub-merge-queue.md`](adaptive-ci/hub-merge-queue.md) |
| 4 | Linux pull request test selection in shadow mode | merged 2026-11-09 17:47Z ([#2435](https://github.com/escoffier-labs/brigade/pull/2435)), collecting observations, skips nothing | [`docs/adaptive-ci/shadow-selection.md`](adaptive-ci/shadow-selection.md) |
| 5 | Merge-group docs classifier loaded from trusted `main` (found while building lane 3) | merged 2026-11-09 17:41Z ([#2441](https://github.com/escoffier-labs/brigade/pull/2441)) | [`docs/adaptive-ci/docs-classifier-trust.md`](adaptive-ci/docs-classifier-trust.md) |
| 6 | `hub-rust` compiles on 1.85.0 instead of repeating the suite there; the nightly keeps the full run | merged 2026-11-14 00:15Z ([#2541](https://github.com/escoffier-labs/brigade/pull/2541)), first four runs measured (see Results log) | [`docs/adaptive-ci/hub-merge-queue.md`](adaptive-ci/hub-merge-queue.md#the-1850-toolchain) |

The baseline and `scripts/ci_timing_report.py` merged 2026-11-09 19:14Z
([#2439](https://github.com/escoffier-labs/brigade/pull/2439)).

<!-- adaptive-ci: add a workstream row above this line -->

## Early observations

These are single runs read from the Actions API, not windowed results. They
show the mechanism working. The Results log holds the measured comparison once
an after window exists.

### Merge-queue hub selection, first day

Merge-group runs of `ci.yml` after #2436 merged. Minutes are `created_at` to
`updated_at` of the run, not the report's wall metric, and each is one run.

| run | queue entry | `hub` | published reason | why | minutes |
|---|---|---|---|---|---|
| 42663340698 | #2433 | skipped | `no-hub-path-changed` | no hub path in the range | 11.4 |
| 42664944828 | #2442 | ran | `trusted-hub-selector-unavailable` | fail-closed fallback, see below; #2442 also changed `engines/hub/**` itself | 13.2 |
| 42685668967 | #2454 | skipped | `no-hub-path-changed` | no hub path in the range | 10.4 |
| 42687967106 | #2448 | skipped | `no-hub-path-changed` | no hub path in the range | 11.5 |
| 42679377812 | #2437 | ran, failed | `hub-path-changed` | own diff (`src/brigade/fleet_client*.py`, `cli/fleet*.py` and others) | 12.9 |
| 42679381170 | #2439 | ran | `hub-path-changed` | only from #2437 ahead of it in the queue | 12.5 |
| 42679427735 | #2444 | ran | `hub-path-changed` | own diff (`src/brigade/fleet_client_message_credentials.py`) | 16.2 |
| 42693841937 | #2446 | ran, failed | `hub-path-changed` | own diff (`src/brigade/memory_hub_client.py`, matched by `src/brigade/memory_hub_*.py`) | 10.8 |

The #2439 row is the case the cumulative range exists for. #2439's own diff
touches no hub path (`.gitignore`, the baseline files, this document and
`scripts/ci_timing_report.py` with its test). It was queued behind #2437, whose
fleet client changes are hub paths, so the range from `main` to #2439's head
held them and the hub suite ran. A selector that diffed only
`base_sha..head_sha` would have skipped it.

The #2442 row shows the fail-closed default. `main` was at #2440 and held the
selector from #2436, which wrote `hub` and `reason`. #2441, ahead of #2442 in
the queue, changed the `ci.yml` step to also require `start`. #2442's group ran
#2441's `ci.yml` against `main`'s older selector, the parse failed, and the step
published `hub=true` with `trusted-hub-selector-unavailable`. The selector's own
log line for that run said `hub-selection-source-changed`, which is not what the
step published. No other run in this table published the fallback; the
`trusted-hub-selector-unavailable` text in other logs is the step's source,
which Actions echoes at the top of the step log.

Two of the runs where the hub suite ran failed on hub tests, each passing in
the other run:
`read_counts_tests::the_sweep_stays_within_its_row_budget_and_finishes_over_several_ticks`
(run 42693841937, `left: (1024, true) right: (2000, true)`) and
`push_withheld_tests::push_card_404_latency_does_not_separate_absent_and_withheld_ids`
(run 42679377812, `withheld 13.694945ms vs absent 8.768908ms: ratio 1.562`). The
quoted values come from the job logs. The causes come from source: the first
test runs under the default 20 ms sweep time budget
(`engines/hub/src/memory/read_counts.rs`), which stopped the first tick at a
chunk boundary, and the second asserts a ratio within 1.5
(`push_withheld_tests.rs`). Neither failure comes from this campaign's changes.

The two failures cost different amounts. #2437's group failed, but #2439's
group, built on top of it, passed, and with `HEADGREEN` that green group merged
#2437 and #2439 together at 19:14:39Z. Nothing was re-queued. The failed 12.9 minute run (42679377812, created
about 19:01Z) falls between the baseline window and the after window, so
neither capture counts it. #2446's
failed group had no green group behind it, so #2446 left the queue and needs
another run. A failed queue entry that is re-queued starts a new workflow run
with its own run id, not another attempt of the failed run. Its cost therefore shows up as extra `merge_group`
runs in the window (the failed one in the `all` view, the retry in either
view), not in `reruns` or `runner_minutes_all_attempts`, which count Actions
re-runs of the same run id only.

### Review gates and account identity

Agents in this repository open pull requests and push from the maintainer's
own GitHub account, and that stays so. GitHub does not let an author approve
their own pull request, and an admin bypass would apply to the agents too, so
a required CODEOWNERS review cannot gate changes to the workflows or the
selectors. The guards for CI selection therefore live inside CI, with one
limit that no CI check can close:

- selectors and classifiers for merge groups are loaded from trusted `main`
  and diff the cumulative range, so a queue entry cannot choose its own tests
  by editing a selector, a classifier or a path list;
- this does not cover an entry that edits `.github/workflows/ci.yml` itself. A
  merge group runs the head's `ci.yml`, so such an entry can bypass the
  trusted bootstrap or set gate outputs directly, for its own group and every
  group queued behind it (see "Trust and its limit" in
  [`hub-merge-queue.md`](adaptive-ci/hub-merge-queue.md#trust-and-its-limit)
  and [`docs-classifier-trust.md`](adaptive-ci/docs-classifier-trust.md#trust-and-its-limit)).
  Workflow edits are covered only by review before they are queued;
- `tests/test_ci_workflow.py` checks that no runtime job is admitted when the
  run is cancelled and that the `ci-gate` job covers every required job and
  its dependencies;
- the nightly `hub-nightly.yml` runs the full hub suite on `main` and opens
  or updates one tracking issue on failure, so a wrongly skipped hub change
  surfaces within a day.

## Method

Use this exact procedure so numbers are comparable.

Timing, from a checkout of the repository with `gh` authenticated (read access
to Actions is enough):

```bash
python3 scripts/ci_timing_report.py --workflow ci.yml \
  --since 2026-11-08T22:00:00Z --until 2026-11-09T12:00:00Z \
  --label "strict baseline" \
  --output docs/adaptive-ci/baseline-ci-2026-11-09.json \
  --markdown docs/adaptive-ci/baseline-ci-2026-11-09.md
```

`--repo owner/name` overrides the repository taken from the working directory.
The 14 hour strict window takes about five minutes and the four day extended window about ten (jobs are fetched four at a time).
For a long window add `--aggregates-only` to keep the JSON small. Run it twice
and diff the files (ignoring `generated_at`) when you publish a number.

Test-suite size, through Brigade so the receipt is kept:

```bash
brigade work verify run --target . --capture brigade-work \
  --argv-json '[".venv/bin/python","-m","pytest","--collect-only","-q"]'
python3 scripts/ci_timing_report.py --inventory --root . \
  --collect-output <command-1-stdout.log from the receipt directory it prints> \
  --output docs/adaptive-ci/baseline-inventory-<YYYY-MM-DD>.json
```

Rules for a result:

1. Take the "after" window after the change merged, with no other change to
   `ci.yml`, `hub.yml` or the Windows and shard scripts inside it. Record the
   merge time.
2. Compare the same event and the `successful` view, with the same script.
3. Report n for both sides and report unchanged and worse numbers too.
4. Judge a pruned test by the failures it used to catch as well as the seconds
   saved. Say how the lane showed the coverage was kept or the risk accepted.
5. A change that reduces flaky failures shows up in `reruns` and in
   `runner_minutes_all_attempts`, not in the `successful` percentiles: a flaky
   run that passes only on a re-run is not in the `successful` view at all (its
   first attempt failed), so fewer flakes raise the `successful` n without
   moving its wall or runner percentiles. A lane that claims fewer flaky
   failures must cite those two fields for both windows.

## Results log

Append one row per measured change. Use the same units as the baseline: minutes
for wall and runner time, seconds for a job family.

| date | change | PR | metric | before | after | n |
|---|---|---|---|---|---|---|
<!-- adaptive-ci: append result rows directly below this line -->
| 2026-11-09 | Test-audit pilot, grokbot tests | [#2438](https://github.com/escoffier-labs/brigade/pull/2438) | grokbot test LOC at the merge commit `7bc63865`; grokbot line coverage at `4940187b` | 33,044; 78.618% | 32,595 (-1.4%); 78.618% | 53 files |
| 2026-11-13 | Windows affected-test sharding | [#2440](https://github.com/escoffier-labs/brigade/pull/2440) | `pull_request` successful wall p90 / max | 26.6 / 33.9 | 16.2 / 18.4 | 19 / 92 |
| 2026-11-13 | Windows affected-test sharding | [#2440](https://github.com/escoffier-labs/brigade/pull/2440) | `pull_request` successful runner p90; Windows runner p90 | 83.8; 31.9 | 115.8; 39.1 | 19 / 92 |
| 2026-11-13 | `hub-rust` selection in the merge queue | [#2436](https://github.com/escoffier-labs/brigade/pull/2436) | `merge_group` successful runs with `hub-rust` | 20 of 20 (100%) | 32 of 46 (70%) | 20 / 46 |
| 2026-11-13 | `hub-rust` selection in the merge queue | [#2436](https://github.com/escoffier-labs/brigade/pull/2436) | `merge_group` successful wall median; runner median; runner mean | 14.9; 123.6; 124.3 | 14.1; 127.2; 116.6 | 20 / 46 |
| 2026-11-13 | Campaign window, all changes | #2435 to #2441, #2459 | failed `merge_group` runs; their runner minutes | 9 of 29; 1,181.7 | 3 of 49; 340.1 | 29 / 49 |
| 2026-11-14 | `hub-rust` compiles on 1.85.0 outside the nightly | [#2541](https://github.com/escoffier-labs/brigade/pull/2541) | 1.85.0 hub job duration, seconds, min to max; before is `hub (Rust 1.85.0)` on 3 `pull_request` runs, after is `hub (Rust 1.85.0 compile)` on 2 `pull_request` runs and 2 `merge_group` runs | 839 to 932 | 70 to 144 | 3 / 4 |

## Prior art

- Machalica et al. "Predictive Test Selection." ICSE-SEIP 2019.
  <https://arxiv.org/abs/1810.05286>. Uses a learned model to pick the tests most
  likely to fail for a change, and evaluates the picks against the full run.
- Rothermel et al. "Empirical studies of test-suite reduction." Software
  Testing, Verification and Reliability, 2002. doi:10.1002/stvr.256. Measures how
  much fault detection a reduced suite loses. The loss depends on the program
  and on the reduction technique.
- OpenClaw's test-audit skill.
  <https://github.com/openclaw/openclaw/tree/main/.agents/skills/test-audit>
- OpenClaw adaptive CI.
  <https://github.com/openclaw/openclaw/blob/main/docs/ci/adaptive-testing.md>
- pytest-testmon, change-based test selection for pytest. <https://www.testmon.org/>
