# Shadow-mode test selection for the Linux shards

Status: observation only. This change skips no test and changes no required
check. Every pull request still runs the whole Linux suite in the four
`test-shards` jobs, and the merge queue still runs the full matrix on
3.11, 3.12 and 3.13. The new jobs record what a selective run would have
skipped and whether any failing test would have fallen outside that
selection. That record is the evidence needed before anyone decides to turn
skipping on.

## Why shadow first

A selector is only worth enabling if it is cheap and almost never wrong, and
neither is known until it has run against real pull requests. The design rules
come from published practice (Meta's predictive test selection is calibrated
to more than 0.95 test recall and more than 0.999 change recall). OpenClaw's
adaptive CI starts in shadow mode, treats incomplete evidence as a reason to
run everything, and lets heuristics only add tests. The rules here follow that:

- Fail open. Any error, any run-everything trigger, and any changed path the
  selector cannot map yields `mode: full`.
- Incomplete evidence never authorizes a skip, and an incomplete observation is
  never counted as a clean pass by the evaluator.
- Every selected file carries a reason.
- Heuristics may add tests. Nothing removes a test the rules selected.

## What runs

Pull requests only (`merge_group` and `push` are unchanged):

| Piece | Where | What it does |
| --- | --- | --- |
| `shadow-plan` job | `ci.yml`, needs `changes` | Runs `scripts/ci_test_selection.py` on the merge commit's first-parent diff and uploads `shadow-plan`. Stdlib Python, no install. |
| junit in each 3.12 shard | `test-shards` | The coverage step adds `--junitxml` (xunit1, so every testcase carries its `file`) through a `$SHADOW_JUNIT` argument that is empty outside pull requests. A non-blocking step uploads `shadow-junit-<shard>`. |
| `shadow-selection` job | `ci.yml`, needs `changes`, `shadow-plan`, `test-shards` | Runs `scripts/ci_shadow_report.py`, uploads `shadow-selection`, and prints a one-line summary to the job summary. |

Neither new job is in `ci-gate`'s needs, both are `continue-on-error`, and no
other job waits on them. The junit upload step is the only
`continue-on-error` step in `test-shards`, so an artifact outage cannot fail a
required shard. `tests/test_ci_workflow.py` pins all of this.

The report job never fails the workflow, and it does not hide problems: a
missing plan, a missing or unreadable junit file, a failing testcase with no
`file`, or a shard that failed with no attributable test failure each set
`status: incomplete` and add an entry to `errors` in the artifact. If the
script cannot start at all, the step writes `status: error`.

## The plan

`scripts/ci_test_selection.py` builds a static import graph over `src/brigade`,
`scripts` and `tests`, using the same `module_name` and `imports_changed`
primitives as the Windows lane (now in `scripts/ci_selection_core.py`). Windows
selection behavior is unchanged and its tests are untouched.

Edges: every `import` and `from` import (including in-function imports and
relative imports), `brigade.x` string references (as used by
`monkeypatch.setattr("brigade.x.y", ...)` and command tables), computed imports
resolved to the modules the same file names as strings (else to everything
under the constant prefix), and conftest imports for every test below it. Tests
that launch the `brigade` console script depend on `brigade.cli`.

Each edge is import-time or deferred (inside a function body in src or
scripts). A selected test records the fewest deferred imports on any path to a
changed module as `closure:K`. One plan can therefore score three policies:

