# Hub suite selection in the merge queue

Part of the adaptive CI work. This page covers one change: a merge group may
now skip `hub-rust` when no hub path changed. A trusted selector, loaded from
`main`, decides. A nightly workflow runs the full hub suite on `main`.

## Before

- `hub-rust` calls the reusable `.github/workflows/hub.yml`: `hub (Rust stable)`,
  `hub (Rust 1.85.0)` and `hub (linux-arm64)`.
- In `ci.yml`, the `changes` job skipped `dorny/paths-filter` for `merge_group`
  and forced every component output, `hub` included, to `'true'`.
- `ci-gate` refused a skipped `hub-rust` in a merge group, even with
  `hub == 'false'`.
- Every queue entry that was not documentation-only ran the hub suite.
- Nothing scheduled ran the hub suite. A new stable toolchain lint surfaced only
  when someone touched a hub path.

## What a merge group must cover

The queue runs up to 3 builds at once with grouping `HEADGREEN`
([merge queue settings](../runbooks/ci-runner-pilot.md#merge-queue-settings)).
Entry B queued behind entry A is built on A's head, so B's head contains A + B.
GitHub sets `merge_group.base_sha` for B to A's head, not to `main`. With
`HEADGREEN` only the group head must pass, so a green B can merge A + B even
when A's own run failed or is still pending.

So `base_sha..head_sha` is not enough. If A changes a hub path and B does not,
B's `base_sha..head_sha` holds only B and would skip the hub suite, and A + B
could merge with A's hub change untested. The selector therefore diffs from the
merge base of the group head with the current `main` tip. That range contains
every speculative entry ahead of B.

Measured on all 208 `ci` merge-group runs from 2026-11-02 to 2026-11-09 for
which `main` at the start of the `changes` job is known (from the `ci` push
runs):

| Measure | Value |
| --- | --- |
| `merge-base(main, head)` equals `base_sha` (entry was first in line) | 129 |
| `merge-base(main, head)` sits below `base_sha` (entries ahead in line) | 79 |
| Earlier entries in `merge-base..base_sha` that `base_sha..head_sha` missed | 113 |
| Runs where those entries differ from the queue chain in the branch names | 0 |
| Runs where `merge-base(main, head)` is not an ancestor of `base_sha` | 0 |

The queue chain comes from the branch names
(`gh-readonly-queue/main/pr-<n>-<base_sha>`): follow each `base_sha` to the run
whose head it is, until a commit already on `main`. In every run the merge base
equals the root of that chain, and the pull request numbers in
`merge-base..base_sha` match the entries in the chain. Example: run
42648511830 (PR 2431) had `base_sha` `2a27f9a26`, while `main` was `595b995e7`.
The cumulative range also holds PRs 2430 and 2432.

When an earlier entry has merged, `main` is that entry's tested head and the
range shrinks to the entries still unmerged. The `main` tip only ever holds
commits that passed the queue, since the ruleset has no bypass actors.

## Change

1. **Trusted selector.** A new `changes` step, `hub_merge_group`, runs only in
   merge groups. It fetches `refs/heads/main` with a fixed refspec (never a
   ref from the event or the proposal), loads `scripts/ci_hub_select.py` from
   that commit with `git ls-tree` and `git cat-file`, requires a regular
   non-executable file of at most 128 KiB, and runs it with `python3 -I -S`.
   It is the first step after checkout, because the documentation-only
   classifier reuses its result in merge groups
   ([docs-classifier-trust.md](docs-classifier-trust.md)): the step also
   outputs the `main` commit it used (`target`) and the validated start of the
   cumulative range (`start`, empty on any failure). The fixed ref is
   `refs/ci-trusted/main`.
2. **What the selector does.** It checks the event (`merge_group`,
   `base_ref == refs/heads/main`, `head_sha == GITHUB_SHA == HEAD`), deepens
   history for `main`, the head and `base_sha` to 256 commits, and requires
   one merge base of `main` and the head that is an ancestor of `base_sha`. It
   loads `scripts/ci_hub_paths.yml` from the same `main` commit (regular file,
   at most 16 KiB, one strict format) and diffs `merge-base..head` with
   renames off, so a rename shows both names. It sets `hub=true` when any
   changed path matches the list, or when the range touches
   `.github/workflows/ci.yml`, `.github/workflows/hub.yml`,
   `.github/workflows/hub-nightly.yml`, `scripts/ci_hub_paths.yml` or
   `scripts/ci_hub_select.py`. That last rule lives in the selector's code,
   not in the list, and anything else sets `hub=false`.
3. **One path list.** The hub patterns moved out of the inline filter into
   `scripts/ci_hub_paths.yml`. Pull requests and pushes read it through a
   second `dorny/paths-filter` step (`hub_filter`, `filters:
   scripts/ci_hub_paths.yml`) with the same pinned action, no `base` or `ref`
   input, and the same `if: github.event_name != 'merge_group'` as the
   original filter step, so their diff source is unchanged. The merge-group
   selector reads the same file from `main`. The original `filter` step is back
   to its pre-change form apart from the pin.
4. **The `hub` output:**

   ```text
   steps.docs.outputs.uncertain != 'false' && 'true'
   || github.event_name == 'merge_group'
      && (steps.hub_merge_group.outputs.hub == 'false' && 'false' || 'true')
   || steps.hub_filter.outcome != 'success' && 'true'
   || steps.hub_filter.outputs.hub == 'false' && 'false'
   || 'true'
   ```

   In a merge group only the trusted selector's exact `'false'` skips the
   suite, and the proposal-evaluated filter is never consulted. On pull requests and
   pushes only a successful `hub_filter` reporting exactly `'false'` skips it.
   Every other state selects the suite. The other outputs (`mesh`,
   `code_graph`, `evidence_ledger`, `notify`, `fleet_hub_container`) are
   unchanged and still forced to `'true'` in merge groups.
5. **`ci-gate`** drops the `EVENT_NAME != 'merge_group'` clause from
   `hub_skip_allowed`. A skipped `hub-rust` passes only when `changes`
   succeeded and its `hub` output is exactly `'false'`.
6. **Path list additions.** The hub suite also runs files the old list missed.
   Added: `registry/skills/verify-brigade/**` (the whole skill, since
   `control-brigade.py` drives the contract runs), `src/brigade/fleet_client*.py`,
   `src/brigade/cli/fleet*.py`, `src/brigade/cli/__init__.py`,
   `src/brigade/__init__.py`, `src/brigade/__main__.py` (`python -m brigade`),
   `src/brigade/fleet_admin_stats.py` and `src/brigade/fleet_owners.py` (loaded
   by `hub_audit.py`), `pyproject.toml` (`pip install -e`), and the selection
   sources listed above.
7. **Nightly.** `.github/workflows/hub-nightly.yml` calls `hub.yml` at 04:30 UTC
   daily and on `workflow_dispatch`, on `main` only. On failure (or a timed-out
   or cancelled hub job in a run that was not itself cancelled) a separate
   `report-failure` job opens a `hub nightly failing` issue labelled
   `hub-nightly`, or comments on the open one. A later passing nightly runs
   `report-recovery`, which comments "hub-nightly recovered" with the run link
   on every open `hub-nightly` issue and closes each one. Only those two jobs have `issues: write`. `hub.yml` now sets `timeout-minutes` on both jobs (45 for
   the x86 toolchains against a measured max of 15.2 min, 30 for arm64), because a job
   that calls a reusable workflow cannot set its own limit.

## Coverage of the path list

`tests/test_ci_hub_selection.py` checks two things statically:

- every repo file `hub.yml` invokes (the `.py` scripts, the `unittest -s`
  directory, and the `pip install -e` project file) matches the list.
- every `brigade.*` module that `engines/hub/tests/*.py`,
  `engines/hub/bench/*.py` and `registry/skills/verify-brigade/*.py` import
  directly (including `importlib` names and `python -m brigade`), plus their
  parent packages, matches the list.

Transitive imports are not covered. A change to a module the hub tests reach
only through other modules can merge without the hub suite. The nightly full
run on `main` is the backstop for that, and for new toolchain releases.

## Fail-closed cases

| Case | `hub` output | Gate result |
| --- | --- | --- |
| Documentation classifier uncertain (`uncertain` not `'false'`) | `'true'` | suite runs and must pass |
| `main` cannot be fetched, or has no selector, or the selector is a symlink, executable, oversize or broken | `'true'` | suite runs and must pass |
| Selector output missing, partial, invalid, or with a repeated or unknown key | `'true'` | suite runs and must pass |
| `base_ref` not `refs/heads/main`, or head mismatch | `'true'` | suite runs and must pass |
| History fetch fails, no merge base within 256 commits (`no-merge-base`), several merge bases (`ambiguous-merge-base`), or merge base not below `base_sha` | `'true'` | suite runs and must pass |
| Path list missing, not a regular file, empty, over 16 KiB, or not in the one accepted format | `'true'` | suite runs and must pass |
| Range touches a workflow or a selection source | `'true'` | suite runs and must pass |
| `hub_merge_group` step skipped or errored in a merge group | `'true'` | suite runs and must pass |
| `hub_filter` fails on a pull request or push | `'true'` | suite runs and must pass |
| `changes` fails or is cancelled | not trusted | `ci-gate` fails (`changes` is mandatory) |
| Docs-only entry | unchanged | existing docs-only skip rule |

`tests/test_ci_hub_selection.py` runs the literal bootstrap from `ci.yml`
against a local origin and a shallow checkout, as `actions/checkout` leaves
it, for most of these rows.

## Trust and its limit

The selector and the path list come from the `main` tip, so a proposal cannot
change what selects the hub suite for its own merge group by editing the list
or the selector: the selector it runs is `main`'s, and any edit to those files
forces the suite.

The bootstrap step itself lives in `ci.yml`, and in a merge group GitHub runs
the head's `ci.yml`. A proposal that rewrites `ci.yml` can change any gate,
this one included, for example by writing `hub=false` directly. That reach is
wider than the proposal's own group. Every group queued behind entry A is
built on A's head, so it runs A's `ci.yml`: a workflow rewrite in A controls
the gates of every group behind A, not only A's own. With `HEADGREEN`, a green
group behind A merges A along with it, even when A's own group failed. No check
inside the workflow can prevent that, because the check would be part of the
rewritten file. The control for it is human review of workflow changes. As of
2026-11-09 that control is not enforced: the repository has no `CODEOWNERS`
file, and `main`'s protection requires 0 approving reviews, no code-owner
review, and only the `ci-gate` status (conversation resolution and admin
enforcement are on). Requiring a code-owner review for `.github/workflows/`
and `scripts/ci_*` would close that gap. This change does not alter protection
settings.

The documentation-only classifier had the same two problems as the first
version of this selector: it loaded its code from `merge_group.base_sha` (the
previous entry's head) and classified `base_sha..head_sha`. That is fixed:
in merge groups it now loads from the same `main` commit as this selector and
classifies the cumulative range this selector validates. The measured impact
and the gate logic are in [docs-classifier-trust.md](docs-classifier-trust.md).

## Before and After

### Before (measured)

Source: every `ci` run with `event=merge_group` created from 2026-11-02 through
2026-11-09. `hub-rust` joined `ci.yml` in #2304 on 2026-11-05, so the hub window
is 2026-11-05T14:28Z to 2026-11-09T07:04Z (3.7 days, 90 runs, all of which ran
`hub-rust`). For each run, `base_sha` came from the queue branch name, `main`
from the latest `ci` push run created before the run's `changes` job started,
the changed files from `git diff --no-renames`, and the job times from the jobs
API. Paths were matched with the selector's matcher.

| Range and list | Runs that could skip | x86 hub min | arm64 hub min |
| --- | --- | --- | --- |
| `base_sha..head_sha`, old 29-pattern list (first version of this PR) | 32 of 90 | 757.8 | 18.9 |
| cumulative `merge-base..head`, old list | 22 of 90 | 517.9 | 12.9 |
| `base_sha..head_sha`, new list and source rule | 27 of 90 | 627.5 | 14.1 |
| cumulative `merge-base..head`, new list and source rule (this change) | 18 of 90 (20%) | 419.2 | 8.9 |

The first version's 32 included 10 runs that would have skipped the hub suite
although an earlier queue entry in the same head changed a hub path. With the
cumulative range and the wider list, 18 entries would have skipped about 428
runner-minutes over 3.7 days (median 22.4 x86 minutes per skipped run). The
arm64 job joined part way through the window. Extending the window to
2026-11-09T15:49Z adds one run (19 of 91, 458.4 minutes).

The nightly adds one full suite per day: two x86 jobs at a mean 10.7 min each
plus the 1.0 min arm64 build, about 22 runner-minutes a day or 157 a week.

### After (to be measured)

PLACEHOLDER: the orchestrator fills this in from merge-group runs after this
change merges. Use the same method and window length: queue entries that ran
`hub-rust`, entries that skipped it, runner-minutes, the selector's `reason`
values from the `changes` log, and the nightly cost.
