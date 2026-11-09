# Documentation-only classification in the merge queue

Part of the adaptive CI work. The `changes` job in `ci.yml` decides whether an
event changed only documentation. When it says `docs_only=true`, `ci-gate`
accepts a skipped `lint`, `test`, `test-shards`, `coverage`, `hub-rust` and
the other jobs in its `docs_skips` set. This page covers how a merge group
reaches that decision. Pull requests and pushes are unchanged.

## Before

The `docs` step bootstrap loaded `scripts/ci_docs_only.py` from the event's
base commit and ran it with `python3 -I -S`. The classifier diffed that base
against the head. For a merge group the base was `merge_group.base_sha`.

The queue builds up to 3 groups at once with grouping `HEADGREEN`
([merge queue settings](../runbooks/ci-runner-pilot.md#merge-queue-settings)).
Entry B queued behind entry A is built on A's head, and GitHub sets B's
`base_sha` to A's head. That caused two problems.

1. **Trust root.** A's head is proposal code. If A changed
   `ci_docs_only.py` to always print `docs_only=true` and `uncertain=false`,
   every group queued behind A ran A's classifier. `ci-gate` then accepted the
   skipped runtime jobs, and with `HEADGREEN` a green group behind A merged A
   too, even when A's own group failed.
2. **Range.** `base_sha..head_sha` holds only B's change. A documentation-only
   entry B behind a failing code entry A was classified documentation-only and
   skipped every runtime job. Its green group could then merge A.

Pull requests and pushes do not have either problem. A pull request's base is
the first parent of GitHub's test merge, a commit on the base branch. A push's
`before` is the previous tip of `main`.

## Measured impact

Source: every `ci` run with `event=merge_group` created from
2026-11-02T13:42Z to 2026-11-09T16:14Z (209 runs). `base_sha` came from the
queue branch name (`gh-readonly-queue/main/pr-<n>-<base_sha>`). `main` came
from the latest `ci` push run on `main` created before the run's first
`changes` job started, which is known for 207 runs. Both ranges were classified
with the current `ci_docs_only.py` rules.

| Measure | Runs |
| --- | --- |
| Documentation-only by `base_sha..head_sha` | 5 |
| Of those, also documentation-only by `merge-base(main, head)..head` | 4 |
| Of those, not documentation-only by the cumulative range | 1 |

The one run is 41602345826 (PR 2217, a documentation change). Its `base_sha`
`1b1fa6e8b` was the head of PR 2216, which `main` (`703eeae63`) did not yet
hold. PR 2216 changed 11 files, among them `src/brigade/cli/run_cloud.py`.
PR 2216's own group, run 41602287988, failed `test (3.12)` and two test
shards. PR 2217's group ran 4 jobs, skipped 23 as documentation-only, and
passed. Both queue heads, `1b1fa6e8b` and `e26be3ca5`, are on `main`. Under
the cumulative range that group would have run the full suite.

## Change

1. **Step order.** `hub_merge_group` (the trusted hub selector from
   [hub-merge-queue.md](hub-merge-queue.md)) now runs first, right after
   checkout. It fetches `+refs/heads/main:refs/ci-trusted/main` (a fixed
   refspec, never a ref from the event), loads `scripts/ci_hub_select.py` from
   that commit, and runs it. The selector already validated the cumulative
   range for the hub decision. It now also outputs that range's start, and the
   bootstrap adds the `main` commit it used:

   ```text
   hub=<true|false>
   reason=<fixed string>
   start=<merge base of main and the head, or empty on any failure>
   target=<the refs/ci-trusted/main commit, or empty if it never ran>
   ```

   The bootstrap accepts the selector's output only when it has exactly three
   `key=value` lines, one each for `hub`, `reason` and `start`, and `start` is
   empty or 40 hex digits. A repeated or unknown key fails, so a repeated
   `hub=false` line cannot override an earlier `hub=true`. The docs step parses
   the classifier's output the same way.
2. **Classifier source.** The `docs` step maps
   `CI_TRUSTED_MAIN: steps.hub_merge_group.outputs.target` and
   `CI_MERGE_GROUP_START: steps.hub_merge_group.outputs.start` into its
   environment. For a merge group its bootstrap:
   - checks `merge_group.head_sha == GITHUB_SHA == HEAD`.
   - resolves `refs/ci-trusted/main` and requires it to equal
     `CI_TRUSTED_MAIN`, so the classifier comes from the commit the range was
     validated against. Nothing fetches the ref again between the two steps,
     so this is a consistency check between the selector's output and the
     ref, not a guard against `main` moving.
   - requires `CI_MERGE_GROUP_START` to be a 40-digit commit id.
   - loads `scripts/ci_docs_only.py` from that `main` commit with
     `git ls-tree` and `git cat-file`: a regular non-executable blob
     (mode `100644`) of 1 to 131072 bytes.
   - runs it with `python3 -I -S` and passes `CI_TRUSTED_MAIN` and
     `CI_MERGE_GROUP_START` only in a merge group.
3. **Classifier range.** For `merge_group`, `ci_docs_only.py` now requires
   `base_ref == refs/heads/main`, a valid `CI_TRUSTED_MAIN` and a valid
   `CI_MERGE_GROUP_START`. It checks that the start is an ancestor of `main`
   and of `base_sha`, and that `base_sha` is an ancestor of the head, and then
   diffs `start..head` instead of `base_sha..head`. The `main` check is what
   makes the range cumulative: `start == base_sha` passes the other two
   checks, and while entry A is not on `main` it would hide A's change. The diff rules (raw, NUL-delimited, renames off, modes
   `100644` only, documentation allowlist) are unchanged.
4. **Pull requests and pushes** keep their source and range. The bootstrap
   removes `CI_TRUSTED_MAIN` and `CI_MERGE_GROUP_START` from the classifier's
   environment for them.

The gate expressions are unchanged. Every job that `docs_skips` covers still
runs unless `needs.changes.outputs.docs_only` is exactly `'true'`, and every
component output is still forced to `'true'` whenever `uncertain` is not
exactly `'false'`.

## Fail-closed cases

Every row gives `docs_only=false`, `uncertain=true`, which runs full CI.

| Case | Where it fails | `reason` |
| --- | --- | --- |
| `main` cannot be fetched | hub step: `target` and `start` empty | `trusted-classifier-unavailable` |
| Hub selector missing, unsafe or broken on `main`, or its output invalid | hub step: `start` empty | `trusted-classifier-unavailable` |
| `base_ref` is not `refs/heads/main` | hub selector, and again in the classifier | `trusted-classifier-unavailable` or `unexpected-target-branch` |
| No merge base within 256 commits, or several | hub selector (`no-merge-base`, `ambiguous-merge-base`) | `trusted-classifier-unavailable` |
| Merge base not below `base_sha`, or `base_sha` not in the head | hub selector, and again in the classifier | `trusted-classifier-unavailable` or `cumulative-range-not-below-group-base` |
| Start not in `main`'s history (for example `start == base_sha` before entry A merges) | classifier | `cumulative-range-not-below-group-base` |
| `refs/ci-trusted/main` differs from the selector's `target` | docs bootstrap | `trusted-classifier-unavailable` |
| Classifier missing on `main`, a symlink, executable, over 128 KiB or broken | docs bootstrap | `trusted-classifier-unavailable` |
| Classifier output missing, partial, invalid, or with a repeated or unknown key | docs bootstrap | `trusted-classifier-unavailable` |
| `CI_TRUSTED_MAIN` missing (an older bootstrap) | classifier | `missing-trusted-main` |
| `CI_MERGE_GROUP_START` missing or not a commit id (an older bootstrap) | classifier | `missing-cumulative-range` |

When the hub selector fails it also selects the hub suite, so these cases cost
a full run and never skip anything.

The selector's own output is printed in the `changes` log between
`::stop-commands::<random token>` and `::<token>::`, so it cannot issue
workflow commands: its stdout on success, the last 2000 characters of its
stderr on failure.

## Tests

`tests/test_ci_docs_merge_group.py` runs the literal `hub_merge_group` and
`docs` steps from `ci.yml`, in order, against a local origin and a shallow
checkout:

- An entry queued behind a code change, whose own change is documentation, is
  not documentation-only.
- An entry ahead that rewrites `ci_docs_only.py` to force documentation-only
  never has its classifier run, and its change counts as code.
- Documentation behind documentation, and documentation behind a code entry
  that has since merged, still skip.
- Every row of the table above. The two rows only an older bootstrap can
  reach run the classifier directly.
- The selector's stdout and stderr both stay inside a `::stop-commands::`
  block.

Mutation checks: loading the classifier from `base_sha` again fails the
forged-classifier tests, diffing `base_sha..head` again fails the
queued-behind test, dropping the `main` ancestry check fails the
`start == base_sha` case, and parsing helper output with `dict()` again fails
the repeated-key cases.

## Trust and its limit

The limit in [hub-merge-queue.md](hub-merge-queue.md#trust-and-its-limit)
applies here too. In a merge group GitHub runs the head's `ci.yml`, so entry A
can rewrite this bootstrap, and that rewrite controls the gates of every group
queued behind A. This change removes the paths that did not need a workflow
edit: a changed classifier script, and a range that hid earlier entries.

During the merge of this change itself, `main` may still hold the previous
`ci_hub_select.py`, whose output has no `start`. The new bootstrap rejects
that output, so that one group runs full CI.
