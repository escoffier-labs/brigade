# The local verify gate

A strict Claude Stop hook checks for a completed verify receipt from the same
session and target, after the last write, covering the current tree. A receipt
is ineligible if the tree changed during verification.

To require a check, commit `verify/required-check.json` at the root of the
repository's default branch. For example, Brigade uses this declaration.

```json
{
  "schema": "brigade.verify_required_check.v1",
  "argv_prefix": ["./scripts/verify-focused"],
  "needs_arguments": true,
  "needs_passed_tests": true,
  "needs_plan_coverage": true
}
```

Brigade first looks for the declaration at the merge base with the remote
default branch. If it resolves that base, it uses only the committed contents.
If it cannot resolve the base, it checks for the declaration at the fetched
default-branch tip, then at local `HEAD`, then in the working tree. This lets a
shallow clone enforce the declaration in the fetched branch even if it lacks
the history to find a merge base. Without a copy, the repository is audit-only.
The receipt stamp records the fallback source. When Stop blocks, it explains
why the base was unresolved and which copy was used. A malformed declaration
from any source blocks Stop with the file path and parse error.

Stop gives policy lookup a two-second budget, including time reserved for the
local fallback. If Git runs out of time before the policy can be read, Stop
skips the check for that target. Stop includes the timeout detail if another check blocks.

At least one recorded command must start with the exact `argv_prefix`.
The prefix cannot contain backticks, line breaks, or control characters.
`needs_arguments` requires at least one argument after the prefix.
`needs_passed_tests` requires a pytest summary with at least one passed test,
zero failed, zero errors, and `no_tests_ran` false. Stop prints a suggested
`--argv-json` invocation for a POSIX shell. When a recent run was stamped
ineligible, Stop can explain why that run did not count.

This repository opts in to plan coverage. Other repositories can set
`needs_plan_coverage` to `true` to require the tests selected for the change.
Both `needs_arguments` and `needs_passed_tests` must also be true. The flag
defaults to false.

A fresh, completed run on a stable tree computes the plan once, when it
writes the receipt. The planner and its settings come from the resolved
base. The planner sees the verified tree, including tracked edits and
untracked files that Git does not ignore. Preview the plan with
`brigade work verify required --worktree --json`, then pass its complete
`run_argv` array to one `brigade work verify run`.

A one-file source edit took about two and a half seconds to plan in a
local benchmark. Previewing and then verifying computes the plan twice.
You can skip the preview and run your focused tests directly. If the run
misses planned files, Stop and closeout reject the receipt and show the
full command to run.

For a selective plan of up to 20 test files, one receipt must cover every
required file. It can cover them across multiple commands. Each contributing
command starts with the declared prefix and specifies whole, repository-relative
test files. The only allowed options are `-v`, `-vv`, `-p xdist`,
`-n`, `--timeout`, `--durations`, and `--tb`, with their required values.
Put extra pytest options such as `-p xdist -n 4` after the command prefix and
before any `--` separator or test path. The repository planner's command keeps
that separator. Stop and closeout print the prefix followed directly by the
planned paths.
The command must report at least one pass and zero failures, errors, or
deselections. Setting `PYTEST_ADDOPTS`, `PYTEST_PLUGINS`, or `PYTHONPATH`
in the environment or on the command prevents it from contributing.

The basic declared check still applies if the plan is full, selects every
discovered test file, selects more than 20 files, or has no planner configured.
For broad plans, one focused pass meets the local gate and required CI on the
head commit checks the change. The receipt records that CI is required. A
planner error leaves the command status intact and makes the stamp ineligible.
The next run computes a fresh plan.

Without a declaration, a completed receipt can satisfy the gate if it meets
the session and tree checks, regardless of its stamp. A declared check requires
an eligible stamp that matches the current policy. The policy identity consists
of the mode, the declaration digest when present, and the fallback source when
used. Verify runs the commands again when a candidate for reuse has a different
identity. A legacy receipt can be reused in audit-only mode if it otherwise
meets the reuse checks.

When coverage is enabled, Stop, closeout, and reuse read the saved plan and
require its base and snapshot tree to match the current ones. A commit that
preserves both keeps the stamp current. An unresolved base leaves the plan
unchecked. When files are missing, Stop and closeout give one command with
all the planned files, since coverage belongs to a single receipt.

Work closeout applies the same policy to the newest applicable receipt for
the session and current tree. If that receipt fails the policy, closeout
reports why and shows the `--argv-json` command when it reads a valid declaration.
Closeout allows up to 60 seconds for policy lookup and blocks if lookup times out.

