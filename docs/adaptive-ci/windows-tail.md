# Windows PR tail: findings and fix

The `windows-affected-tests` check on pull requests is usually fast and
sometimes very slow. This note records what the data showed, what changed, and
what was measured. It covers PR runs from 2026-11-02 to 2026-11-09 and the
`windows-affected-results` artifacts those runs retained (seven days).

## What the data showed

Sample: 197 PR runs of `ci.yml`, 168 with a Windows job that started. Artifacts
were available for the 82 runs that ran at least four minutes.

| Measure | Value |
| --- | --- |
| Windows jobs that finished (success or failure) | 102 |
| Median, p75, p90, max job duration | 2.7, 4.7, 19.0, 33.1 minutes |
| Finished jobs at or over 10, 15, 20 minutes | 18, 15, 8 |
| Share of finished-job minutes spent in jobs at or over 10 minutes | 376 of 593 (63%) |
| Windows job minutes spent in runs that were later cancelled | 574 of 1,166 (49%) |
| Artifact runs that selected 250 files or more | 44 of 82 |

The tail is not a flaky runner. It tracks selection size, and it is
reproducible: the same 326-file selection took 26.1, 26.4 and 26.6 minutes on
three separate runs (table below).

### Why selections get large

`scripts/windows_affected_tests.py` selects every test file whose imports touch
a changed `src/brigade` module, including the parent-package rule: a test that
imports package `P` is selected when any module under `P` changes. Attribution
of four large selections (static import analysis at each run's PR head):

| Run | Changed source modules | Selected | Matched only through a parent package import |
| --- | --- | --- | --- |
| 42562923914 | 2 (a `brigade.cli` leaf and `dispatch_memory`) | 284 | 282, of which 279 import `brigade.cli` |
| 42550748859 | 13 | 326 | 295 of 326 (259 import `brigade.cli`, 117 `brigade`, 106 `brigade.aboyeur`) |
| 42530200940 | 8 | 116 | 37 |
| 42572433971 | 7 | 126 | 50 |

A two-module change selected 284 files because 279 of them run
`from brigade.cli import main`. Those tests can reach a leaf command module
without importing it statically, by dispatching through `main`. Dropping the
parent-package rule would drop exactly the tests that exercise a changed
command end to end, and I could not show that any of them is unaffected. The
rule is unchanged. Dynamic and transitive coverage stays with the nightly
`windows-observe` run.

### Where wall time goes

For the 18 large complete runs (median 288 selected files, 13.5 to 33.1
minutes), summed per-file seconds ran 3,263 to 8,731 and the driver finished
in a wall time that implies a steady 5.2 to 5.3 concurrent files out of six
workers. A hosted Windows runner has four cores, so the job is throughput
bound. The other findings:

- Order and tail are minor. In a list-scheduling simulation over the artifacts,
  alphabetical start order costs about one to one and a half minutes per
  runner against a longest-first order.
- The serial phase is real. When `test_aboyeur.py` is selected it runs alone
  first and takes 170 to 300 seconds (it is an allowlisted failure that fails
  slowly), while five workers sit idle. That was 8 of the 18 large runs.
- Allowlisted failures consume 39.6% of summed seconds in the 18 large runs
  (36% in the 326-file run: 84 files, 3,165 seconds). Their allowlisting is
  not touched here. Twelve allowlisted files passed in every sampled run
  (`test_pre_push_hook.py`, `test_fleet_claims.py` and `test_run_lifecycle.py`
  cost the most), so they are removal candidates for a separate PR that shrinks
  the allowlist.
- Fixed per-job overhead is small: 80 to 130 seconds for checkout, Node,
  Python, install, temp configuration and the SQLite probe. Selection itself
  took 10 to 11 seconds on the Windows runner.
- A 497-file selection (the range of 40 recent main commits, run offline) is
  estimated at 33 minutes in one job. The driver stops at 38 minutes
  (`--job-timeout 2280`), and the same files varied up to 2x between runners in
  the experiment below, so a selection that size can run out of budget and
  report incomplete coverage. Splitting removes that failure mode too.

## What changed

One serial runner cannot go faster, so a large selection now spreads across up
to three hosted Windows runners. No file is dropped: every selected file still
runs exactly once, the allowlist and ratchet are untouched, and the driver
policy flags are the same per runner.

- `windows-affected-plan` (Ubuntu, a few seconds) runs the selector once on the
  test merge and writes a shard plan. Selection moved off the Windows runner.
  The eight fleet files that a `scripts/windows_pytest.py` change adds moved
  from PowerShell in the workflow into the selector, with a test.
- `windows-affected-shard (n/N)` is a matrix over the plan. N is 1 for small
  selections, so the common case still uses one runner. N is
  `ceil(estimated wall seconds / 480)`, capped at 3. Each runner starts its
  heaviest files first (`--keep-order`).
- `windows-affected-tests` keeps the existing check name as a small aggregator
  that fails unless the plan and every shard succeeded (or the selection was
  empty and every shard skipped). `ci-gate` is unchanged and does not need this
  job, as before.

Balancing uses `scripts/windows_test_weights.json`, mean measured seconds for
the 156 files that average ten seconds or more, with a default for the rest.
Serial files count as wall time, parallel files at the measured 5.2 concurrency.
Weights only affect which runner a file lands on and its start order. A
missing or stale table costs balance, never coverage. Refresh it from any set
of driver records:

```bash
python scripts/windows_affected_tests.py weights summary-1.json summary-2.json
```

## Measured before and after

Same forced selection on both sides: the 326 files of run 42550748859, run on
real hosted Windows runners from throwaway branches that ran only this job. The
Before arm is the unmodified job from `main`. The After arm is the plan job
plus three shard jobs from this change, copied from `ci.yml`. In every run
242 files passed, 84 allowlisted files failed, and there were no regressions or
infrastructure rows, on both sides.

| Arm | Run | Files | Wall minutes | Summed file seconds | Slowest, fastest runner (minutes) |
| --- | --- | --- | --- | --- | --- |
| Before (one job) | 42645648087 | 326 | 26.4 | 6,927 | n/a |
| Before (one job) | 42648412557 | 326 | 26.6 | 6,870 | n/a |
| Before (one job) | 42648616329 | 326 | 26.2 | 6,857 | n/a |
| After (3 shards) | 42645815436 | 326 | 13.9 | 8,081 | 13.5, 6.1 |
| After (3 shards) | 42648829040 | 326 | 13.9 | 7,403 | 12.3, 7.9 |
| After (3 shards) | 42649018444 | 326 | 13.2 | 7,590 | 12.3, 8.4 |

Wall minutes run from the first job start to the last job end. Before averages
26.4 minutes and After averages 13.7, a 48% cut (n=3 each, measured in CI).

Things that did not improve or got worse:

- The shards are not evenly balanced. The slowest runner took 12.3 to 13.5
  minutes against 6.1 to 8.4 for the fastest, though the plan splits the
  estimate evenly. The same files took up to twice as long on one runner as on
  another, and the slow runner changed between runs, so this is host variation
  and not a systematic plan error. The offline replay predicted 11.7 minutes
  for this selection, 2 minutes under the measured mean.
- Runner minutes rose: about 28 to 33 shard minutes plus the plan job against
  26.4 for one job. That is the fixed 1.5 to 2 minute setup paid three times.
- Summed file seconds are higher after (7,403 to 8,081 against 6,857 to 6,927).
  Individual files vary between runners by more than the effect of splitting.

Offline replay across the 33 complete artifact runs (no CI), predicting
`max(shard) + 1.6 minutes` of overhead with the observed per-file seconds:

| Selection size | Runs | Before (actual job minutes) | After (predicted) | Shards |
| --- | --- | --- | --- | --- |
| under 60 files | 13 | 4.0 to 7.4 | 3.7 to 5.9 | 1 |
| 89 to 140 files | 7 | 6.5 to 25.5 | 5.8 to 13.2 | 1 to 3 |
| 277 to 326 files | 13 | 14.3 to 33.1 | 5.6 to 11.7 | 3 |

Small selections stay on one runner and are expected to be unchanged or a few
seconds faster, since selection moved off Windows. The extra Linux plan job adds
a few seconds of queue time before the Windows job can start.

## Follow-ups not done here

- Shrinking the allowlist: twelve files pass on every sampled run and about 40%
  of summed seconds go to allowlisted failures. The allowlist ratchet only
  permits removals, so this is a separate PR with its own Windows evidence.
- A fourth runner or finer balancing, if the slow-runner spread keeps showing
  up. Per-runner time for identical files varied by up to 2x here.
