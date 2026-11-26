# Workflow guard

GitHub Cloud has sourced `pull_request_target` workflows and their default
checkout from the repository's default branch since December 8, 2025.
The guard uses the pull request event's base ref and repository to admit the
default branch and the exact `release/0.32` maintenance branch for 0.32 patch
releases. It checks GitHub's changed-file list and both paths of a rename.
[GitHub's rollout notice](https://github.blog/changelog/2025-11-07-actions-pull_request_target-and-environment-branch-protections-changes/).

The guard protects the following paths.

- GitHub Actions workflows and any repository-local actions under `.github/`.
- `pyproject.toml`, `.content-guard-baseline.json`, and everything under `verify/`.
- `scripts/ci_*` and the scripts and data files called out in the
  [guard's path rules](../scripts/workflow_guard.py), including CI selection,
  Windows runners, release checks, workflow verification, and the approval audit.
- The npm publishing helpers and their inputs under `npm/feasibility/`.
- The Hub verification helper `registry/skills/verify-brigade/control-brigade.py`.
- Every `conftest.py`, plus the coverage sidecar guard, shared test support,
  workflow contract tests, Windows pytest allowlist, and Fleet Hub container
  smoke runner under `tests/`.

The workflow drift test checks paths named in workflow commands and inputs.
Transitive inputs are covered only where listed in
[`tests/test_workflow_guard.py`](../tests/test_workflow_guard.py).

## Who applies the label

The author of a change does not approve it, and a builder that wrote the change never applies the label. The conductor who owns the pull request applies `workflow-change-approved` after one independent security review of the protected-path changes at the current head commit. The review checks what the change does to CI checks, including a removed or weakened check, narrowed test selection, changed pass condition, new skip, widened allowlist, changed permission, or changed trigger. A comment on the pull request records the reviewed commit, lists the protected files, and gives the verdict. The conductor applies the label only when the verdict is to ship.

A protected-file change needs one such review at the pull request head. A push that changes protected files again needs a new review and label. A push that leaves the protected files unchanged still needs the label again, but may cite the earlier review in a comment that says so. No pull request gets more than two of these reviews without the operator's agreement. The approval is recorded and audited after the fact. It does not come from a separate account.

## Approval

When protected files change, the pull request conductor applies the
`workflow-change-approved` label after an independent security review of the
protected-path changes at the current head says to ship. The workflow
records a status on the event's head commit after checking the live pull
request and label. An approval status applies to the pull request number,
exact base branch, head commit, and latest approval label event. The writer
serializes status writes per pull request. A delayed removal leaves a newer
application of the label in force, and a removed label invalidates the
previous approval even before a failure status is written.

Retargeting, reopening, or force-pushing invalidates earlier label events.
Remove any remaining label and apply it again after confirming the new
state. Older default-branch statuses still count when the pull request has
no such history and the status was written after the current label event.

Synchronization tries to clear the approval label. Wait for that run to
finish before applying the label to the new head. Confirm the head
immediately before labeling and check the result afterward. Reruns read
live state and skip label and status writes.

GitHub can advance the base branch while a run waits. The guard reads the
current base commit at the start of file collection. A change to the head,
base ref, base commit, or file count during collection fails the check.

GitHub's API lists at most 3,000 changed files. The check always fails if the
list is incomplete, even with an approved commit. Split a pull request that
changes more than 3,000 files.

On a merge queue event, the job reports `workflow-guard` for the merge group.
The changed-file and approval checks run on pull request events.

Source and test files outside the listed paths are outside the guard's scope. A pull
request can still weaken a test it edits, and code review is what catches
that. The approval is recorded and audited after the fact. It does not come
from a separate account.

## Approval digest

Run `python scripts/workflow_approval_digest.py --repo OWNER/NAME` to print the last seven days of merges that changed protected paths on the default branch or `release/0.32`. Whoever audits approvals should run the script once a week. Use `--days N` to choose another range or `--since ISO-DATE-OR-DATETIME` to choose another cutoff. The digest lists the base branch, approval status timestamps, and label presence, then calls out protected changes without an approval status for audit. API failures stop the command with exit status 2 and no partial digest.

## Before making the check required

- Configure an applicable Actions event policy that allows
  `pull_request_target` for `.github/workflows/workflow-guard.yml` before
  GitHub's default block starts on 2 December 2026.
  [GitHub's rollout notice](https://github.blog/changelog/2026-11-02-pull_request_target-blocked-by-default-december-2/).
- Confirm that the `workflow-change-approved` label exists in the repository.
- On a live pull request, confirm that the `workflow-guard` check attaches to
  the pull request head commit.


## Remove the maintenance exception

When the 0.32 maintenance branch closes, remove the exact branch from the
guard's admission rule and its tests. Remove the ruleset that targets only
`release/0.32`, or remove that branch from the ruleset's include list if
it is shared. Revert any Actions policy exception scoped only to that branch.
