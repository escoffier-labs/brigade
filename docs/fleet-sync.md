# Fleet sync design

(Tracked home for the former local `docs/plans/fleet-sync-design.md`; `docs/plans/` is gitignored.)

Tracking doc for epic #1121: one answer to "what work is running where, by
which agent/harness, at what status" across rocinante, shadowfax, and
gandalf. The full problem statement, prior art, and the hub-vs-coordinator-free
decision live on the epic; this page records the shape we are building and the
status of each phase.

## Shape

- Each machine's **local run journal stays the source of truth**
  (`.brigade/runs/<run>/events/*.jsonl`, digest-chained). A machine never
  depends on the hub to do its own work.
- A **single small hub** (`brigade fleet serve`) runs on a dedicated hogwarts
  LXC CT, bound to its Tailscale IP, bearer-token auth, one SQLite file in WAL
  mode. It is an aggregator (and, from Phase 4, a claim arbiter), not the
  source of truth.
- On every journal append a machine POSTs the event, denormalized with its
  identity (`node_id`, `repo`, `seat`, `harness`), to the hub. If the hub is
  unreachable the event is spooled locally and flushed in order on reconnect;
  the hub dedupes on `(node_id, run_id, sequence, digest)` so replays are
  idempotent.
- Hub address and token files live in `~/.brigade/fleet.toml`
  (`[fleet] hub_url = ..., node_token_file = ..., token_file = ...`);
  `BRIGADE_FLEET_HUB_URL`, `BRIGADE_FLEET_NODE_TOKEN`, and
  `BRIGADE_FLEET_TOKEN` override. Tokens are dedicated fleet secrets and are
  never written into a Brigade config value.

## Trust model

Two kinds of bearer token (#1150), both compared in constant time, neither
ever logged or rendered:

- **A node token is the node's identity.** `brigade fleet nodes add <node_id>`
  mints one per machine; the hub stores only its SHA-256 (`nodes` table:
  `node_id`, `token_sha256`, `label`, `created_at`, `revoked_at`; schema
  v4) and prints the plaintext exactly once. `POST /events` and
  `POST /claims` derive the caller's `node_id` from the token and answer
  403 when a body `node_id` differs — a fleet member can post events and
  acquire, renew, release, supersede, or inspect claims only as itself. A
  revoked token answers 401; `nodes add` again rotates it. Node tokens may
  also read `/status` and `/claims`, so `brigade fleet status` and
  `brigade fleet claims` work on every machine.
- **The admin token is the control plane.** The one shared bearer from
  before (`BRIGADE_FLEET_TOKEN` / `--token-file` on the hub, `token_file`
  on a client) manages `/nodes`, reads `/status` and `/claims`, and enrols
  the dashboard cookie. It may post events or claims under *any* `node_id`
  only when the hub runs with `brigade fleet serve --allow-admin-writes`
  (off by default), which is the explicit switch for a fleet that is still
  on the shared token. A client with `token_file` but no `node_token_file`
  falls back to sending the admin token as its own credential and logs one
  WARNING per process naming the deprecated shared-token mode; a 403 from a
  hub without the flag keeps the event in the spool (like a 401) until the
  machine is enrolled.
- **Scope and owner fields are honest-client intent, not authorization.**
  `node_id` is now bound to the credential, but `lock`, `supersede`,
  `holder`, `conductor`, and `scope` remain caller-asserted: `scope`
  keeps honest clients from stealing each other's claims (a same-basename
  workspace, a cloned node identity), and any enrolled node may still
  `force`-release a claim — attributed to its own `node_id`, which is the
  difference from before. Everything else on the hub (Tailscale-only bind,
  plain HTTP on the encrypted tailnet, dashboard cookie derived from the
  admin token) is unchanged.

## Decisions (closed)

