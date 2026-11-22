# Workflow guard

The `workflow-guard` check runs from the pull request's base branch. It
protects GitHub Actions workflows and local actions, CI scripts matching
`scripts/ci_*`, pull request test tier files, the Windows test selector,
the guard script, and the required check and test declarations under
`verify/`. It checks both paths when a file is renamed.

If a pull request changes any protected paths, the check lists them and
requires the `workflow-change-approved` label. A maintainer applies the
label after reading the change. Adding or removing the label reruns the
check. The merge queue reports success because pull requests pass the
guard before entering the queue.

An account with permission to label pull requests can approve its own
change by applying the label.
