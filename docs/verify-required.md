# Required test planning

`brigade work verify required` asks the repository's tracked planner which local tests cover the diff from a base commit to `HEAD`. It is a read-only planning command. It does not run tests or change verification gates.

The command loads `verify/required-tests.json` from the base commit, not from the working tree. By default, the base is the merge base of `HEAD` and the default upstream branch. Use `--base REF` to plan another range.

A selective plan prints the exact focused command for the selected test files. A full plan means the diff is too broad for safe local selection. In that case, required CI on the exact head commit supplies the evidence, and the full suite must not be run locally. Planner errors require attention and never mean that no checks are required.

The JSON plan is printed with `--json` and is always written atomically under `.brigade/work/verify-required/`. The `covers_worktree` field is false when uncommitted changes differ from `HEAD`.