| Policy | Keeps |
| --- | --- |
| `import-time` | everything except `closure:1` and deeper |
| `lazy-1` | everything except `closure:2` and deeper |
| `full-closure` | everything (the plan's `selected` list) |

Reasons: `changed` (a changed test file), `imports` (a test imports a changed
module), `closure:K`, `references:<path>` (the test's text names a changed
module path, script, doc, workflow or changelog fragment, its file name, or its
top-level directory), and `invariant` (whole-tree tests no import edge
predicts: module size ratchet, mypy override ratchet, POSIX facade guard,
phase 257 facades, verify skill, retired engine names).

### Run-everything triggers (`mode: full`)

| Trigger | Reason string |
| --- | --- |
| `pyproject.toml`, `.github/workflows/ci.yml`, `scripts/ci_pytest_shard.py`, the selector, its core and the report script, `src/brigade/__init__.py` | `shared-infra` |
| any `conftest.py` | `conftest` |
| anything under `tests/` that is not a test file (helpers, fixtures, data) | `test-support` |
| a non-Python file under `src/brigade` | `src-data` |
| a path outside `src/brigade`, `scripts`, `tests`, `docs`, `changelog.d`, `.github` and root `*.md` (engines, registry, packaging, root config) | `unmapped-path` |
| empty diff, no test files discovered, unparseable test, src or script file | `empty-diff`, `no-tests-discovered`, `unparseable-source` |
| any exception in the selector or the diff | `selector-error:<Type>`, `diff-error:<Type>` |

In full mode `selected` lists every test file with reason `full`, and the
artifact keeps the triggering paths so the evaluator can show which areas to
map next.

## The artifact

`shadow-selection.json` (artifact `shadow-selection`, default retention):
`status` (`ok`, `incomplete`, `error`), `errors`, `mode`, `full_reasons`,
`full_paths`, `changed_count`, `test_files_total`, `tests_run`,
`total_test_seconds`, `failing_files`, `failing_test_count`,
`unattributed_failures`, per-file junit seconds, shard counts (expected, found,
job result), run id, attempt, sha, and for each policy: `selected_files`,
`selected_fraction`, `selected_seconds`, `escape_files` and `escape_nodeids`.
An escape is a failing test file outside the policy's selection. A failure
that is not caused by the pull request (a flaky test, a break on the base) is
still recorded as an escape, on purpose: the record is triaged when read.

## Evaluating

```bash
python scripts/ci_shadow_evaluate.py --gh-runs 200        # pulls artifacts with gh
python scripts/ci_shadow_evaluate.py --dir ./artifacts    # downloaded artifacts
```

It reports runs by status, the full-mode rate and its reasons and most frequent
paths, and per policy: median selected fraction, escape runs and the escape
rate per failing selective run, and the would-be test-seconds share. The
seconds are summed junit testcase durations. They are not wall time: shards run
in parallel with four xdist workers each, and install and setup time is not in
them, so the share bounds the saving in test work and does not predict job
latency.

## Backtest

Method: the 77 failed `ci.yml` pull request runs created from 2026-10-26 on
(about two weeks before this change) were checked. 38 had a failing 3.12 test
shard. For each, the failing test files came from the shard log's short test
summary, the tree was rebuilt by merging the run's head into the main commit
current when the run started (5 runs conflicted and were skipped), and the
selector ran on that diff, as CI would. The other 39 failed runs failed in
lint, content guards or other non-test jobs and say nothing about selection.

| Measure | import-time | lazy-1 | full-closure |
| --- | --- | --- | --- |
| failed PR runs evaluated | 33 | 33 | 33 |
| of those, full mode (no skip possible) | 16 | 16 | 16 |
| selective-mode runs | 17 | 17 | 17 |
| selective runs with an escape | 6 | 2 | 1 |
| median selected fraction, selective runs | 20% | 100% | 100% |

Escapes by example, all file names are test files:

- import-time only: `test_dispatch_memory`, `test_coverage_sidecar_guard`,
  `test_verify_script`, `test_grokbot_obsidian_excalidraw_client` each failed
  through a deferred import or one that the static graph reaches only through a
  function body. These are why `import-time` alone is not defensible.
- import-time and lazy-1: `test_component_install` needs two deferred imports
  to reach the changed module.
- all policies: `test_daily_driver_cmd` failed with `KeyError: 'job_count'` on a
  pull request that changed three other test files. Nothing the selector
  can see links them. It is either a failure that came from the base or an
  order or timing dependence. This one was not traced further.

One more escape was found and fixed during the backtest: `test_retired_engine_names`
scans the whole tree, so a new doc or source file can fail it with no import
edge. It is now an `invariant` test. Escapes that were found and then fixed are
not evidence the selector is complete: they show the invariant list needs to be
grown from data, and only the live record can show what is still missing.

Selection fraction across the 262 first-parent commits merged to main in the
same window (diffs applied to the current tree, so approximate): 50% run in full
mode. The median over all of them is 100% for every policy. Among the 54 to 111
that select less than everything the median selection is 18% for `import-time`,
12% for `lazy-1` and 4% for `full-closure`. Share of pull requests that select
at most a quarter of the files: 32%, 18% and 14%.

Reading: with the conservative policies the useful saving is real for a minority
of pull requests (small, leaf-level changes) and the PRs that fail tests are
disproportionately the broad ones, where nothing is saved. The majority of
engine, registry and packaging PRs are `unmapped-path`, which is the main lever
for improving this and the first thing the live record will rank.

## Overhead

Measured on this change's own pull request runs (two runs, four 3.12 shards
each):

