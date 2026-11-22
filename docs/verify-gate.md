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

Brigade reads the declaration at the merge base with the remote default
branch. In a repository without a remote, it reads the `HEAD` commit.
Working-tree edits to the declaration have no effect on the check.

At least one recorded command must start with the exact `argv_prefix`.
`needs_arguments` requires at least one argument after the prefix.
`needs_passed_tests` requires a pytest summary with at least one passed
test, zero failed, zero errors, and `no_tests_ran` false. The receipt
records `plan_coverage: "not_checked"`. Any selector passed to the
declared command can qualify. A completed run selected with `--manifest`
can also satisfy the gate on a stable tree.

Without a declaration, the repository is audit-only. Any completed receipt
can satisfy the gate if it meets the existing session and tree checks. Stop
guidance says when the repository declares no required check. Legacy receipts
without a verdict count only in audit-only mode. If Brigade cannot determine
the Git base or read a valid declaration there, the verdict is unresolved
and the receipt cannot satisfy the gate.

A reused receipt carries the original verdict. Use `--no-reuse` to execute
the check again.
