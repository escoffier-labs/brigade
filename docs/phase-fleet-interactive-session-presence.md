# Fleet interactive session presence

## Goal

Make the Fleet Hub the source of truth for interactive agent presence so an
agent can see overlapping work before it starts or rebases. The Hub stores only
bounded operational metadata: repository identity, checkout path, branch,
harness, session identity, dirty paths, timestamps, and lifecycle state. It
never receives diffs, prompts, transcripts, tool arguments, file contents, or
credentials.

This is issue #1261. Fleet-wide plans and campaigns are a later slice. This
slice supplies the live session and overlap substrate those plans will use.

## Decision

Use a dedicated Hub session authority with a typed client and harness adapters.

Alternatives considered:

1. Derive presence from generic run events. Rejected because interactive work
   has no run lifecycle, and event replay cannot safely express TTL refresh,
   exact current state, or bounded dirty-path replacement.
2. Poll worktrees from each host. Rejected because it creates a second local
   authority, cannot identify harness sessions, and misses cloud or detached
   worktrees.
3. Treat repository claims as session presence. Rejected because claims are
   exclusive execution fences. Interactive presence is advisory and several
   sessions may legitimately inspect the same repository.

## User-visible contract

- `brigade fleet sessions` lists active sessions from the Hub.
- `brigade fleet sessions --all` includes ended and expired history.
- `brigade fleet sessions --json` emits the same bounded fields as the HTTP
  projection.
- `brigade work brief` includes an `overlap_warnings` section when another live
  session has the same repository identity and at least one identical dirty
  path. A warning never blocks work or acquires a lock.
- The Command Deck shows live interactive sessions separately from Brigade runs
  and cloud workers. Sessions do not consume station worker capacity.
- `brigade fleet status` run rows gain `repo_identity` when the reporting node
  can derive it. Existing rows and clients remain readable when it is absent.

## Authority and data flow

1. A harness adapter resolves the Brigade target, repository identity, branch,
   checkout path, and bounded dirty paths locally.
2. `fleet_client` sends a node-authenticated session operation. The Hub derives
   `node_id` from the bearer token and rejects a body that tries to select it.
3. The Hub atomically upserts or ends the one row keyed by
   `(node_id, harness, session_id, repo_identity)`.
4. Reads exclude ended and expired rows by default. `--all` is history only.
5. Consumers query the Hub. They never scan another machine's worktree or infer
   live state from old events.

Hub unavailability is fail-open for editor hooks: the editor continues and the
hook writes no competing local queue. Read commands surface the Hub error. A
later heartbeat repairs presence after connectivity returns.

## Harness coverage

- Claude Code: the managed `SessionStart` hook upserts, bounded `PostToolUse`
  refreshes after relevant tools, and an accepted `Stop` ends the session. A
  blocked Stop leaves the row active until a later accepted Stop or TTL expiry.
- Codex and T3 Code: the shared work-loop entrypoint recognizes the existing
  Codex thread identity and upserts presence when the session brief starts.
  Supported post-tool hooks call the same bounded refresh command. T3 Code uses
  the Codex harness identity rather than inventing a second identity.
- Cursor: managed `sessionStart`, `postToolUse`, and `sessionEnd` hooks call the
  same bounded presence command. The start hook preserves the existing
  work-loop context output; refresh and end return empty JSON objects. Cursor
  Cloud omits editor-lifetime start/end hooks, so its tasks remain represented
  by the existing cloud tracker rather than duplicated as interactive sessions.
- Other harnesses: a small internal `brigade work presence-hook` command accepts
  only explicit lifecycle, harness, session, and target fields. It exists for
  managed adapters, not as a second status store.

No adapter starts a daemon. Missing end callbacks are handled by TTL.

## Repository identity

`src/brigade/fleet_session_presence.py` owns identity and local snapshot rules.

For a Git worktree with an `origin` remote:

1. Parse HTTPS, SSH URL, and SCP-like forms without invoking a shell.
2. Remove user information, password, query, fragment, leading slash, and one
   trailing `.git`.
