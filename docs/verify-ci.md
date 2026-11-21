# CI evidence for HEAD

Run `brigade work verify ci` in a clean Git checkout after pushing the commit.
Use `--target` to select a different checkout, `--check` to look at a check
other than `ci-gate`, and `--json` for machine-readable output. Authenticate the GitHub CLI (`gh`) for the checkout's
GitHub origin before running the command.

Brigade records the newest run of `ci-gate` for the exact commit in
`.brigade/work/verify-ci/<head-sha>.json`. A run with `--check` is recorded in
its own file next to it. The record includes the commit and
tree IDs, the check run ID, and the time Brigade fetched the result. A dirty
checkout is refused before contacting GitHub or writing a record.

| State | Exit code | Meaning |
| --- | --- | --- |
| `passed` | 0 | The check completed with conclusion `success`. |
| `failed` | 1 | The check completed with any other conclusion. |
| `pending` | 2 | The check is queued or in progress. |
| `unknown` | 2 | Brigade could not obtain a matching check result. |

A checkout or record-writing error also returns 2 and reports the error on
standard error. Only `passed` for the required check on the exact head commit
counts as suite evidence. Local verification receipts describe local checks.

`brigade work closeout` reads the saved record for `ci-gate` on the current
commit and shows when it was fetched. It reports `unknown` when no such record
exists, when the record names another check or commit, or when the working tree
has changed since. Each check name has its own record, so `--check` never
replaces the `ci-gate` result, and closeout does not show a `--check` result. A
repository whose required check has another name therefore reads `unknown` at
closeout for now. Closeout does not contact GitHub: run
`brigade work verify ci` again to refresh the result, for example after a job is
re-run on the same commit.

Limits:

- The GitHub CLI must be version 2.48 or newer, because the command uses
  `gh api --slurp`. An older version is reported as `unknown`.
- The check is read from the `origin` remote. In a fork checkout, where the
  required check ran on the upstream repository, the result is `unknown`.
