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
- The Hub verification helper `registry/skills/verify-brigade/control-brigade.py`.
- Every `conftest.py`, plus the coverage sidecar guard, shared test support,
  workflow contract tests, Windows pytest allowlist, and Fleet Hub container
  smoke runner under `tests/`.

When protected files change, a maintainer applies the
`workflow-change-approved` label after reviewing the change. Approval covers
the head commit at the time the label was applied. Every push clears the
label, and the push's guard run disregards any approval it sees. Adding or
removing the label manually reruns the check. If a push lands just before a maintainer
applies the label, the label can approve the newly pushed commit or the
pending removal can clear it. Confirm the current head before labeling and
check the result after labeling.

GitHub's API lists at most 3,000 changed files. The check always fails if the
list is incomplete, even with the approval label. Split a pull request that
changes more than 3,000 files.

On a merge queue event, the job reports `workflow-guard` for the merge group.
The changed-file and label checks run on pull request events.

Ordinary test files and source files are outside the guard's scope. A pull
request can still weaken a test it edits, and code review is what catches
that. An account with permission to label pull requests can apply the
approval label to its own change.
