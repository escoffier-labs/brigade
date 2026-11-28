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

The check skips tags, deletions, and unchanged refs. It checks up to 128
local receipt files. The receipt's tree fingerprint and its plan snapshot
tree, if present, must both match the pushed branch tip's committed tree.
A directory without a receipt provides no verification evidence. Changes in the working tree and session
identity have no effect. The declaration comes from the merge base of the
pushed tip and the fetched default branch.

The gate compares the recorded command with the current required argv
prefix. A pass requires the recorded command to start with that exact
prefix, including the executable spelling on Windows, and to meet the tree,
policy, and coverage requirements. A run through a wrapper such as `env`
or `timeout`, with an environment variable set in front, or under another
spelling of the program does not count as a pass. This includes assignments
recorded separately from argv and alternate names such as `py.test`.

The gate recognizes up to eight layers of `env`, `command`, `nice`,
`nohup`, `time`, `timeout`, and `stdbuf` with supported options.
Recognized wrappers, environment prefixes, and alternate spellings make
the invocation ambiguous. A failed, timed-out, or unfinished ambiguous
run makes the verdict indeterminate. A completed ambiguous run leaves an
earlier pass in place. When such an invocation affects the verdict, the
gate names the wrapper, variable, or executable spelling and prints the
plain policy command to run again.

When verify refuses every command at intake and starts none of them, the
receipt provides no evidence about the pushed tree. This needs the receipt's
run directory name and start time to agree, like any other receipt. An
earlier passing receipt can still count.

After a merge from the default branch, a passing receipt for the exact
merged tree can still count if its plan base is an ancestor of the push
base and the required-check declaration still matches. The gate plans
against the push base and checks that the receipt covers the selected
test files. If ancestry or coverage cannot be established within the
push check deadline, the gate rejects the receipt and prints the
verification command to run again after the merge. When it can calculate
the new plan, the command includes the new required test files.

A newer matching failure
takes precedence over an older pass regardless of the policy declaration
or base recorded in the receipt. A command that failed or timed out, or
recorded failed or errored tests, counts as a failure. If an older
receipt has only a failed or timed-out overall status and includes the
required command, the gate treats the required command as failed. A failed
run that started on the pushed tree still counts if the tree changed during
the run. Rerun the required check on the pushed tree to clear the failure.

A failed or timed-out run of the required check counts even when it
collected no tests or omitted a test selector. A run with the exact declared
prefix that completed without meeting the current eligibility rules, or was
explicitly rejected, also takes precedence over an older pass. A terminal
run whose recorded commands are all unrelated leaves an earlier verdict in
place. A newer possible run of the required check gives an indeterminate
verdict when its identity or result is inconclusive and the receipt does
not already record a required check failure. This includes unsuccessful
or unfinished invocations that use the required executable with different
prefix arguments or use an interpreter or launcher whose arguments mention
the required program, module, or script. When the policy identifies a
module or script, the gate uses that target for mentions. A script name
can mention it as a whole segment delimited by a hyphen or underscore.
A different target with no such mention is unrelated. So is an invocation
of the required executable with just one of `--version`, `--help`, `-h`,
`--collect-only`, or `--co`. A policy with no required command prefix
accepts any eligible receipt for the tree.

When a receipt lists its planned commands, the gate compares the plan with
recorded commands in order using the same identity rules. A missing or
conflicting result for a possible required check makes the verdict
indeterminate. An in-progress receipt whose plan lists only clearly
unrelated commands leaves an earlier verdict in place. The writer fixes
the plan in memory before executing commands and saves it with the receipt.
That plan identifies the required check when the run includes it. Receipts
written before the plan field existed are
evaluated using their recorded commands.

Across seconds the check orders runs by the UTC timestamp in the directory
name. The name must have the writer's timestamp and suffix format. For a
matching receipt, the name and `started_at` must identify the same UTC
second, with up to just under one second of fractional precision in
`started_at`. A future timestamp makes the verdict indeterminate. Within a
second the check orders runs by their full recorded start times.

An unreadable or malformed receipt in a directory with a valid run name
makes the verdict indeterminate if that run could affect the decision.
An unreadable or malformed receipt in another directory generates a warning
because the writer could never use that directory as a run. Invalid test
counts, including negative numbers and booleans, make a receipt malformed.
A symbolic link in the receipt directory makes the verdict indeterminate.
Only receipt files in writer-named directories count toward the cap. If
more than 128 such receipts exist, the result is indeterminate even if a
pass is present. The check reports ignored malformed entries once per push.

