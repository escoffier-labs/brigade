# Required test planning

Run `brigade work verify required` from the Git repository root, or pass that root with `--target`. The command plans local checks for the committed diff and writes a plan record.

The base is the merge base of `HEAD` and the local remote-tracking ref for the default branch. Fetch the default branch if its ref is missing. The record identifies the base and its source. The `base_trusted` field is true only when the base comes from that remote-tracking ref. In a repository without remotes, the base is local `HEAD` and `base_trusted` is false. The human output warns that the local planner and configuration come from the same commit being checked.

The command reads `verify/required-tests.json` from the base tree. It copies each regular file listed in `planner_files` from that tree into a fresh directory, keeps the relative paths, and runs `planner_argv` there without a shell. Brigade hashes the paths and exact file bytes. Its `planner_digest` is independent of the optional `planner_reported_digest`.

Planner placeholders are whole arguments. Use `{python}` for the running Python interpreter, `{base}` and `{head}` for commit IDs, `{output}` for the plan file, `{planner_dir}` for the planner directory, and `{repo}` for the target root. An argument may start with `{planner_dir}/` followed by a relative path. Planners that inspect source files can use `{head_dir}` for a temporary copy of regular files from the committed head tree, with Git history available for read operations. The bundled planner uses this copy so uncommitted source edits cannot change its selection.

Use `--json` to print one JSON object. The command also writes the object atomically to `.brigade/work/verify-required/<head>.<base-first-12>.json`. Unavailable commit IDs use `unknown` in the filename. The JSON record path is null when the target directory does not exist or the record cannot be written. The `covers_worktree` field compares the committed tree with tracked files and untracked files that Git does not ignore, excluding generated Brigade evidence. Ignored untracked files can affect test collection even when this field is true. A plan describes requirements for the committed diff. Verification evidence comes from running the selected checks or observing required CI.

| Mode | Exit code | Local requirement |
| --- | --- | --- |
| `selective` | 0 | `tests`. The plan contains a nonempty list of files from the head tree. Brigade expands the whole `{tests}` element in `run_argv` into separate arguments. The Bash hint shows up to 40 files and 4,000 characters. On Windows, use Git Bash. Set `PY=.venv/Scripts` when the virtual environment uses the Windows layout. The record contains the complete command. |
| `full` | 0 | `ci`. Use required CI on the recorded head commit. The record carries planner reasons and an empty test list. |
| `unconfigured` | 3 | `unknown`. The base tree has no `verify/required-tests.json`. |
| `error` | 1 | `ci`. The base, configuration, planner, selection, or record could not be verified. |