3. Lowercase the host. Preserve repository path case except for hosts whose
   repository paths are documented as case-insensitive.
4. Emit a bounded value such as `github.com/escoffier-labs/brigade`.

The raw remote URL is never sent or persisted. A remote containing an embedded
credential must produce the same credential-free identity as its clean form.

Without a usable remote, emit `local:<sha256>` from a canonical local Git common
directory identity and mark `identity_scope="node"`. Node-scoped identities do
not match sessions from another node. This is honest degradation, not a claim
of cross-machine identity.

## Session schema

Add schema version 14 and one table:

```sql
CREATE TABLE interactive_sessions (
    node_id TEXT NOT NULL,
    harness TEXT NOT NULL,
    session_id TEXT NOT NULL,
    repo_identity TEXT NOT NULL,
    identity_scope TEXT NOT NULL,
    repo_label TEXT NOT NULL,
    checkout_path TEXT NOT NULL,
    branch TEXT,
    dirty_paths_json TEXT NOT NULL,
    dirty_truncated INTEGER NOT NULL,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    ended_at TEXT,
    ttl_seconds INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (node_id, harness, session_id, repo_identity)
);
```

Allowed state is `active` or `ended`. Expiry is a read-time classification and
does not rewrite history. An upsert after expiry starts a new active interval by
replacing `started_at`; an exact active retry preserves `started_at`. End is
idempotent. Heartbeats may replace branch and dirty paths, but cannot change the
row identity.

Bounds:

- harness and session ID: 128 characters, opaque identity alphabet
- repository identity: 512 characters
- repository label and branch: 256 characters
- checkout path: 1,024 characters
- dirty paths: at most 64 entries, each at most 512 characters, total encoded
  JSON at most 32 KiB
- TTL: 120 through 3,600 seconds, default 900
- list response: at most 500 active rows or 1,000 history rows

Every string rejects control characters. Dirty paths must be repository-relative
Git paths. Absolute paths and `..` components are rejected. The server validates
the complete body before a transaction.

## HTTP and client contract

Add authenticated `GET /sessions` and `POST /sessions`.

`POST /sessions` actions:

- `upsert`: insert or refresh one active row
- `end`: end an exact row owned by the caller node

The node token may mutate only its derived node identity. The admin token may
read sessions but cannot write unless the server is in the existing explicit
admin-write compatibility mode. Dashboard cookies remain dashboard-only.

`fleet_client` owns all transport, auth classification, encryption checks, and
bounded timeouts. Hooks call the client, never the HTTP surface. Session
heartbeats are not placed in the run-event spool because replaying an old
heartbeat could resurrect expired presence. A failed heartbeat is discarded;
the next live hook refresh is authoritative.

## Dirty-path collection and overlap

Collect paths from one bounded `git status --porcelain=v1 -z` invocation. Parse
rename pairs without line splitting, exclude `.brigade/`, sort, deduplicate, and
set `dirty_truncated=true` when the count or encoded-size cap is reached.

An overlap warning requires all of:

- both sessions are active and unexpired
- repository identities and identity scopes match
- the other row is not the current `(node, harness, session)`
- the exact normalized dirty-path intersection is nonempty

The warning contains node, harness, session ID, branch, checkout path, age, and
at most eight overlapping paths. It never includes a diff or file content.
Truncated path sets produce a visible `partial=true` marker. No warning is
interpreted as proof that no overlap exists when either side is truncated.

## Module boundaries

