# Hub suite selection in the merge queue

Part of the adaptive CI work. This page covers one change: the merge queue now
selects `hub-rust` with the same `hub` path filter that pull requests and pushes
already use, and a nightly workflow runs the full hub suite on `main`.

## Before

- `hub-rust` calls the reusable `.github/workflows/hub.yml`: `hub (Rust stable)`,
  `hub (Rust 1.85.0)` and `hub (linux-arm64)`.
- In `ci.yml`, the `changes` job skipped `dorny/paths-filter` for `merge_group`
  and forced every component output, `hub` included, to `'true'`.
- `ci-gate` refused a skipped `hub-rust` in a merge group, even with
  `hub == 'false'`.
- Every queue entry that was not documentation-only ran the hub suite. Each
  x86 toolchain job took a median 10.8 minutes and the arm64 build 1.0 minute
  (180 and 30 jobs).
- Nothing scheduled ran the hub suite. A new stable toolchain lint surfaced only
  when someone touched a hub path.

## Change

1. The existing filter step (`id: filter`) now also runs in merge groups. Its
   `base` and `ref` inputs are `github.event.merge_group.base_sha` and
   `github.event.merge_group.head_sha`. Both expressions are empty on
   `pull_request` and `push`, so those events keep their current diff source.
   In a merge group the step runs only when the trusted classifier reported
   `uncertain == 'false'`.
2. The `hub` output is now:

   ```text
   steps.docs.outputs.uncertain != 'false' && 'true'
   || steps.filter.outcome != 'success' && 'true'
   || steps.filter.outputs.hub == 'false' && 'false'
   || 'true'
   ```

   It is `'false'` only when classification is certain, the filter step
   succeeded, and the filter reported exactly `'false'`. Every other state
   selects the suite. The other outputs (`mesh`, `code_graph`,
   `evidence_ledger`, `notify`, `fleet_hub_container`) are unchanged and still
   forced to `'true'` in merge groups.
3. `ci-gate` drops the `EVENT_NAME != 'merge_group'` clause from
   `hub_skip_allowed`. A skipped `hub-rust` passes only when `changes` succeeded
   and its `hub` output is exactly `'false'`, on every event. No other gate rule
   changed.
4. The filter action is pinned to the commit for `v4.0.3`
   (`ceb8a2b8f2d89434be7ff52d3de7ec3738c5cc9d`). In a merge group it now decides
   whether a required suite may skip, so a moved tag must not change that.
5. `.github/workflows/hub-nightly.yml` calls `hub.yml` at 04:30 UTC daily and on
   `workflow_dispatch`, on `main` only. A failure shows as a failed scheduled run
   in the Actions tab.

The path list stays in one place: the `hub:` block of the filter step in
`ci.yml`. That list already includes `.github/workflows/hub.yml` and
`.github/workflows/ci.yml`, so a change to either workflow selects the suite.

## What the merge-group diff covers

GitHub sets `merge_group.base_sha` to the parent of the entry's merge commit.
For an entry queued behind another, that parent is the earlier entry's head, not
the current `main` tip. In the measured window all 204 merge-group runs were
exactly one commit ahead of `base_sha` (compare API `ahead_by == 1`,
`behind_by == 0`). The lint log of run 42576432742 shows `EVENT_BASE_SHA` equal
(`b4eb7f4f3`) equal to the head of the entry ahead of it. When that run
started, `main` was at `a9204e9a6`, two entries behind.

So each entry's filter sees its own change. That is safe for the same reason
path filtering is safe on pull requests: an entry that touches a hub path runs
the hub suite on its own merge commit, with every earlier entry included. If
that run fails, GitHub removes the entry and rebuilds the later groups on a new
base, which creates new runs. An entry that skips the suite changed no hub path
relative to a tree whose hub paths were already tested. If the queue is later
configured to batch several pull requests into one group, the same
`base_sha..head_sha` diff covers the whole batch.

## Fail-closed cases

| Case | `hub` output | Gate result |
| --- | --- | --- |
| Classifier missing, invalid, or erroring (`uncertain` not `'false'`) | `'true'` | suite runs and must pass |
| Unsupported event, invalid SHA, or checkout mismatch (classifier marks uncertain) | `'true'` | suite runs and must pass |
| Merge group with `uncertain != 'false'` (filter step skipped) | `'true'` | suite runs and must pass |
| Filter step fails (fetch error, missing history, action error) | job fails | `changes` is not `success`, so `ci-gate` fails |
| Filter output empty or any value other than `'false'` | `'true'` | suite runs and must pass |
| `changes` fails or is cancelled | not trusted | `ci-gate` fails (`changes` is mandatory) |
| `hub == 'false'` but `changes` did not succeed | n/a | skip rejected |
| Docs-only entry | unchanged | existing docs-only skip rule |

History: the checkout keeps `fetch-depth: 2`. Before the filter runs in a merge
group, the classifier's `ensure_commit` has fetched `base_sha` and verified that
`head_sha` equals `GITHUB_SHA` and the checked-out `HEAD`. The filter step
compares two commits that are both present (`git diff base..head`), and fetches
a missing SHA with `--depth=1` on its own. A missing object fails the step,
which fails `changes`.

Trust: the path list comes from `ci.yml` at the merge-group head, the same
source pull requests already use. A pull request that edits `ci.yml` or
`hub.yml` matches the `hub` filter and runs the suite.

Tests in `tests/test_ci_workflow.py` evaluate the literal `hub` expression over
every combination of event, `uncertain`, filter outcome and filter output. They
also run the gate program with `hub-rust` skipped for each event and each
`changes` result.

## Before and After

### Before (measured)

Source: every `ci` run with `event=merge_group` created from 2026-11-02 through
2026-11-09 (204 runs). `hub-rust` joined `ci.yml` in #2304 on 2026-11-05, so
only runs from 2026-11-05T14:28Z to 2026-11-09T07:04Z (3.7 days) include it.
For each run, the base came from the queue branch name
(`gh-readonly-queue/main/pr-<n>-<base_sha>`), the changed files from the compare
API (no response hit the 300-file cap), and the job times from the run's jobs
API. Paths were matched against the 29 patterns in the `hub` filter, with renamed
files checked under both names.

| Measure | Value |
| --- | --- |
| Merge-group runs that ran `hub-rust` | 90 |
| Of those, no `hub` path in `base..head` | 32 (36%) |
| x86 hub job time in those 32 runs | 757.8 min (788 min with per-job minute rounding) |
| arm64 hub job time in those 32 runs | 18.9 min (29 min rounded) |
| Median x86 hub time per skippable run | 23.0 min |
| Runs where a hub path matched | 58 |

Those 32 entries would have skipped about 777 runner-minutes (n=32, over 3.7
days). The nightly adds one full suite per day: two x86 jobs at a mean 10.7
min each plus the 1.0 min arm64 build, about 22 runner-minutes a day or 157 a
week.

### After (to be measured)

PLACEHOLDER: the orchestrator fills this in from merge-group runs after this
change merges. Use the same script and window length: queue entries that ran
`hub-rust`, entries that skipped it, runner-minutes, and the nightly cost.
