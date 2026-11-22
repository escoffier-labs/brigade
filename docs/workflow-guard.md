# Workflow guard

GitHub runs the pull request check and its checkout from the repository's
default branch. The `workflow-guard` check reruns when the base branch changes
and fails if the pull request targets another branch. It uses GitHub's
changed-file list and checks both paths when a file is renamed.

The guard protects the following paths.

- GitHub Actions workflows and any repository-local actions under `.github/`.
- `pyproject.toml`, `.content-guard-baseline.json`, and everything under `verify/`.
- `scripts/ci_*` and the scripts and data files called out in the
  [guard's path rules](../scripts/workflow_guard.py), including CI selection,
  Windows runners, release checks, and workflow verification.
- The npm publishing helpers and their inputs under `npm/feasibility/`.
- The Hub verification helper `registry/skills/verify-brigade/control-brigade.py`.
- Every `conftest.py`, plus the coverage sidecar guard, shared test support,
  workflow contract tests, Windows pytest allowlist, and Fleet Hub container
  smoke runner under `tests/`.

The workflow drift test checks paths named in workflow commands and inputs.
Transitive inputs are covered only where listed in
[`tests/test_workflow_guard.py`](../tests/test_workflow_guard.py).

## Approval

When protected files change, a maintainer applies the
`workflow-change-approved` label after reviewing the change. The workflow
writes a success status with that context to the head commit in the label
event. Removing the label manually writes a failure status to the event's
head commit. For the current head, the guard uses the newest status in this
context from GitHub Actions.

A push to a new commit needs a fresh label event. The workflow tries to clear
the label on push so the pull request shows that approval needs to be renewed.
If the label remains, remove it, wait for the revocation run, then add it
again. The guard decides from the status on the live head even if label
cleanup fails. If recording approval or revocation fails, that run fails.
Reruns read the live head and its statuses without replaying label writes.

A push just before labeling can cause the label event to name the newly
pushed commit. Confirm the head immediately before applying the label and
check the result afterward. Wait for a label run to finish before reversing
the label again, since the API orders statuses by when they were written.

GitHub's API lists at most 3,000 changed files. The check always fails if the
list is incomplete, even with an approved commit. Split a pull request that
changes more than 3,000 files.

On a merge queue event, the job reports `workflow-guard` for the merge group.
The changed-file and approval checks run on pull request events.

Source and test files outside the listed paths are outside the guard's scope. A pull
request can still weaken a test it edits, and code review is what catches
that. An account with permission to label pull requests can apply the
approval label to its own change.

## Before making the check required

- Configure an applicable Actions event policy that allows
  `pull_request_target` for `.github/workflows/workflow-guard.yml` before
  GitHub's default block starts on 2 December 2026.
  [GitHub's rollout notice](https://github.blog/changelog/2026-11-02-pull_request_target-blocked-by-default-december-2/).
- Confirm that the `workflow-change-approved` label exists in the repository.
- On a live pull request, confirm that the `workflow-guard` check attaches to
  the pull request head commit.
