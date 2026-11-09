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
  run) and whose status is `completed`, for one workflow file.
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
  attempt's jobs that were not skipped. Cancelled jobs count, because they held
  a runner, and each matrix job counts. Zero-second jobs whose name still holds
  an unexpanded `${{` expression (cancelled before the matrix expanded) are
  dropped. The sum measures cost and wall minutes measure latency. Re-run cost
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
  wall time is shorter than its longest job, and a redacted job name are
  printed to stderr and stored in the `warnings` list of the JSON. A search
  slice that reaches the 1,000 row cap is split and retried, and the script
  exits non-zero if a one second slice still reaches it.
- **Privacy.** The report stores run ids, head SHAs (which anyone can trace to
  a commit and its author through the GitHub API), events, job names, runner
  labels and numbers. It does not store user names, branch names or PR titles.
  A runner label is kept only when it is a GitHub-hosted image label
  (`ubuntu-*`, `windows-*`, `macos-*`) or exactly `self-hosted`. Any other label
  is stored as its OS bucket. A job name containing the run's branch name or
  actor login (case-insensitive) is stored as `redacted`.

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
2026-11-09T12:00:00Z (half-open), 123 completed runs. Times are
minutes, first attempt of each run.

| event | view | n | wall median | wall p75 | wall p90 | wall max | runner median | runner p90 | Windows runner median | Windows runner p90 |
|---|---|---|---|---|---|---|---|---|---|---|
| merge_group | successful | 20 | 14.9 | 15.2 | 15.4 | 15.8 | 123.6 | 130.3 | 12.2 | 13.5 |
| merge_group | all | 29 | 14.9 | 15.2 | 15.4 | 15.8 | 126.7 | 133.9 | 12.4 | 13.6 |
| pull_request | successful | 19 | 14.7 | 17.2 | 26.6 | 33.9 | 75.7 | 83.8 | 9.8 | 31.9 |
| pull_request | all | 74 | 10.4 | 14.6 | 18.0 | 33.9 | 55.1 | 78.9 | 10.8 | 22.3 |
| push | all | 20 | 11.0 | 32.3 | 134.3 | 509.5 | 92.6 | 222.6 | 7.7 | 8.9 |

All 20 `push` runs in the window were cancelled, so there is no `push`
baseline. The `all` rows include cancelled runs, which end early, so they read
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

Window 2026-11-05T15:00:00Z to 2026-11-09T12:00:00Z (half-open), 495 completed runs. Times are minutes, first attempt of each run.

| event | view | n | wall median | wall p75 | wall p90 | wall max | runner median | runner p90 | Windows runner median | Windows runner p90 |
|---|---|---|---|---|---|---|---|---|---|---|
| merge_group | successful | 70 | 11.5 | 13.4 | 15.1 | 15.8 | 120.5 | 125.3 | 12.6 | 13.9 |
| merge_group | all | 88 | 11.6 | 13.9 | 15.1 | 15.8 | 120.5 | 129.1 | 12.6 | 13.8 |
| pull_request | successful | 110 | 11.7 | 14.3 | 19.3 | 71.7 | 70.6 | 87.4 | 9.2 | 24.8 |
| pull_request | all | 340 | 11.1 | 13.4 | 19.1 | 71.7 | 61.7 | 86.2 | 9.2 | 24.0 |
| push | all | 67 | 35.4 | 88.9 | 254.7 | 638.5 | 126.9 | 358.0 | 8.0 | 9.3 |

`pull_request`, successful runs, n=110:

| family | runner | ran in | median s | p90 s |
|---|---|---|---|---|
| test shard 3.12 | ubuntu-latest | 109/110 | 480 | 584 |
| hub-rust / hub (Rust 1.85.0) | ubuntu-latest | 80/110 | 636 | 761 |
| hub-rust / hub (Rust stable) | ubuntu-latest | 80/110 | 612 | 707 |
| windows-native-acceptance | windows-latest | 109/110 | 316 | 352 |
| windows-affected-tests | windows-latest | 109/110 | 143 | 1124 |
| engine-rename-installer | ubuntu-latest | 109/110 | 93 | 106 |
| content-guard | ubuntu-latest | 110/110 | 70 | 79 |
| quickstart-smoke (windows-latest) | windows-latest | 109/110 | 56 | 71 |
| lint | ubuntu-latest | 109/110 | 44 | 51 |

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
printed a warning. The strict window lists one more run (123, against 122 in an
earlier capture) because a `push` run created inside the window finished
between the captures: `push` runs wait on the self-hosted
`trusted-runner-canary` job for hours (max 38,294 seconds). A window is only
reproducible once every run created in it has completed, so end a window well
before the capture, and treat `push` numbers as unstable.

### Caveats

- n is small in the strict window: 19 successful pull request runs and 20
  successful merge group runs.
- 2026-11-09 is a Monday and the window is mostly overnight in US time, so the
  mix of pull request sizes may differ from a weekday afternoon. This affects
  `windows-affected-tests`, whose duration follows the size of the selection.
- Re-runs are measured on their first attempt. 5 of the 74 pull request runs
  in the strict window were re-run. All 5 failed on the first attempt and passed
  later, so none is in the `successful` view. Their first-attempt wall times are
  10.2, 11.2, 14.6, 15.4 and 26.5 minutes. Counting every attempt they cost 97.9
  more runner minutes than their first attempts alone, and the headline runner
  minutes leave that out. `runner_minutes_all_attempts` holds the figure: the
  pull request `all` view has a median of 55.1 runner minutes on first attempts
  against 56.6 over every attempt. A flaky test therefore lowers the `successful` n and raises the `all` cost
  instead of appearing as a slow successful run. In the
  extended window 12 of 340 pull request runs were re-run.
- `trusted-runner-canary` runs on a self-hosted runner and counts in runner
  minutes (the report keeps a per-OS median, `self-hosted` included). It does
  not appear in successful `pull_request` or `merge_group` runs, so those
  numbers are unaffected. It dominates `push` runner and wall time.
- Hosted runner speed varies between runs. Compare medians and p90 across
  several runs, never a single run.
- The inventory's pytest count was collected on a clean checkout of the same
  commit as the CI baseline's merge base, without this PR's own test file.
- The Actions runs API returns at most 1,000 runs per query. The script slices
  the window into six hour queries, splits a slice that reaches the cap and
  retries it, and compares the rows it collected with `total_count`. No warning
  was printed for either window.

## Workstreams

Each lane adds its own file under `docs/adaptive-ci/` and one row to the Results
log. Add the PR link and move the status when a lane lands or stops.

| # | workstream | status | lane file |
|---|---|---|---|
| 1 | Test-audit pruning pilot on the grokbot subsystem | in progress | `docs/adaptive-ci/test-audit-grokbot.md` (to be added) |
| 2 | Windows affected-tests tail | in progress | `docs/adaptive-ci/windows-affected-tests-tail.md` (to be added) |
| 3 | `hub-rust` path filtering in the merge queue | in progress | `docs/adaptive-ci/hub-rust-merge-queue.md` (to be added) |
| 4 | Linux pull request test selection in shadow mode | in progress | `docs/adaptive-ci/linux-selection-shadow.md` (to be added) |

<!-- adaptive-ci: add a workstream row above this line -->

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

## Results log

Append one row per measured change. Use the same units as the baseline: minutes
for wall and runner time, seconds for a job family.

| date | change | PR | metric | before | after | n |
|---|---|---|---|---|---|---|
<!-- adaptive-ci: append result rows directly below this line -->

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