| Step | Seconds |
| --- | --- |
| `shadow-plan` job, wall (parallel to the shards, not on the critical path) | 9 |
| `shadow-selection` job, wall (after the shards, not required by `ci-gate`) | 10 to 14 |
| `Upload shadow junit` step per shard | 1 to 2 |
| junit file written by pytest, per shard | about 280 KB |
| selector alone, local, on 830 test files | 7 to 8 |

The junit write itself is the only change inside a required job. A local A/B on
a 1/16 slice of the suite (about 3,500 tests, `-n 4`, warm cache) ran 62 s
without junit and 64 to 67 s with it, roughly 1 ms per test, which is about
10 to 15 s on a full shard if it scales linearly. In CI the `Pytest shard with
coverage` step took 454, 552, 406 and 460 s on this change's run against a
median of 485 s (range 316 to 594 s, 48 shard jobs from 12 recent successful
pull request runs): the junit cost is inside the run-to-run spread and cannot be
resolved from it. The step-level difference is therefore reported as not
measurable in CI, with the local figure as the estimate. Total added runner time
per pull request is about 30 seconds across the new jobs and uploads.

## Decision rule for ever skipping tests

Skipping is a separate decision made from the evaluator's output, never from
this document. Proposed minimum, per policy:

1. At least 100 complete (`status: ok`) selective-mode pull request runs that
   have failing tests, after the last change to `scripts/ci_test_selection.py`.
   Any selector change resets the count. With zero escapes in 100 runs the 95%
   upper bound on the escape rate is about 3% per failing run, which is the
   most this rule can claim.
2. Zero unexplained escapes. Each escape is triaged: a failure that also fails
   on the base or that a rerun clears is recorded as not caused by the pull
   request. A real miss resets the count.
3. A median would-be test-seconds share low enough to matter (for example at
   most half), and a full-mode rate whose causes have been reviewed.
4. The merge queue full run and a scheduled full run stay forever. A failure
   there of a test a pull request run would have skipped counts as an escape
   after the fact.
5. First enablement skips nothing required: run the selected set as an extra
   leg for one policy and keep the full run as the required check until the
   record shows no divergence.

## Not in scope

- Dynamic dependency maps (per-test coverage contexts, as pytest-testmon does)
  would be more precise than static imports. They need a new dependency or
  coverage overhead, so they need the owner's approval first.
- The earlier `pr-tier-shadow` trial (see `docs/pr-tier-shadow-trial.md`)
  re-ran selected tests in a second process and scraped job logs. This change
  reuses the shard run and junit, so it adds no test execution. That trial's
  window has ended. Removing its job is a separate change.
- Windows selection is untouched.