| Result | Meaning |
| --- | --- |
| `ok` | An eligible receipt matches the tree and required check, if declared, with no newer governing run. |
| `missing_receipt` | There is no matching local receipt. |
| `rejected` | A newer run failed the required check, or the scan found matching receipts but none were eligible. |
| `not_applicable` | Neither the base nor the fetched default branch tip declares a required check. Text output is silent for this result. |
| `base_predates_policy` | Update the branch from the default branch to pick up the declaration. |
| `indeterminate` | The check could not reach a verdict, for example because a possible required run has an unconfirmed result, the base is unresolved, or a limit was reached. |

If the scan limit leaves the verdict indeterminate, set `verify_runs_keep`
in `.brigade/config.json` to at most 128 and check the configured archive.
The default retention is 50 runs, and the default archive is
`.brigade/work/verify-archive`. Run `brigade work verify push-gate prune`
to retain the newest writer-named receipts by recorded start time and archive
older eligible runs. Prune leaves non-writer directories in place and lists
each with a move command. Prune reports invalid writer-named receipts before
changing retention. A fresh verification run resolves an unordered
pass and failure in the same second. If the base or declaration is the
problem, fetch the default branch or correct the declaration as indicated
by the reason, then run the push check again.

The exit code is 1 if any result is `missing_receipt` or `rejected`, or 3
if the only problems are `base_predates_policy` or `indeterminate`. It
returns 0 for `ok`, `not_applicable`, or no branch updates, and 2 for
invalid input. Missing or rejected receipts print a verification command.

The advisory command reports results. The installed hook uses the same
evaluator. The check runs offline with a two-second deadline and a 4 MiB
limit per receipt. A filesystem operation or JSON parse in progress can
run past the deadline. The next deadline check makes the result indeterminate.

## Install the push gate

Run `brigade work verify push-gate install` to set this clone to `warn`.
The gate advises on missing or rejected receipts and lets the push continue.
Run `brigade work verify push-gate install --mode enforce` to opt this clone
into refusing a push if any branch tip has a missing receipt, a rejected
receipt, or an indeterminate verdict. The receipt scan limit and the
two-second check deadline both produce indeterminate verdicts. A refusal
names the affected ref and prints the reason and a recovery action. If
the action is to verify a commit, do it in the pushing worktree with the
named commit's tree. A valid receipt or a trusted base with no required check
allows the push. The gate ignores deletions and updates to non-branch refs.

Use `install --mode off` to turn off checking and recording. An unset mode
also means `off`. Git reads the mode from the clone's local config and
its includes. Global, system, and worktree-scoped values do not select
the mode. The gate trims whitespace and ignores letter case. A different
nonempty value produces a warning and leaves the gate idle.

The effective Git pre-push hook must carry the gate's marked block, and
`brigade` must be on the `PATH` Git uses. The shipped Brigade content-guard
template includes the block. If the clone uses its own Git hooks directory
and has no pre-push hook, install writes a minimal one. Install prints the
block and the effective hook path when the hook needs updating. Existing
hooks are left in place, including tracked hooks and hooks in a global
`core.hooksPath`. For a global Brigade content guard, refresh that guard
from the current shipped template. For any existing v1 hook, upgrade
Brigadestack, rerun install with your chosen mode, and replace the old
marked block with the v2 block printed by install. The v1 hook discards
the gate's exit status, so it still allows the push with `enforce` set.

`brigade work verify push-gate status` shows the effective hook, its version,
whether it can run, and whether enforcement is effective. Status can confirm
enforcement for the current managed minimal hook and the shipped full hook.
For a customised hook that contains the current block, status and install
say that the block is present and that enforcement cannot be confirmed.
Install still accepts the current block in a customised hook. Install
requires the current hook block, `brigade`, and a usable `timeout`. `brigade work verify push-gate uninstall` clears the
clone's mode and removes an unmodified minimal hook if present. It keeps
the records.