| File | Responsibility |
| --- | --- |
| New `src/brigade/fleet_session_presence.py` | Local repository identity, dirty-path snapshot, overlap projection, validation constants. |
| New `src/brigade/fleet_hub_sessions.py` | Session schema, migration helpers, request validation, transactions, safe list projections. |
| Modify `src/brigade/fleet_hub.py` | Schema version and delegated session schema initialization. |
| Modify `src/brigade/fleet_hub_http.py` | Authenticated `/sessions` routing only. |
| Modify `src/brigade/fleet_client.py` | Typed session upsert, end, and list transport. |
| Modify `src/brigade/cli/fleet.py` | `brigade fleet sessions` read surface. |
| Modify `src/brigade/claude_hooks/runtime.py` | Lifecycle calls through the presence helper. |
| Modify `src/brigade/cursor_user_cmd.py` | Managed Cursor session-start publication. |
| Modify `src/brigade/work_cmd/session/briefing.py` | Startup publication and advisory overlap section. |
| Modify `src/brigade/fleet_command_deck.py` | Separate interactive-session projection and rendering. |

The Hub domain stays outside `fleet_hub.py` to avoid extending that module's
already broad schema and request surface.

## Acceptance tests

### Hub and client

- schema 13 upgrades to 14 without changing existing event, claim, cloud,
  model, Grok Bot, or preference rows
- a fresh schema 14 database opens from every supported request path
- caller node identity overrides or rejects any body node identity
- active retry preserves `started_at`; refresh replaces bounded mutable fields
- end is fenced to the exact row and idempotent
- expired and ended rows are absent by default and present under `all=1`
- admin, node, revoked, unknown, and dashboard-cookie auth boundaries match the
  contract
- malformed, oversized, absolute, traversal, control-bearing, and secret-like
  remote inputs are rejected without partial writes
- client errors never include bearer values or raw response bodies

### Identity and overlap

- HTTPS, SSH, SCP-like, credential-bearing, and `.git` remote forms normalize
  to the same safe repository identity
- a missing remote produces node-scoped identity and never cross-matches nodes
- rename, spaces, non-ASCII names, count truncation, and byte truncation are
  parsed deterministically
- exact overlap warns once, same-session rows are excluded, and disjoint paths
  do not warn
- truncated observations mark the result partial

### Harnesses and UI

- Claude SessionStart, relevant PostToolUse, accepted Stop, blocked Stop, Hub
  outage, and hook timeout preserve the existing stdout envelope contract
- Codex/T3 thread identity publishes once through work brief without printing
  the session payload
- Cursor's managed session-start, post-tool, and session-end hooks publish the
  expected lifecycle, preserve start context, and consume stdin safely
- `fleet sessions` text and JSON stay bounded and terminal-safe
- the Command Deck escapes every field, shows sessions separately, and never
  counts them against 10/8/4 station capacity
- no prompt, transcript, tool input, diff, file content, remote credential,
  bearer, or fencing token appears in SQLite, HTTP JSON, HTML, hook stdout, or
  logs

## Rollout

1. Merge and run the full Brigade verification gate once.
2. Back up the Hub database and deploy the Hub first so schema 14 and the new
   routes are available.
3. Deploy Rocinante, Shadowfax, and Gandalf clients without copying identities
   or tokens.
4. Update managed Claude and Cursor hooks through their normal merge-safe
   installers. Update the Codex/T3 work-loop hook only through its managed user
   configuration path.
5. Start one bounded session per host, modify distinct scratch paths, and prove
   no warning. Modify the same fake path on two hosts and prove one warning.
6. End the Claude and Cursor sessions, then wait for one Codex TTL expiry.
   Confirm all disappear from the active view while history remains under
   `--all`.
7. Confirm claims, model policy, cloud policy, Grok Bot projections, deck config,
   and station capacities remain unchanged.

Rollback may run the prior client against schema 14 because the migration is
additive. The prior Hub binary must be refused after schema 14 rather than
silently opening a newer database. Restore the pre-deploy backup only if a full
Hub rollback is required.

## Out of scope

- cross-worktree file locks or automatic claim acquisition
- automatic interruption, cancellation, rebase, merge, or branch mutation
- diff or file-content transport
- cloud-provider task internals already covered by the cloud tracker
- durable fleet plans, campaigns, dependencies, and agent assignment
- hard host-wide worker admission beyond existing model and cloud leases
