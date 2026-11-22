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
untracked files that Git does not ignore. Run
`brigade work verify required --worktree` first, then run the files it lists
with `./scripts/verify-focused` in one `brigade work verify run`. Use `--json`
on the planning command to get the complete argument list as `run_argv`.

For a selective plan of up to 20 test files, one receipt must cover every
required file. It can cover them across multiple commands. Each contributing
command starts with the declared prefix and specifies whole, repository-relative
test files. The only allowed options are `-q`, `-v`, `-vv`, `-p xdist`,
`-n`, `--timeout`, `--durations`, and `--tb`, with their required values.
The command must report at least one pass and zero failures, errors, or
deselections. Setting `PYTEST_ADDOPTS`, `PYTEST_PLUGINS`, `PYTHONPATH`,
or `PY` in the environment or on the command prevents it from contributing.

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
- Brigade runs the declared command from the working tree. The gate does not
  compare the script with the copy on the base.
- With no remote, the base is local `HEAD`.
- Policy lookup runs the first `git` on `PATH`, so a declared repository
  reads as undeclared if a stand-in fails the remote and `HEAD` probes and
  the working-tree declaration is deleted. Required CI on the head commit
  checks the change independently.
