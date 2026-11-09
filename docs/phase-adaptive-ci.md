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
  minutes, but its p90 is 26.5 minutes and the worst run took 33.9. The long
  runs are the `windows-affected-tests` job: median 196 seconds, p90 1,561
  seconds, max 1,986 seconds. It is short when the selection is small and long
  when it is large.
- **The merge queue repeats work.** Every merge group runs the Rust hub suite
  (`hub-rust`) on two toolchains of about 12 to 15 minutes each, whatever the
  change touched, plus the full Linux and Windows jobs. A queue entry costs a
  median of 123.6 runner minutes against 76.1 for a pull request run.

The goal is a smaller, better targeted suite and cheaper gates, with each
saving measured against the baseline below and each safety net kept or replaced
by something at least as strong.

## Metrics

All definitions match `scripts/ci_timing_report.py`. Its module docstring is the
source if the two ever disagree.

- **Window.** Runs whose `created_at` is inside `--since` and `--until` (UTC,
  inclusive) and whose status is `completed`, for one workflow file.
- **Successful view.** Runs with conclusion `success`. This is the headline
  view. **All view.** Every completed run, including `failure` and `cancelled`.
  Cancelled runs are common here because a newer push cancels the older run.
- **Wall minutes.** `run_started_at` to `updated_at` of the latest attempt. It
  excludes time queued before the run started and does not add the time of an
  earlier attempt of a re-run.
- **Runner minutes.** Sum of `completed_at - started_at` over the latest
  attempt's jobs that were not skipped. Cancelled jobs count, because they held
  a runner, and each matrix job counts. The sum measures cost. Wall minutes
  measure latency.
- **Windows runner minutes.** The same sum restricted to jobs whose first
  runner label starts with `windows`. Reported apart because GitHub weights
  Windows minutes at twice Linux when minutes are billed. The report also keeps
  a median per OS (`linux`, `windows`, `macos`, `self-hosted`, `other`).
- **Percentiles.** Linear interpolation between order statistics (numpy's
  default method). `p75`, `p90` and `max` are over runs, or over job instances
  for job families.
- **Job family.** Matrix names are normalized: a trailing integer shard index is
  dropped (`test shard (3.12, 1)` becomes `test shard 3.12`) and a free-text
  argument group is dropped (`install-from-source (x, --depth y)` becomes
  `install-from-source`). Other names stay whole, so the two `hub-rust` toolchain
  jobs remain separate. For each family the report records how many runs it ran
  in (`runs_with`, `ran_fraction`), its job instances, and median, p90 and max
  seconds.
- **Re-runs.** The jobs API returns the latest attempt only. `run_attempt` is
  stored per run and the number of re-run runs is counted per event.
- **Privacy.** The report stores run ids, SHAs, events, job names, runner labels
  and numbers. It does not store user names, branch names or PR titles.

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
2026-11-09T12:00:00Z, 122 completed runs.

| event | view | n | wall median | wall p75 | wall p90 | wall max | runner median | runner p90 | Windows runner median | Windows runner p90 |
|---|---|---|---|---|---|---|---|---|---|---|
| merge_group | successful | 20 | 14.9 | 15.2 | 15.4 | 15.8 | 123.6 | 130.3 | 12.2 | 13.5 |
| merge_group | all | 29 | 14.9 | 15.2 | 15.4 | 15.8 | 126.7 | 133.9 | 12.4 | 13.6 |
| pull_request | successful | 24 | 14.6 | 15.4 | 26.5 | 33.9 | 76.1 | 84.7 | 9.8 | 32.3 |
| pull_request | all | 74 | 10.1 | 14.4 | 16.2 | 33.9 | 55.6 | 78.9 | 10.8 | 22.3 |
| push | all | 19 | 9.0 | 25.5 | 110.5 | 135.7 | 91.3 | 199.3 | 7.7 | 8.9 |

All 19 `push` runs in the window were cancelled, so there is no `push`
baseline. The `all` rows include cancelled runs, which end early, so they read
lower than the `successful` rows. Compare successful runs.

Job families that dominate runner time in successful runs (median seconds,
p90 seconds, share of runs the job ran in):

`pull_request`, successful runs, n=24:

| family | runner | ran in | median s | p90 s |
|---|---|---|---|---|
| test shard 3.12 | ubuntu-latest | 24/24 | 474 | 590 |
| hub-rust / hub (Rust 1.85.0) | ubuntu-latest | 12/24 | 867 | 896 |
| hub-rust / hub (Rust stable) | ubuntu-latest | 12/24 | 752 | 838 |
| windows-native-acceptance | windows-latest | 24/24 | 308 | 338 |
| windows-affected-tests | windows-latest | 24/24 | 196 | 1561 |
| engine-rename-installer | ubuntu-latest | 24/24 | 97 | 108 |
| content-guard | ubuntu-latest | 24/24 | 66 | 81 |
| quickstart-smoke (windows-latest) | windows-latest | 24/24 | 56 | 59 |
| lint | ubuntu-latest | 24/24 | 44 | 52 |

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

