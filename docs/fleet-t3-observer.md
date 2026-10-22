# Native T3 Code thread presence

This optional observer reads current conversations from native T3 Code shell
metadata. Ordinary user conversations are included independently of t3-fleet's
managed dispatch receipt store. It uses no thread snapshots, chat history, or
provider turns and installs no packages, service, configuration, or credentials.

Use an explicitly configured absolute path to an existing trusted local Python
interpreter with t3-fleet installed:

```bash
python -m brigade.fleet_t3_observer --t3-python /absolute/path/to/t3/venv/bin/python
```

The default only reports counts. Add `--publish` to publish through Brigade's
public fleet client using this node's existing enrollment. A node token and Hub
URL are required for publishing. The interpreter is trusted operator
configuration, never browser or remote input. Missing executables, missing or
incompatible packages, schema errors, and native failures return `ok=false`.
No Brigade runtime dependency on t3-fleet is added.

A constant bounded subprocess calls the installed
`t3_fleet.agent._build_agent(os.environ).t3.shell_snapshot()` read client, which
uses its existing local configuration and credential broker. Native authentication
may issue its own local grant. The helper exposes no grant values. The supported
native schema is version 2, with project `id` and `workspaceRoot`, and thread
`id`, `projectId`, `status`, `activeRunId`, `activityRunStatus`, `worktreePath`,
`archivedAt`, and `deletedAt` when present. The helper validates states across
current threads, then projects only active threads and their referenced projects into operational metadata. The optional
`archivedThreads` array is excluded when present and may be omitted. The helper
never accesses or serializes
`latestVisibleMessage`, titles, prompts, provider identities, or model selection.
The native client bounds the underlying shell response to 16 MiB. The helper
bounds projection to 256 KiB, 2,000 projects, and 10,000 total threads including
archived records. Incompatible schema and any exceeded bound fail visibly.

Known active states are queued, preparing, starting, running, and waiting.
A nonterminal thread with an active run ID or known active run status also
qualifies. Pending runtime requests or background tasks also qualify. Their native
shapes are validated against the installed client contract. The helper projects
only `hasPendingRuntimeRequest` and `hasPendingBackgroundTasks` booleans from
field truthiness, never their contents. Unknown shapes fail the whole collection.
Idle threads without activity, terminal states, deleted threads,
and archived threads are excluded. Unknown states fail collection with a fixed error. Only active referenced projects need an existing absolute
workspace. A valid actual `worktreePath` needs no project lookup. Only a null
worktree falls back to the project's `workspaceRoot`. A bad non-null worktree
never falls back. Invalid active-row IDs, workspaces, or referenced project
metadata skip that row and increment `invalid_rows`. Valid rows still publish,
with `ok=false` and exit 1. Identity collisions, schema/bounds errors, and
unknown states reject the whole collection before publication. Obsolete project
directories do not block active threads. Workspace paths, including resolved
paths, are capped at Hub's 1,024 characters.

Presence uses `harness="t3"` (T3 Code), TTL 300 seconds, and the native thread ID
when it is a valid opaque ID of at most 128 characters. Every other valid native
ID uses `t3-` followed by the SHA-256 hexadecimal digest of the full native ID.
This includes long delegated IDs and short IDs containing `/` or `%`. Native
IDs must first be nonempty, at most 4,096 characters, and free of controls or
whitespace. This bounded value is an internal presence identity, never a native
execution request handle or a substitute for native correlation.
Native provider-thread references do not reliably match Claude or Codex hook
session IDs, so the observer does not guess deduplication or report an aggregate
working-agent total. `activity_scope="native_t3_presence"` describes reported
presence and makes no execution, capacity, or model-policy admission claim.

The observer shares the safe bounded workspace snapshot and publication loop
with the OpenClaw observer. Git identity and branch metadata are read with
fsmonitor disabled. No dirty paths are attributed to a thread. Non-repository
workspaces use the existing node-scoped `local:` path-hash identity.
`dirty_paths=()` and `dirty_truncated=True` mean dirty-file ownership is unknown.

A poll has a total 15-second work budget, native timeout at most eight seconds,
and up to 50 publication attempts while time permits. The caller should allow five
additional seconds for process cleanup with an outer 20-second hard timeout.
Every native/Git call uses the remaining deadline. Publication starts only when
the public client's fixed two-second call plus 250 ms fits. A fast Hub can
refresh all 13 active threads in one pass. Slow calls or the 50-row cap leave
partial publication without extending the TTL.

A private numeric cursor stores the next offset into sorted thread selection,
independently of caller cadence. List churn can shift which row that offset selects. It contains no native metadata
and lives at `$BRIGADE_HOME/observer-cursors/t3.cursor` (default
`~/.brigade/observer-cursors/t3.cursor`). Inspect mode never creates it. Cursor
files use the fleet client's existing portable private-file helpers, including
its Windows path fallback, plus atomic replacement. The fallback is covered by
fixtures without POSIX-only ownership functions. Native Windows execution still
requires platform verification. Run one publisher per harness.

OpenClaw and T3 run separately with independent budgets and cursors. A large
OpenClaw listing cannot consume T3's deadline. Partial publication can still
leave rows to expire before the cursor returns to them.

JSON reports active, total, observed, invalid_rows, published, skipped, and truncated counts.
Skipped includes invalid rows and valid rows not published.
Exit 0 means collection and requested publication completed. Exit 2 means the
publication cap or deadline left partial publication. Exit 1 means collection or
publication failed. All partial and failed results set `ok=false`. Publication failures are counted as partial and later rows are attempted while
the two-second deadline reserve remains. Successful writes remain counted. Raw stdout, stderr, credentials, shell
contents, and native identifiers are never copied to the count report.

Live inspection does not publish or prove Hub visibility. The integration owner
must review and explicitly wire publishing into the existing telemetry path.