The gate keeps up to 5,000 branch-tip observations and 2 MiB under the common
Git directory at `brigade/push-gate.jsonl`. Entries include the UTC time, ref,
commit, tree, verdict, reason code, check duration, Brigade version, worktree,
and mode. Enforce entries record whether the push was blocked. When a push
updates several refs, each row records the same push outcome.
Use `brigade work verify push-gate report` for branch-tip counts and timings, optionally
with `--since YYYY-MM-DD` for an inclusive UTC date filter or `--json`.
The log records outcomes only when the gate can start and write the log.

The gate requires a `timeout` command that supports the GNU coreutils `-k`
option. Stock macOS and Git for Windows may lack that command. Windows'
own `timeout.exe` does not support this invocation. Bash bounds the readiness
probe independently of the `timeout` command being checked. When Git for
Windows supplies Bash, status and install probe from that Git environment.
Git config and hook setup have a separate deadline from the two-second receipt
evaluation. The hook asks the
gate process to stop after five seconds and kills it one second later if
needed. Warn mode reports indeterminate verdicts and gate errors and lets
the push continue. In enforce mode a gate error blocks
branch updates because Brigade could not decide whether they have a valid
receipt. Restore `brigade` and GNU coreutils `timeout` on Git's `PATH` if
either is missing, then run `brigade work verify push-gate status`. For an
evaluation error before Brigade can read the policy, use the printed
`brigade work verify push-check` command to diagnose the pushed commit.

### Upgrade the block in a customised hook

The effective pre-push hook must start with a Bash interpreter line. Run
`brigade work verify push-gate install --block-only --dry-run` to preview
a unified diff. Remove `--dry-run` to replace the block atomically. The
upgrade preserves the surrounding bytes, line endings, file mode, and Git
configuration. It saves a timestamped backup beside the hook and prints a
Bash command to restore it. If the hook has no block, pass
`--confirm-insert` to insert it immediately after its single
`STDIN_REFS="$(cat)"` line. An upgrade requires exactly one gate invocation
inside the markers. Install and dry run refuse to change the hook if a
line outside the block contains the `push-gate` token. A shell line
continuation joins its physical lines for this check, and the report names
the line where the joined line starts. Comment lines are skipped and do not
continue onto the next line. Both report the line numbers. Move or remove
the calls before running the command again.
Status reports the block version, identifies a customised hook as
unverified, and lists any lines with the token outside the block.

### Known limits

- Git's `git push --no-verify` skips every pre-push hook, including this gate.
  Brigade adds no bypass and cannot detect that Git skipped the hook.
- A clone opts in through `push-gate install` and needs the current hook.
  The older v1 block allows the push even when the clone is set to enforce.
- When Git cannot read the clone's configuration, the gate does not run and
  reports the error. Fix the configuration and check with
  `brigade work verify push-gate status`.
- A receipt can list up to 64 planned commands, with at most 16,384
  characters and 1,024 parsed arguments per command. A larger plan or
  command makes the result indeterminate when that receipt can affect it.
- The gate treats tasks launched through `make`, `just`, `tox`, or
  `npm run` as unrelated unless their arguments mention the required check
  or the policy names the invocation directly. It does not inspect task
  definitions. Policies can also name interpreters and other launchers
  directly in their required argv prefix.
- A failed run of the required executable with a different argument prefix
  can make the verdict inconclusive until the required check runs again.
  The read-only invocations and clearly unrelated interpreter targets
  described above leave an earlier verdict in place.
- Verify receipts are local files. The gate checks their shape, the agreement
  between the run name and start time, and the exact tree IDs. It does not
  authenticate receipts. Required CI supplies independent evidence on the
  pushed commit.
- An indeterminate verdict blocks in enforce mode. Follow the printed
  recovery guidance. For a receipt-store problem, repair the named entry
  or prune as indicated before retrying. The 128-file cap depends on
  `verify_runs_keep` and a working verify archive. Prune archives first and
  never deletes a run it could not archive. An archive error identifies the
  archive and the first failure.
- The receipt store must be a directory. The gate reports a symbolic link or
  a regular file at that path, and retention leaves it in place.
- Prune and the receipt writer do not share a lock. Prune preserves running
  runs and waits at least 60 seconds after a run starts or its directory or
  receipt is updated. If fresh runs keep the store above the cap, retry
  pruning after they finish and the grace period elapses.
- A broken required-check declaration on the trusted base cannot be repaired
  by a push through this gate. Restore the declaration on the trusted base
  or fetch the corrected default branch before retrying.