Window 2026-11-05T15:00:00Z to 2026-11-09T12:00:00Z, 495 completed runs. Times are minutes.

| event | view | n | wall median | wall p75 | wall p90 | wall max | runner median | runner p90 | Windows runner median | Windows runner p90 |
|---|---|---|---|---|---|---|---|---|---|---|
| merge_group | successful | 70 | 11.5 | 13.4 | 15.1 | 15.8 | 120.5 | 125.3 | 12.6 | 13.9 |
| merge_group | all | 88 | 11.6 | 13.9 | 15.1 | 15.8 | 120.5 | 129.1 | 12.6 | 13.8 |
| pull_request | successful | 120 | 11.7 | 14.5 | 19.3 | 71.7 | 70.6 | 87.4 | 9.2 | 25.3 |
| pull_request | all | 340 | 10.8 | 13.2 | 19.0 | 71.7 | 62.5 | 86.0 | 9.2 | 24.1 |
| push | all | 67 | 35.4 | 88.9 | 254.7 | 638.5 | 126.9 | 358.0 | 8.0 | 9.3 |

`pull_request`, successful runs, n=120:

| family | runner | ran in | median s | p90 s |
|---|---|---|---|---|
| test shard 3.12 | ubuntu-latest | 119/120 | 478 | 586 |
| hub-rust / hub (Rust 1.85.0) | ubuntu-latest | 86/120 | 636 | 764 |
| hub-rust / hub (Rust stable) | ubuntu-latest | 86/120 | 617 | 713 |
| windows-native-acceptance | windows-latest | 119/120 | 315 | 354 |
| windows-affected-tests | windows-latest | 119/120 | 145 | 1138 |
| engine-rename-installer | ubuntu-latest | 119/120 | 93 | 106 |
| content-guard | ubuntu-latest | 120/120 | 70 | 79 |
| quickstart-smoke (windows-latest) | windows-latest | 119/120 | 56 | 68 |
| lint | ubuntu-latest | 119/120 | 43 | 51 |

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
| engine-rename-installer | ubuntu-latest | 70/70 | 96 | 110 |
| build-and-test | ubuntu-latest | 70/70 | 92 | 113 |

### Reproducibility check

The timing script was run twice over the strict window, with the final script.
The two JSON files were identical apart from `generated_at` (`diff` after
removing that line). The extended window was run twice as well, the second time
with `--aggregates-only`. Its `pull_request` and `merge_group` aggregates were
identical. Its `push` aggregates differ by one run (66 against 67): a `push` run
created inside the window finished between the two captures, because `push`
runs wait on the self-hosted `trusted-runner-canary` job for hours (max 38,294
seconds). The committed extended file is the second capture. A window is only
reproducible once every run created in it has completed, so end a window well
before the capture, and treat `push` numbers as unstable.

### Caveats

- n is small in the strict window: 24 successful pull request runs and 20
  successful merge group runs.
- 2026-11-09 is a Monday and the window is mostly overnight in US time, so the
  mix of pull request sizes may differ from a weekday afternoon. This affects
  `windows-affected-tests`, whose duration follows the size of the selection.
- Re-runs distort one run's wall time. The jobs API lists the latest attempt's
  jobs, and a re-run that executes only the failed jobs reports a short wall
  time next to carried-over job durations. 5 of the 24 successful pull request
  runs in the strict window have `run_attempt` above 1. Restricted to first
  attempts (19 runs), the pull request wall time is median 14.7, p75 17.3, p90
  26.6, max 33.9 minutes and runner time is median 75.7, p90 83.8, so the
  headline numbers hold. Filter on `run_attempt` in the raw JSON for that view.
- `trusted-runner-canary` runs on a self-hosted runner and counts in runner
  minutes (the report keeps a per-OS median, `self-hosted` included). It does
  not appear in successful `pull_request` or `merge_group` runs, so those
  numbers are unaffected. It dominates `push` runner and wall time.
- Hosted runner speed varies between runs. Compare medians and p90 across
  several runs, never a single run.
- The inventory's pytest count was collected on a clean checkout of the same
  commit as the CI baseline's merge base, without this PR's own test file.
- The Actions runs API returns at most 1,000 runs per query. The script slices
  the window into six hour queries and warns on stderr when a slice reaches the
  cap. No warning was printed for either window.

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
A window of one day takes about two minutes and a four day window about twenty.
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
