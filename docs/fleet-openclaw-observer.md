# OpenClaw recent-session presence

The optional observer adds OpenClaw session presence through the enrolled
node's public `fleet_client.publish_session` API. It runs once and installs
no service, timer, hook, credentials, or configuration.

Inspect counts without publishing:

```bash
python -m brigade.fleet_openclaw_observer
```

Explicitly publish from an already enrolled node:

```bash
python -m brigade.fleet_openclaw_observer --publish
```

The native metadata contract was checked against OpenClaw 2026.10.5:

- `openclaw agents list --json` supplies agent `id` and configured `workspace`
  through `buildAgentSummaries` in `src/commands/agents.config.ts`.
- `openclaw sessions --all-agents --active 15 --limit 200 --json` supplies
  `count`, `totalCount`, `hasMore`, and `sessions`. `totalCount` counts rows
  within the activity window before limiting. `hasMore` means rows are omitted.
- Each session needs a bounded opaque `sessionId`, configured `agentId`, and
  integer millisecond `updatedAt`. `toSessionDisplayRow` in
  `src/commands/sessions-table.ts` also exposes optional `spawnedWorkspaceDir`
  and `spawnedCwd`. The observer prefers those paths over the configured
  workspace and checks existence only for referenced recent sessions.

The poll has a 15-second work budget, leaving five seconds for the caller's
20-second outer process timeout and cleanup. Native calls use at most ten
seconds each and share the remaining deadline. All subprocesses use supervised
process cleanup and bounded capture. Native captures above 256 KiB, truncation,
decoding failures, invalid JSON, and inconsistent counts fail visibly. An empty
agent list is invalid. Invalid individual agents or sessions are skipped and
counted as `invalid_rows`. Valid sessions can still publish, with exit 1.
Cross-agent native session ID collisions reject the collection before writes.
No session key or default workspace substitutes for invalid metadata.

Sessions updated within 15 minutes receive a five-minute TTL. Repeated polls
stop refreshing a session once its native activity leaves that window. A failure
leaves previous rows to expire. Each distinct actual workspace gets one cached
snapshot per poll. Other sessions reuse it with their own session ID.

Workspace paths, including resolved paths, are capped at Hub's 1,024 characters.
Detached HEAD has no branch value. Snapshots read bounded Git identity and branch metadata, with fsmonitor disabled
and no `git status` invocation. Non-repositories retain Brigade's path-hash
`local:` identity, scoped to this node. Workspace dirty files cannot establish
which session edited them, so observational rows always publish
`dirty_paths=()` and `dirty_truncated=True`. Exact dirty-file overlap is unknown.

Publication permits up to 50 session attempts per poll while the 15-second deadline
allows it. Before each call, the observer reserves the public client's fixed
two-second timeout plus 250 ms. A fast Hub can refresh 13 sessions in one pass
without extending their TTL. Slow calls or larger populations produce partial
counts and advance a private cursor over sorted session IDs for the next poll.
The cursor stores one numeric offset in
`$BRIGADE_HOME/observer-cursors/openclaw.cursor` (default
`~/.brigade/observer-cursors/openclaw.cursor`), using the fleet client's portable
private-file and atomic-write helpers. Inspect mode never creates it. Run one
publisher per harness to avoid concurrent cursor writers. Per-row snapshot or
publication failures leave `ok=false` but do not stop later attempts while the
two-second deadline reserve remains. Only deadline exhaustion stops the loop.

The deadline or 50-row cap can leave sessions unpublished within their TTL.
A large native listing can also exceed the capture cap. These partial or failed
polls do not claim complete coverage. Native listing truncation is distinct from
publication rotation: omitted rows cannot be rotated into the returned listing.

JSON output includes observed recent sessions, native total, `invalid_rows`,
`published`, `skipped`, `publish_limit` when publishing, `truncated`, observation
time, and newest activity age. In inspect mode `skipped` counts invalid rows. In publish mode it also counts observed sessions not published. Exit 0 means
collection and requested publication completed. Exit 2 means a partial listing,
publication cap, or a deadline reached during publication. Exit 1 means invalid
metadata, collection failure, or publication failure. `ok` is false for all
partial and failed results. `published` preserves successful writes before an
error. Raw native output and stderr never enter reports.

Presence describes recent activity. A native `done` session can still be recent.
Rows publish no execution state, model, admission decision, job, session key,
participant, prompt, or message content. An empty live inspection proves only
the empty path. Nonempty fixture regressions prove the metadata mapping without
creating activity or sending a message.

An operator may add the explicit publish invocation to an existing telemetry
publisher after review. Installation, scheduling, and a live Hub check remain
separate operator steps.
