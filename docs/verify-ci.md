# CI evidence for HEAD

Run `brigade work verify ci` in a clean Git checkout after pushing the commit.
Use `--target` to select a different checkout, `--check` to select the
repository's required check if it differs from `ci-gate`, and `--json` for
machine-readable output. Authenticate the GitHub CLI (`gh`) for the checkout's
GitHub origin before running the command.

Brigade records the newest run of the named check for the exact commit in
`.brigade/work/verify-ci/<head-sha>.json`. The record includes the commit and
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

`brigade work closeout` reads the saved CI record for the current commit and
tree. Run `brigade work verify ci` again to refresh the result before closeout.
