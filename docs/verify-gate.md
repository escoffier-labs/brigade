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
  "needs_passed_tests": true
}
```

Brigade first looks for the declaration at the merge base with the remote
default branch. If it resolves that base, it uses only the committed contents.
If it cannot resolve the base, it trusts local state. It reads the declaration
from local `HEAD` first. If `HEAD` has no copy, there is no commit, or Git is
unavailable, it looks in the working tree. Without either copy, the repository
is audit-only. The receipt stamp records the fallback source and Stop explains
why the base was unresolved and which copy was used. A malformed declaration
from any source blocks Stop with the file path and parse error.

Stop gives policy lookup a two-second budget, including time reserved for the
local fallback. If Git runs out of time before the policy can be read, Stop
skips the check for that target and reports the timeout.

At least one recorded command must start with the exact `argv_prefix`.
`needs_arguments` requires at least one argument after the prefix.
`needs_passed_tests` requires a pytest summary with at least one passed test,
zero failed, zero errors, and `no_tests_ran` false. Stop prints a suggested
`--argv-json` invocation for a POSIX shell. When a recent run was stamped
ineligible, Stop can explain why that run did not count. Receipts record
`plan_coverage: "not_checked"`.

Without a declaration, any completed receipt can satisfy the gate if it meets
the session and tree checks. Legacy receipts without a verdict count only in
audit-only mode. The policy identity consists of the mode, the declaration
digest when present, and the fallback source when used. Stop treats a receipt
with a different identity as a legacy receipt. Verify runs the commands again
when a candidate for reuse has a different identity. A legacy receipt can be
reused in audit-only mode if it otherwise meets the reuse checks.

## Limits

- A completed manifest run on a stable tree counts whatever the manifest checks,
  because manifests are read from the trusted base.
- Any selector passed to the declared command counts once at least one test passes
  with no failures or errors. A narrow or unrelated selector can satisfy the gate.
  Required CI on the head commit is the backstop.
- Brigade runs the declared command from the working tree. The gate does not
  compare the script with the copy on the base.
- With no remote, the base is local `HEAD`.
- Closeout does not read the verdict yet.