| Question | Decision |
| --- | --- |
| Hub host | hogwarts, dedicated LXC CT (isolated from the working box) |
| Token source | dedicated secrets via env (`BRIGADE_FLEET_TOKEN`, `BRIGADE_FLEET_NODE_TOKEN`) or file paths (`token_file`, `node_token_file`); never a config value |
| Caller identity | per-node credentials (#1150): the hub binds `node_id` to the node token; the shared bearer is the admin token |
| Git-ref journal backup per machine | no; local journal + hub is enough |
| Dependencies | stdlib only (`http.server`, `sqlite3`, `urllib`) |

## Phases

| Phase | Issue | Status |
| --- | --- | --- |
| 1. Machine identity, namespaced run ids, lease reaper | #1122 | shipped (#1129) |
| 2. Fleet hub (`brigade fleet serve`), event POST, store-and-forward, `brigade fleet status` | #1123 | **built, hub CT pending** — code, tests, and CLI are in; the hogwarts CT that hosts `brigade fleet serve` is not provisioned yet |
| 3. Web dashboard served by the hub | #1124 | shipped |
| 4. Server-arbitrated claims with TTL | #1125 | shipped (#1140); crash self-lockout recovery #1141 |
| 5. Export and Dolt sink for versioned history (optional) | #1127 | built |
| 6. Cross-repo campaigns on the hub | #1128 | not started |

## Phase 2 surface

Hub (`src/brigade/fleet_hub.py`):

- `GET /health` — no auth, liveness.
- `POST /events` — node token (or the admin token with
  `--allow-admin-writes`); one event object or an array (max 1000), every
  event carrying the caller's `node_id` (403 otherwise); a batch with any
  invalid event is rejected whole; responds `{"accepted": n, "duplicate": m}`.
- `GET /status` — admin or node token; latest event per `(node_id, run_id)`
  for runs whose latest state is not terminal (`run.completed`,
  `run.failed`, `run.interrupted`); `?all=1` includes terminal runs. A
  terminal event that omitted `seat` keeps the last non-empty seat from
  earlier events on that run.
- `GET /preference` — admin or node token; the one-row fleet run preference
  pin (`impl`, `review`, `chef`, `notes`). This is a routing overlay, not
  roster sync. `--worker` and a spoken seat name always win.
- `PUT /preference` — admin token only; replace the pin. Seat names only;
  tokens, env values, and home paths are rejected.
- `GET /nodes`, `POST /nodes` — admin token only; list enrolled nodes,
  `{"action": "add", "node_id", "label"?}` (the token is in that response
  and nowhere else; an enrolled, unrevoked node answers 409),
  `{"action": "revoke", "node_id"}`.
- SQLite in WAL mode, `PRAGMA user_version` = schema version (v4 adds the
  `nodes` table; v5 adds the one-row `run_preference` table); a database
  from a newer hub is refused rather than reinterpreted.
- `brigade fleet serve --host <ip> [--port 3774] [--db PATH] [--token-file PATH] [--allow-admin-writes]`;
  `--host` is required so the hub never binds all interfaces by accident.
- `brigade fleet nodes add <node_id> [--label L] [--json]`, `nodes list
  [--json]`, `nodes revoke <node_id> [--json]` — the control plane, using
  the admin token from `[fleet] token_file` / `BRIGADE_FLEET_TOKEN`.
- `brigade fleet preference get|set|pull` — read or replace the hub pin
  and refresh `~/.brigade/run-preference.toml`. `brigade fleet status`
  prints the same pin (and JSON includes `"preference"`). `brigade run`
  honors `impl` as the default worker when `--worker` is omitted and that
  seat exists locally. `--worker` and a spoken roster seat name still win;
  two named seats leave worker unset so chef plans, and the planner-visible
  prefix is only added in that fallback. The hub is the pin, not the
  source of truth: a down hub keeps the last cache.

Client (`src/brigade/fleet_client.py`, hooked from `run_journal.append_event`
after the journal write completes and the lock is released):

- No hub configured → no-op, no spool directory is created.
- One HTTP request per append, run on a worker thread joined with a hard
  2s deadline that covers name resolution, connect, and the response (a
  hostname `hub_url` with a dead resolver cannot block the writer). Any
  failure spools to `<brigade-home>/fleet-spool/<node_id>.jsonl` and
  returns. `Exception` never escapes; `KeyboardInterrupt`/`SystemExit` are
  re-raised only after the event is durable in the spool.
- Spool mutations take a per-node lock file (`flock` on POSIX,
  `msvcrt.locking` on Windows) so concurrent runs on one node cannot erase
  each other's events. Partial flushes rewrite via temp file + `os.replace`;
  a flush that delivers nothing leaves the file untouched.
- The spool is capped at 16 MiB (oldest events dropped, logged on the
  `brigade.fleet` logger). A non-retryable 4xx (anything but
  401/403/408/429) drops that batch as poison instead of retrying forever;
  401, 403 (a credential bound to another node, or the admin token on a
  hub without `--allow-admin-writes`), 5xx, and network failures keep the
  spool.
- When a spool exists, new events are appended to it and the spool is flushed
  in order (100 per request) so the hub never sees a node's events out of
  sequence. `brigade fleet flush` drains it explicitly.
- Hub traffic never uses a proxy (#1154): the client's opener is built with
  an empty `ProxyHandler`, so `HTTP_PROXY`/`HTTPS_PROXY` can neither receive
  the bearer token nor blackhole tailnet requests. Prefer HTTPS via Tailscale
  Serve where possible. Redirects are followed only within the same origin,
  compared with default ports normalized (`http://hub` and `http://hub:80`
  are one origin); a cross-origin 302 is refused instead of replaying the
  Authorization header to another host (#1157).
- The spool is private (#1154): the spool directory is created 0700, spool,
  lock, and temp files are created 0600 (existing files forced back to 0600
  on every open), opens are no-follow (`O_NOFOLLOW`
  where available) and refuse non-regular files and symlinked spool
  directories, and atomic rewrites use
  exclusive, unpredictable temp names in the spool directory. The
  directory's privacy is enforced through a descriptor, not the path
  (#1157 round 2): it is opened `O_DIRECTORY|O_NOFOLLOW`, forced to 0700
  via `fchmod`, and re-stat'ed through the same descriptor; lock and spool
  files are opened `dir_fd` against that validated descriptor. Atomic
  rewrites are descriptor-scoped too (#1157 round 3): the temp file is
  opened `O_CREAT|O_EXCL|O_NOFOLLOW` with an unpredictable name and the
  replace is issued with `src_dir_fd`/`dst_dir_fd`, so both resolve inside
  the validated directory and a concurrent directory swap under the old
  path cannot redirect them. When 0700
  cannot be applied or verified, writes are refused (an event stays
  undelivered) rather than written to a possibly-readable directory.
- `brigade fleet status [--all] [--json]`.

## Interactive session presence

The Hub stores live Claude, Codex/T3, and Cursor editor sessions as a separate
authority from run events and repo claims. Sessions are advisory: they never
acquire a claim, consume station capacity, or block work.

`brigade fleet sessions` lists active rows from `GET /sessions`.
`brigade fleet sessions --all` adds ended and expired history.
`brigade fleet sessions --json` prints the same bounded list the Hub returns.

```json
[
  {
    "node_id": "11111111-1111-4111-8111-111111111111",
    "harness": "claude",
    "session_id": "sess-example",
    "repo_identity": "github.com/example/project",
    "identity_scope": "fleet",
    "repo_label": "project",
    "checkout_path": "/tmp/example/project",
    "branch": "topic",
    "dirty_paths": ["src/a.py"],
    "dirty_truncated": false,
    "state": "active",
    "started_at": "2026-08-29T12:00:00+00:00",
    "heartbeat_at": "2026-08-29T12:01:00+00:00",
    "ended_at": null,
    "ttl_seconds": 900,
    "expires_at": 1787997660.0
  }
]
```

`brigade work brief --json` publishes the current Codex/T3 thread when
`CODEX_THREAD_ID` is set, then projects overlap against other live rows. A
warning never blocks the brief or takes a lock.

```json
{
  "interactive_sessions": {"published": true},
  "overlap_warnings": [
    {
      "node_id": "22222222-2222-4222-8222-222222222222",
      "harness": "cursor",
      "session_id": "sess-other",
      "branch": "main",
      "checkout_path": "/tmp/other/project",
      "age": 120,
      "paths": ["src/a.py"],
      "partial": false
    }
  ]
}
```

Data bounds: at most 64 dirty paths, 512 characters each, 32 KiB encoded JSON,
128-character opaque harness and session IDs, 512-character repository
identity, 256-character label and branch, 1,024-character checkout path. Active
lists cap at 500 rows; history caps at 1,000.

TTL is 120 through 3,600 seconds, default 900. Expiry is a read-time
classification. An upsert after expiry starts a new active interval. Missing
end callbacks rely on TTL; no adapter starts a daemon.

Node credentials remain the write identity. The Hub derives `node_id` from the
bearer token. Public rows never include remotes, tokens, diffs, prompts,
transcripts, or file contents. A credential-bearing origin URL is stored only
as its credential-free identity, such as `github.com/example/project`.

Session heartbeats are not placed in the run-event spool. Replaying an old
heartbeat could resurrect expired presence. A failed heartbeat is discarded;
the next live hook refresh is authoritative. Hub outage is fail-open for
editor hooks.

Harness coverage: Claude `SessionStart` / relevant `PostToolUse` / accepted
`Stop`; Codex and T3 Code through the shared work-loop brief using
`CODEX_THREAD_ID` as harness `codex`; Cursor managed `sessionStart`,
`postToolUse`, and `sessionEnd`. Cursor Cloud stays on the existing cloud
tracker. Overlap warnings require matching repository identity and at least
one identical dirty path. They are advisory only.

The Command Deck renders a separate Interactive sessions panel and marks repo
rows with exact live dirty-path overlap. Those rows never join `LiveRun`,
station `busy`, cloud capacity, run collisions, or outcome history.

## Phase 3 surface

Hub-served Fleet dashboard (`src/brigade/fleet_dashboard.py`, routed by
`fleet_hub.py`), so the fleet view is a page you leave open or check from a
phone over Tailscale:

- `GET /` and `GET /view/machines` — one card per observed `node_id` (never a
  hardcoded host list): live run count, when the hub last heard from it, the
  claims it holds, and a table of its runs (run, repo, seat/harness, state,
  elapsed, last event).
- `GET /view/repos` — one row per observed repo: where it is running (node,
  seat/harness, state, elapsed), its claim (owner node, conductor, TTL
  remaining), its last outcome, and a `collision` flag when two nodes have
  live runs on one repo or a live run is not on the claim owner.
- Attention buckets: `failed`, `awaiting approval` (`run.paused`,
  `approval.requested`, `approval.held`), `stale` (non-terminal with no event
  for 30 min), `running`, `queued`, `interrupted`, `succeeded`. The default
  sort surfaces the first three at the top, oldest first within a bucket.
- Query params: `sort=attention|age|node|repo|state|seat`, substring filters
  `node=`, `repo=`, `seat=` (matches seat or harness), `state=` (bucket or raw
  event type), `attention=1` (needs-attention only), `all=1` (include
  finished runs). Same params on both boards; the board links carry them
  across.
- Server-rendered, stdlib only, no framework or CDN asset. Tables, sorting,
  and filtering are plain HTML + query params and work with JavaScript off;
  refresh is a `<meta http-equiv="refresh" content="10">`. The inline
  script only ticks elapsed timers and adds a client-side text filter.
- Same bearer auth as the JSON endpoints, plus a cookie for phone use: open
  the page once with `?token=<fleet token>` and the hub answers a 303 to the
  same URL without the token and sets `brigade_fleet_view` (HttpOnly,
  SameSite=Strict, 30 days). The cookie value is an HMAC of the token, never
  the token, and it authorizes only the HTML routes: it cannot read
  `/status` or `/claims` or post events or claims, and rotating the hub token
  invalidates every cookie. Tradeoff: the token transits once in a URL (it
  lands in that device's browser history; the hub logs nothing) and the
  cookie is a 30-day read-only capability on that device — treat the device
  like a tailnet member, and rotate the token if it is lost. Tailscale
  encrypts the link, so the cookie is not marked `Secure` (the hub is plain
  HTTP on the tailnet).
- No token, cookie value, or claim holder token is ever rendered; the
  response carries the same CSP / no-store / no-referrer headers as
  `brigade center serve`.

## Phase 4 claims

The claim key is the bare workspace directory name, matching the `repo` key
on fleet events. Unrelated repositories with the same directory basename
therefore arbitrate against each other. Separate worktrees of one repository
have different directory names and do not arbitrate against each other when
each resolves its own workspace identity. If they instead resolve the
per-user `~/.brigade/node.toml` fallback, every such worktree under the home
directory uses the home directory name and arbitrates on that shared key.

Claims are best-effort protection layered over the local run lock. If the hub
rejects the configured token with HTTP 401 or 403, the client logs one WARNING
(carrying the hub's message) and continues under the local lock alone, just as
it does when the hub is unreachable.

Lost ownership fails closed by default (#1152): when the mid-run heartbeat
learns another owner holds the claim (a renew answered 409 held-by-another),
the run is aborted — with no callback the main thread is interrupted through
the same path as Ctrl-C and the loss is logged once — so two machines never
keep working one repo unarbitrated. The abort does not depend on the
callback behaving (#1157 round 2): a callback that blocks is cut off after a
bounded grace period (`CLAIM_LOST_CALLBACK_GRACE_SECONDS`) and one that
raises `SystemExit` or `KeyboardInterrupt` is logged; the interrupt fires
either way, after the callback has had its chance to record state. The
grace-boundary fire and the callback-completion fire share one atomic
check-and-set, so the abort interrupts exactly once (#1157 round 3).
`repo_claim` accepts `on_claim_lost` to react differently, and
`BRIGADE_FLEET_CLAIM_LOSS=continue` (or
`claim_loss_policy="continue"`) is the documented opt-out for solo machines,
restoring the old log-and-continue behavior. `brigade run` records a mid-run
claim-loss abort as a failed run with failure kind `fleet-claim-lost`, never
as "canceled by user"; the receipt is classified and written on the main
thread from the heartbeat's marker (#1157 round 2), so a heartbeat-side
receipt-write failure cannot downgrade the abort to a user cancel. The CLI's
abort callbacks record their classification markers before any stderr
diagnostic and interrupt from a `finally` (#1157 round 3), so a failed
stderr write can neither swallow a credential-refusal abort (kind
`fleet-credentials-rejected`) nor downgrade a lost-claim abort to a user
cancel.

Off-main-thread owners (#1157 round 2): `_thread.interrupt_main()` targets
the process main thread, so when `repo_claim` is entered from a worker
thread the abort is delivered cooperatively instead — the yielded decision's
`cancel_event` is set and a WARNING names it. Guarded work entered off the
main thread MUST observe `decision.cancel_event` and unwind promptly; that
check is the documented requirement for non-main-thread claim owners (the
CLI always enters on the main thread and is unaffected).

Orphan cleanup (#1157): a lost acquire response schedules a background
release that attempts **immediately**, then backs off, so a short-lived run
still frees the row before process exit kills its daemon threads. A
"missing" answer inside one outstanding-request uncertainty window is
retried rather than accepted as definitive, since the abandoned acquire can
still commit its row afterwards. At exit,
when a heartbeat re-acquire may have committed an orphan row, the release is
retried inline not only while the hub answers "unavailable" but also after a
definitive "missing" (the straggler may commit right after), each retry is
spaced across one request deadline so back-to-back retries cannot all
complete before the straggler lands, and exhaustion
of those retries is logged as a WARNING naming the residual TTL exposure.

## Export and optional Dolt sink

`brigade fleet export [--since <event-timestamp> | --since-received
<hub-timestamp>] --format jsonl|csv [--claims [--include-expired]] [--db PATH]
[--out PATH]` reads
the hub database in read-only mode and streams events in `(node_id, run_id,
sequence, digest)` order. The columns have a fixed order, the event digest is
included, and exporting the same database twice produces identical bytes.
`--claims` exports the live claims snapshot in `target` order, omits the claim
holder token, and includes an explicit `expired` column. Expired rows are
excluded unless the human export also passes `--include-expired`. `--since`
filters on the event's `ts`. `--since-received` filters on the hub's
`received_at` value. Incremental archives should use `--since-received` because
a spooled event may reach the hub days after its event timestamp. An event whose
selected filter timestamp cannot be parsed is excluded and counted in the
stderr summary. Human-facing CSV cells that start with `=`, `+`, `-`, `@`, a
tab, or a carriage return are prefixed with a single quote to prevent
spreadsheet formula execution. JSONL values are unchanged. A named `--out`
file is replaced only after the complete export succeeds.
The replacement uses the process's normal file-creation mode, so re-exporting
does not replace a group/world-readable file with the private temp-file mode.

Export and sink work stay off the reporting path. Neither command participates
in journal append, event delivery, claim arbitration, `/status`, `/claims`, or
dashboard requests. Run them manually or from a scheduler on the host that can
read the hub SQLite file.

The Dolt sink is disabled by default. Configure one import pass in the existing
`~/.brigade/fleet.toml` file:

```toml
[fleet.sink]
enabled = true
dolt_binary = "dolt"
dolt_dir = "~/.brigade/dolt"
db = "~/.brigade/fleet-hub.db"
timeout_seconds = 120
events_incremental = true
```

`enabled` must be the Boolean `true`. `dolt_binary` may name a binary on
`PATH` or an explicit path. `dolt_dir` is the destination Dolt database, and
`db` optionally overrides the hub SQLite file. A command-line `--db` takes
precedence over the configured `db` value. `timeout_seconds` bounds each Dolt
subprocess. With `events_incremental = true`, the sink stores the last exported
`received_at` value in `.brigade-fleet-events-watermark` inside `dolt_dir` and
exports only events received after it on the next pass. The watermark is
replaced atomically after successful table imports, even when the Dolt working
set is unchanged and no commit is needed. A missing or invalid watermark
causes a full event export. Set `events_incremental = false` to export the full
event log on every pass.

`brigade fleet sink [--db PATH]` exports deterministic CSV files. It runs
`dolt table import -u` for `fleet_events`, which is an append log, and `dolt
table import -r` for `fleet_claims`, which is a live snapshot. The claims
snapshot excludes expired rows. Replacing it removes released or expired claims
from Dolt on the next pass. The sink CSV writes SQL NULL as Dolt's `\N` import
marker and Boolean claim expiry as `0` or `1`; JSONL continues to use `null` and
JSON Booleans. After both imports, the sink reports unparseable incremental
`received_at` values on stderr and checks `dolt status --porcelain`. A changed working set
is staged with `dolt add -A` and committed with the UTC export time followed by
`fleet events/claims`. That same time is the expiry cutoff for the claims
snapshot. An unchanged pass skips the add and commit commands.
A fresh Dolt database is initialized with the local `brigade` identity, so no
host-level Dolt identity is required. The sink has no Python Dolt dependency.
If it is disabled, it exits 0 without reading the hub database. If the
configured Dolt binary is absent, it prints a clear `documented no-op` message
and exits 0, which keeps an optional scheduled job from failing on a host
without Dolt.

For example, this Dolt SQL reports each ISO week's terminal-run outcome rate
and the percentage-point change from the prior week. `ts` is stored as an
ISO-8601 string with a trailing `Z`. go-mysql-server may not parse that value
directly in `DATE_FORMAT`, so the query strips the suffix before casting:

```sql
WITH weekly AS (
  SELECT
    DATE_FORMAT(CAST(SUBSTRING(ts, 1, 19) AS DATETIME), '%x-%v') AS iso_week,
    SUM(state = 'run.completed') /
      NULLIF(SUM(state IN ('run.completed', 'run.failed', 'run.interrupted')), 0)
      AS outcome_rate
  FROM fleet_events
  WHERE state IN ('run.completed', 'run.failed', 'run.interrupted')
  GROUP BY iso_week
), compared AS (
  SELECT
    iso_week,
    outcome_rate,
    LAG(outcome_rate) OVER (ORDER BY iso_week) AS prior_rate
  FROM weekly
)
SELECT
  iso_week,
  ROUND(outcome_rate * 100, 1) AS outcome_rate_pct,
  ROUND((outcome_rate - prior_rate) * 100, 1) AS weekly_diff_pct_points
FROM compared
ORDER BY iso_week;
```

Run the hub under a process supervisor on the CT, e.g.
`brigade fleet serve --host "$(tailscale ip -4)" --token-file /etc/brigade/fleet-token`.

## Phase 4 surface (claims)

Hub-arbitrated repo claims (`POST /claims`, `GET /claims`,
`brigade fleet claims`) are described in full in the CHANGELOG entry for
#1125. Recovery from a crashed run (#1141):

- Every claim row records the acquiring run's local `run.lock` lease
  (`lock`: owner token, `acquired_at`, `run_dir`; never serialized; hub
  schema v3, live rows survive the upgrade).
- A run killed with SIGKILL leaves an unexpired hub claim under its own node
  whose holder token died with it. On the next `brigade run` in that repo,
  the Phase 1 lease reconcile frees `run.lock` for the dead owner, and
  because it did, the hub acquire is sent with `scope: "node"` and that dead
  lease as `supersede`: the hub replaces only the exact row taken under that
  lease (same `node_id`, same lease token, lease stamp not newer) and the
  run logs one line naming the superseded claim. No TTL wait. A claim held
  by another node, by a run in another same-name workspace, on a cloned
  node identity, or without a recorded lease is never touched: the acquire
  is refused with the two ways out named in the error, and so is a
  same-node claim with no dead lock to vouch for it (a sibling run, or a run
  already recovered with `brigade runs recover`).
- `brigade fleet claims --release <key> [--path] [--node NODE_ID] [--force] [--json]`
  frees a claim without its holder token (`POST /claims` `release` with
  `scope: "node"`): the hub deletes the row only if the given node owns it;
  another node's claim is refused with its owner named. `<key>` is always a
  claim key; with `--path` it is the workspace directory itself (a directory
  inside a workspace is refused, never resolved upward). Without `--force`,
  both modes run the same proof that the run which took the claim is dead:
  `POST /claims` `inspect` returns the recorded run directory to the owner
  node only, the CLI maps it to a workspace on this machine (`run.json`
  `lock_workspace` / `cwd`, or the `.brigade/runs/<id>` layout; with
  `--path` it must be the workspace given) and refuses while that
  `run.lock` has a live owner or is malformed, or when it cannot resolve
  the run at all, or when the probe finds no claim owned by this node.
  The release then carries the inspected row's `acquired_at` — the hub
  refuses a token-less node-scoped delete without it — and deletes only
  that exact row (one write transaction), so a claim re-acquired in
  between is refused with the current row. `--node` other than this machine's identity needs
  `--force`; `--force` (`scope: "force"`) skips the proof and the JSON
  receipt's `forced` reflects the flag. Renew is never token-less.
- `brigade run --no-fleet-claim` skips the hub claim entirely and relies on
  the local run lock alone, logged once on the `brigade.fleet` logger.
- Trust boundary: `node_id` is bound to the caller's node token (#1150; see
  *Trust model* above), so `--node` for another machine's identity is a 403
  from the hub unless the caller is the admin token on a hub started with
  `--allow-admin-writes`. The leases and `scope` remain caller-asserted
  intent that keeps honest clients from stealing each other's claims, not
  an authorization — any enrolled node may still `--force`, attributed to
  its own `node_id`.