## Limits

- A change can select more than 20 test files or trigger a full plan, for
  example by adding many test files, adding an untracked `pytest.ini`, or
  changing a `conftest.py`. The gate falls back to the basic focused check.
  Required CI on the head commit checks the change.
- Deleting the only test that imports a changed module removes that test from
  the plan. Required CI on the head commit checks the change.
- The snapshot omits a new file hidden by a local exclude rule. It also omits a
  change confined to ignored files. Required CI on the head commit checks the
  committed change.
- With plan coverage off, a completed manifest run on a stable tree counts
  whatever the manifest checks, because manifests are read from the trusted base.
- With plan coverage off, any selector passed to the declared command counts once at least one test passes
  with no failures or errors. A narrow or unrelated selector can satisfy the gate.
  Required CI on the head commit is the backstop.
- Brigade runs the declared command from the working tree with the caller's
  environment, including `PY`. The gate does not compare the script with the
  copy on the base.
- With no remote, the base is local `HEAD`.
- Policy lookup runs the first `git` on `PATH`, so a declared repository
  reads as undeclared if a stand-in fails the remote and `HEAD` probes and
  the working-tree declaration is deleted. Required CI on the head commit
  checks the change independently.

## Push check

`brigade work verify push-check` reads Git pre-push updates from standard
input. Each line contains the local ref, local object ID, remote ref, and
remote object ID. Use `--ref <name>` to check a single local ref without
reading standard input. Use `--target <path>` to select a worktree and
`--json` for one object with a `refs` array.

The check skips tags, deletions, and unchanged refs. It compares each pushed
branch tip's committed tree to the newest tested receipt in that worktree
with the same tree fingerprint or saved plan snapshot. It skips running,
busy, and canceled receipts. A newer failed or ineligible run takes
precedence over an older pass. Changes in the working tree and session
identity have no effect. The declaration comes from the merge base of the
pushed tip and the fetched default branch.

| Result | Meaning |
| --- | --- |
| `ok` | The newest matching receipt meets the required check. |
| `missing_receipt` | There is no matching local receipt. |
| `rejected` | The newest matching receipt fails the required check. |
| `not_applicable` | Neither the base nor the fetched default branch tip declares a required check. Text output is silent for this result. |
| `base_predates_policy` | Update the branch from the default branch to pick up the declaration. |
| `indeterminate` | The check could not reach a verdict, for example because the base is unresolved, the declaration is malformed, or a limit was reached. |

The exit code is 1 if any result is `missing_receipt` or `rejected`, or 3
if the only problems are `base_predates_policy` or `indeterminate`. It
returns 0 for `ok`, `not_applicable`, or no branch updates, and 2 for
invalid input. Missing or rejected receipts print a verification command.

This version only reports results. It installs no hook and does not block
a push. The command shares a two-second budget across its Git calls and
reads at most 128 local receipts. It runs offline without a planner or tests.
Local receipts can be forged. Required CI on the head commit is the proof.

## Install the push gate

Run `brigade work verify push-gate install` to set this clone to `warn`.
The gate advises on missing or rejected receipts and lets the push continue.
Use `install --mode off` to turn off checking and recording. An unset mode
also means `off`.

The effective Git pre-push hook must carry the gate's marked block, and
`brigade` must be on the `PATH` Git uses. The shipped Brigade content-guard
template includes the block. If the clone uses its own Git hooks directory
and has no pre-push hook, install writes a minimal one. Install prints the
block and the effective hook path when the hook needs updating. Existing
hooks are left in place, including tracked hooks and hooks in a global
`core.hooksPath`. For a global Brigade content guard, refresh that guard
from the current shipped template or paste the printed block after it reads
the pre-push input. Each clone still chooses its own mode.

`brigade work verify push-gate status` shows the effective hook, its version,
and whether it can run. `brigade work verify push-gate uninstall` clears the
clone's mode and removes an unmodified minimal hook if present. It keeps
the records.

Warn mode keeps up to 5,000 branch-tip observations and 2 MiB under the common
Git directory at `brigade/push-gate.jsonl`. Entries include the UTC time, ref,
commit, tree, verdict, reason code, check duration, Brigade version, worktree,
and whether the declared check would have blocked under enforcement.
Use `brigade work verify push-gate report` for counts and timings, optionally
with `--since YYYY-MM-DD` for an inclusive UTC date filter or `--json`.
A push whose Git environment cannot find `brigade` leaves no observation.

The gate does not defend against `git push --no-verify`.
