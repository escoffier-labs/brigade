# Fleet sync design

(Tracked home for the former local `docs/plans/fleet-sync-design.md`; `docs/plans/` is gitignored.)

Tracking doc for epic #1121: one answer to "what work is running where, by
which agent/harness, at what status" across the workstation, GPU box, and
desktop. The full problem statement, prior art, and the hub-vs-coordinator-free
decision live on the epic; this page records the shape we are building and the
status of each phase.

## Shape

- Each machine's **local run journal stays the source of truth**
  (`.brigade/runs/<run>/events/*.jsonl`, digest-chained). Local journaling
  continues during a hub outage. Hub-based model admission can still refuse launches.
- A **single small hub** (`brigade fleet serve`) runs on a dedicated Proxmox
  LXC CT, bound to loopback behind Tailscale Serve HTTPS, bearer-token auth, one SQLite file in WAL
  mode. It is an aggregator (and, from Phase 4, a claim arbiter), not the
  source of truth for local event history.
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

## Cloud concurrency controls

`brigade fleet cloud` remains a read command. Use `--json` to see the effective
`policy.global_limit` and each provider's `limit`, enabled state, and usage.
`--all` also includes released and expired leases.

With the administrator token already configured in `[fleet] token_file` or
`BRIGADE_FLEET_TOKEN`, change both limits in one request:

```bash
brigade fleet cloud set --global-limit 8 --provider codex --limit 8 --json
```

To change one limit, use `fleet cloud set --global-limit 8` or
`fleet cloud set --provider codex --limit 8`. Both limits accept integers in
`0..64`. A provider name and its limit must be supplied together. Node tokens
cannot make these policy changes. The command uses the existing administrator
authentication path and does not enable administrator lease writes.

The hub validates the entire request before committing either limit. Omitted
fields retain their current values, including provider enabled state, hosted
classification, circuit state, and subscription metadata. Updates leave active
leases and model retirement policy intact. Lowering a limit, including to zero,
blocks new admissions above that limit while existing leases can still renew or
release. A persisted global override survives restart and takes precedence over
the startup configuration. Without an override, the startup default still applies.

These are Brigade's internal hosted-worker admission limits. Ordinary local
Codex workers use the separate model-seat lease limit. These controls do not
describe or change an OpenAI account limit.

Upgrade the hub before using the writer. The client checks the hub's cloud
schema before sending a policy update and refuses an older hub. Schema v25 adds
the persisted global override. An older Brigade cannot open the upgraded database.
The override has no reset command. Set it explicitly to the desired value to
change it again. Explicit null limits are rejected. For direct policy API writes,
omitted reason and subscription fields remain even when enabling a provider.

## Native launch model bindings

A hub seat's canonical `model` can differ from the exact model ID accepted by
its execution harness. `fleet models set` accepts separate `--brigade-model`
and `--t3-model` bindings. `--t3-model` requires a `--t3-instance-id` binding and
projects as `bindings.native.model` for T3. Brigade workers use only their
`bindings.brigade` launch model. Without a launch override, execution falls back to the
canonical ID. `--clear-t3-model` removes the T3 override.

For a legacy roster that has not activated policy authority, use the current
roster revision from `brigade fleet models list --json`:

```bash
brigade fleet models set opencode model-example seat-example --enable \
  --reasoning medium --t3-instance-id instance-example \
  --t3-model opencode/model-example --expect-revision <REVISION> --json
```

Enabling a seat or selecting a consumer default refuses retired canonical or
native launch models. Policy activation merges native binding fields
individually, preserving an existing instance ID without dropping the mapped
launch model. Schema 32 stores `model_policy.t3_model` and `events.context`.
Startup repairs either missing additive column even if the database already
reports version 32. Upgrade the Hub before using the new T3 binding or
[lifecycle reporter](fleet-t3-events.md).

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
  on a client) manages `/nodes`, reads `/status` and `/claims`, and mints
  dashboard enrollment codes. Browser sessions are independent read-only
  credentials, with fixed expiry and individual revocation.
  It may post events or claims under *any* `node_id`
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
  difference from before. Dashboard session authority is described below.

## Decisions (closed)

| Question | Decision |
| --- | --- |
| Hub host | dedicated Proxmox LXC CT (isolated from the working box) |
| Token source | dedicated secrets via env (`BRIGADE_FLEET_TOKEN`, `BRIGADE_FLEET_NODE_TOKEN`) or file paths (`token_file`, `node_token_file`); never a config value |
| Caller identity | per-node credentials (#1150): the hub binds `node_id` to the node token; the shared bearer is the admin token |
| Git-ref journal backup per machine | no; local journal + hub is enough |
| Dependencies | stdlib only (`http.server`, `sqlite3`, `urllib`) |

## Phases

| Phase | Issue | Status |
| --- | --- | --- |
| 1. Machine identity, namespaced run ids, lease reaper | #1122 | shipped (#1129) |
| 2. Fleet hub (`brigade fleet serve`), event POST, store-and-forward, `brigade fleet status` | #1123 | **built, hub CT pending** — code, tests, and CLI are in; the Proxmox CT that hosts `brigade fleet serve` is not provisioned yet |
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
- `GET /status`: admin or node token. Latest event per `(node_id, run_id)`
  for nonterminal runs received within 30 minutes. Terminal states include
  `run.completed`, `run.failed`, `run.interrupted`, `run.orphaned`,
  `verify.completed`, and external completion, failure, cancellation,
  cancellation acknowledgement, or expiry, plus any state ending in
  `.cancelled`, `.timed_out`, or `.timeout`. `?all=1` includes history. A
  terminal event that omitted `seat` keeps the last non-empty seat from
  earlier events on that run.
- `GET /preference` — admin or node token; the one-row fleet run preference
  pin (`impl`, `review`, `chef`, `research`, `security`, `scout`, `notes`).
  This is a routing overlay, not roster sync. `--worker` and a spoken seat
  name always win.
- `PUT /preference` — admin token only; replace the pin. Seat names only;
  tokens, env values, and home paths are rejected.
- `GET /deck/roster`, `POST /deck/roster` — the hub roster page (spec:
  `docs/phase-fleet-roster-deck-page.md`). Read with the admin bearer, the
  dashboard cookie, or a trusted Tailscale identity (read-only). Save with
  an admin Authorization bearer header only. Cookies and trusted identities
  cannot edit, and their pages contain no admin CSRF value. The form carries a CSRF
  token derived from the admin token, the roster revision, and the
  preference row's `updated_at`. One Save is one `BEGIN IMMEDIATE`
  transaction: seat on/off and consumer defaults bump the roster revision
  once; roles, notes, and cloud lane on/off do not touch the revision.
  Schema v19 adds the three role columns.
- `GET /nodes`, `POST /nodes` — admin token only; list enrolled nodes,
  `{"action": "add", "node_id", "label"?}` (the token is in that response
  and nowhere else; an enrolled, unrevoked node answers 409),
  `{"action": "revoke", "node_id"}`.
- SQLite in WAL mode, `PRAGMA user_version` = schema version (v4 adds the
  `nodes` table; v5 adds the one-row `run_preference` table; v19 adds three
  role columns to `run_preference`); a database from a newer hub is refused
  rather than reinterpreted.
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
  401/403/408/429) rejects the whole batch, so the client bisects it: valid
  sub-batches are delivered in order and each event the hub rejects on its
  own moves to `<node_id>.rejected.jsonl` beside the spool (the newest 200
  records, each with the event's sequence, digest, timestamp, and a
  sanitized reason; bodies over 16 KiB are omitted). A transport failure
  during bisection keeps every unprocessed event queued. 401, 403 (a credential bound to another node, or the admin token on a
  hub without `--allow-admin-writes`), 5xx, and network failures keep the
  spool.
- When a spool exists, new events are appended to it and the spool is flushed
  in order (100 per request) so the hub never sees a node's events out of
  sequence on this journal path. The explicit `fleet events report` path sends
  its own observation first, independently of a backlog, and returns a delivery
  receipt. `brigade fleet flush` drains the shared spool explicitly.
- Hub traffic never uses a proxy (#1154): the client's opener is built with
  an empty `ProxyHandler`, so `HTTP_PROXY`/`HTTPS_PROXY` can neither receive
  the bearer token nor blackhole tailnet requests. Prefer HTTPS via Tailscale
  Serve for remote hubs. Remote cleartext HTTP is refused before credentials
  are attached. Redirects are followed only within the same origin,
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

The Command Deck's Activity panel separates managed jobs from reported agent
sessions, grouped by node, harness, and session id. Each group's checkout
memberships stay in native details. The first eight groups are visible, with
the rest folded. At 500 memberships, the list may be capped and the session
total is unknown. Exact live dirty-path overlap warns on job and repo rows.
Presence never joins `LiveRun`, station `busy`, cloud capacity, or outcome history.

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
  remaining), its last outcome, and neutral `parallel activity` details when
  active runs on two machines report the same nonempty repo label. This does
  not prove repository identity or conflicting files, and adds no attention
  priority. Claim-owner differences remain visible in the claim and run columns.
- Attention buckets: `failed`, `awaiting approval` (`run.paused`,
  `approval.requested`, `approval.held`), `unknown`, `stale` (non-terminal with no event
  for 30 min, or a T3 observation older than 900 seconds), `running`, `queued`,
  `interrupted`, `succeeded`. The default sort surfaces failed, awaiting approval,
  unknown and stale runs at the top, oldest first within a bucket.
- Query params: `sort=attention|age|node|repo|state|seat`, substring filters
  `node=`, `repo=`, `seat=` (matches seat or harness), `state=` (bucket or raw
  event type), `attention=1` (needs-attention only), `all=1` (include
  finished runs, still paginated), `offset=` (next LIMIT page). Same params
  on both boards; the board links carry them across.
- Board queries reuse latest-state-per-run. The default fetch windows
  terminal history to the deck horizon (`stale_history_after_seconds`,
  default 24h) so the 10s refresh does not scan the whole journal; `all=1`
  drops the window and still applies LIMIT plus a `more` link. Start times
  and node cards are derived from those rendered rows. GET never prunes
  events; operators compact the journal from the Proxmox runbook.
- Server-rendered, stdlib only, no framework or CDN asset. Tables, sorting,
  and filtering are plain HTML + query params and work with JavaScript off;
  refresh is a `<meta http-equiv="refresh" content="10">`. The inline
  script only ticks elapsed timers and adds a client-side text filter.
- Admin bearer headers can read HTML and enable roster/policy editors.
  Browser sessions and trusted Tailscale identities read HTML only. A cookie
  never authorizes a JSON API or an HTML mutation, even with valid CSRF and
  Origin headers. CLI edits remain available through `fleet models set`,
  `fleet preference set` and `fleet policy` commands.

### Browser enrollment and lost-device revocation

On an administrator's configured client, run:

```bash
brigade fleet enroll --label "phone"
```

The command prints a URL containing a random single-use code valid for exactly
five minutes. Open it on the intended device and confirm the form. GET only
shows confirmation, so a link preview cannot consume the code. POST requires
same-origin browser signals. Browsers sending neither Origin nor
`Sec-Fetch-Site: same-origin` cannot enroll. The code is a transferable
read-only capability until consumed or expired, so keep the printed URL private.
The admin bearer never appears in a generated URL. Bearer query enrollment is
refused on all HTML routes, including when another credential is present.

Confirmation creates a separate random session credential in the host-only
`brigade_fleet_view` cookie with Path=/, HttpOnly, SameSite=Strict, and an
absolute 30-day expiry. Page reads check the database for live expiry and
revocation without extending that expiry. Only credential digests are stored.
The fixed redirect is `/deck`. Old HMAC cookies cease authenticating on upgrade,
so existing devices must enroll again. Rotating the admin bearer leaves these
sessions valid and individually revocable.

```bash
brigade fleet sessions --dashboard
brigade fleet sessions --dashboard --all --json
brigade fleet sessions --dashboard --after SESSION_ID
brigade fleet sessions --dashboard --revoke SESSION_ID
```

List and revoke each unwanted session by its non-secret id and optional device
label. Listings return safe metadata only. Active pages contain at most 100
records, with `next_after` in JSON and a next-page command in text. `--all`
includes revoked and expired records. Repeat revocation preserves its original
timestamp. During an incident, repeat listing from page one because concurrent
insertions before the cursor can be missed. These commands require the admin
token and never fall back to a node credential. Unadorned `fleet sessions` and
its `--all` and JSON behavior still describe interactive editor presence.

Both the configured hub URL and an optional `fleet enroll --base-url` must be
absolute HTTPS origins, or loopback HTTP origins, with no credentials, extra
path, query or fragment. `--base-url` supports a browser-facing HTTPS proxy URL
when administration uses loopback HTTP. The client disables proxy environment
settings, follows no redirects, and caps response reads at 128 KiB.

Secure is set for an actual TLS connection. HTTPS termination at a local proxy
requires `fleet serve --trust-forwarded-proto`, a numeric loopback bind and an
immediate loopback peer. The proxy must preserve the browser-facing Host and
overwrite client-supplied X-Forwarded-Proto with one exact `https` value.
Duplicate, comma-separated or unknown values do not assert HTTPS. Direct HTTP
ignores forwarding headers by default. X-Forwarded-Host/Port are never trusted.
This setting is independent of `--trust-tailscale-identity`, which still grants
HTML reads only. Local processes are inside this proxy trust boundary.

Schema 23 adds dashboard enrollment and session tables at startup, preserving
node, event, claim and interactive presence rows. An older binary refuses this
newer schema on rollback. No migration is performed by enrollment requests.

HTML views omit bearers, session credentials and claim holder tokens. Enrollment
codes appear only in authenticated mint responses, private enrollment URLs and
the escaped hidden confirmation field. Enrollment responses carry no-store
and no-referrer headers.

## Phase 4 claims

The claim key is the bare workspace directory name, matching the `repo` key
on fleet events. Unrelated repositories with the same directory basename
therefore arbitrate against each other. Separate worktrees of one repository
have different directory names and do not arbitrate against each other when
each resolves its own workspace identity. If they instead resolve the
per-user `~/.brigade/node.toml` fallback, every such worktree under the home
directory uses the home directory name and arbitrates on that shared key.

By default, claims fall back to the local run lock when no Hub is configured,
the node has no usable identity, the Hub is unavailable, or acquisition twice
refuses without naming an owner. HTTP 401 or 403 refuses admission with
`FleetClaimAuthError`, including in default mode. Rejected credentials never
authorize local fallback.

`brigade run TASK --require-fleet-claim` requires a confirmed Hub grant before
agent execution. Library callers select the same behavior with
`repo_claim(target, require_claim=True)`. Missing Hub configuration, configuration
read errors, unusable identity, unavailable Hub and repeated ownerless refusal
raise `FleetClaimRequiredError`, a `FleetClaimHeldError` subclass, with a fixed
refusal reason. Existing auth, held, quota and protocol refusals retain their
specific error paths. A late initial grant remains `FleetClaimExpiredError`.

The flag is mutually exclusive with `--no-fleet-claim` and is retained by detached
children. A refused CLI start exits 2, starts no agent, releases its local lock,
and leaves a failed terminal receipt when artifacts are enabled. Ambiguous
acquisition still retries once with the same holder and retains existing cleanup
on its bound connection. Initial auth and held refusals keep their existing
cleanup decisions, including no background retry of rejected credentials. An
uncertain remote claim can remain until TTL expiry or operator recovery.

Fleet claims require **machine-local workspaces**. Node identity is a
per-workspace (or per-home) uuid4 stored in `.brigade/node.toml`, and
deadness is a local pid check against that workspace's `run.lock`. A
workspace shared across machines over a network filesystem (NFS, SMB, or
a bind-mount of the same tree) collapses two machines into one `node_id`
*and* one `run.lock`: machine B's pid check calls machine A's live owner
dead because that PID is not local, and a same-node supersede or
`--release` then fires. Copied `node.toml` files with *separate* local
workspaces stay distinct (different `lock_token`s); a shared mount does
not. Keep each machine's checkout on its own disk.

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
restoring the old log-and-continue behavior. Required claims override both
continuation settings. They also request cancellation on active credential
rejection or an unclassified heartbeat refusal. Credential detail is delivered
once through `on_credential_failure`, without also calling `on_claim_lost` for
that refusal. The existing bounded abort still applies if the credential callback
blocks or is absent. `brigade run` records a mid-run
claim-loss abort as a failed run with failure kind `fleet-claim-lost`, never
as "canceled by user"; the receipt is classified and written on the main
thread from the heartbeat's marker (#1157 round 2), so a heartbeat-side
receipt-write failure cannot downgrade the abort to a user cancel. The CLI's
abort callbacks record their classification markers before any stderr
diagnostic and interrupt from a `finally` (#1157 round 3), so a failed
stderr write can neither swallow a credential-refusal abort (kind
`fleet-credentials-rejected`) nor downgrade a lost-claim abort to a user
cancel.

Confirmed lease deadlines (#1219): `repo_claim` records monotonic time
immediately before each acquire, renew, or re-acquire and accepts a grant
only before request start plus the exact integer TTL sent to the hub. It
also checks the previous confirmed deadline before accepting renew or
re-acquire. Server `expires_at` and client wall time do not govern local
authority. An independent watcher marks authority lost at its first
observation of the deadline, even when a renewal request is still in flight.
Under abort policy it sets `cancel_event` before invoking the one-shot
claim-loss callback with `lease-expired`. A late success cannot restore
authority or restart renewal. TTL1 receives a renewal opportunity at ttl/3.

A delayed initial grant is released with its holder token and refused with
`FleetClaimExpiredError`, a `FleetClaimHeldError` subclass. Continue mode
receives an ungranted `lease-expired` decision. Mid-run continue mode logs
expiry once, stops renew and re-acquire permanently, and keeps work under
the local lock without cancellation, callbacks, or interrupts. Credential
refusal in default mode keeps its separate callback and stops deadline watching.
Required mode routes that callback through bounded abort publication.

Local authority loss requests cancellation. It does not prove the worker
or its process group stopped before hub expiry: callback grace is five
seconds and cancellation of blocked subprocesses remains outside this
slice. Issue #1219 stays open for that residual. Shutdown disarms both
threads before joining and ignores in-flight successes for authority.
If loss wins the authority lock before shutdown, its callback is delivered
once outside the lock even after shutdown begins. If shutdown wins first,
no new loss callback is admitted. Main-thread interrupt dispatch shares
the shutdown lock, so an unfinished callback cannot later interrupt
unrelated work after shutdown. Late renew and re-acquire grants receive
holder-fenced cleanup before notification can block. An unavailable
release uses the existing orphan cleanup. An initial unavailable acquire
also retains that cleanup when its retry returns `missing`, since the
first request may still commit after the retry.

When an initial acquire times out and its retry succeeds, cleanup waits
until admitted work exits. Delayed-grant refusal uses the same cleanup
before abort raises or continue yields an ungranted decision. Both paths
perform an immediate holder-fenced release and two further releases,
each after a 2.5-second pause, even if an earlier release answers `ok` or
`missing`. Each call has a 2.5-second client deadline, so the inline release
sequence has a nominal 12.5-second budget, apart from scheduling overhead
and the existing thread joins. An unresolved unavailable release retains
best-effort background cleanup. A server request can still commit beyond
the cleanup window, which remains #1189. This sequence does not prove
process quiescence, and HIGH2 remains open.

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
holder token, and includes an explicit `expired` column followed by `generation`
and `protocol`. Positional CSV consumers must accept those two trailing columns.
Generation comes from the durable target fence, and protocol is `legacy`.
The exporter validates all fences in the same read snapshot as the claims.
Missing, corrupt, or newer-schema fence state refuses claims export before any
output is written. A pre-generation hub database must first be upgraded to schema
26 by starting the upgraded hub once. Event-only export remains available. Expired rows are
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
from Dolt on the next pass. The sink validates claims before importing either
table. A claims refusal stops the whole pass, including events, without advancing
the watermark. After upgrading Brigade, upgrade the hub database to schema 26
before running the sink.

Existing Dolt claims tables gain nullable `generation` and `protocol` columns
without losing indexes, unrelated tables, or history. Successful snapshot imports
populate both fields. These migrated columns remain nullable, while fresh tables
require them. A downgraded writer can therefore leave null metadata in a migrated
mirror. Dolt history is derived data, and the hub remains the fence authority.
The sink CSV writes SQL NULL as Dolt's `\N` import
marker and Boolean claim expiry as `0` or `1`; JSONL continues to use `null` and
JSON Booleans. After both imports, the sink reports unparseable incremental
`received_at` values on stderr and checks `dolt status --porcelain`. Only an
explicit unsupported-option error, as returned by Dolt 2.4.2, uses a JSON query
against `dolt_status` instead. Other errors, timeouts, and malformed fallback
responses stop the pass. A changed working set
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

Run the hub under a process supervisor on the CT, behind a same-host HTTPS proxy, e.g.
`brigade fleet serve --host 127.0.0.1 --trust-forwarded-proto --token-file /etc/brigade/fleet-token`.

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
  node only, the CLI maps it to a workspace on this machine through
  `runguard.resolve_run_lock_workspace` (`run.json` `lock_workspace`, the
  `.brigade/runs/<id>` layout, then `cwd`; a resolved path that is not a
  directory here is refused; with `--path` it must be the workspace given)
  and refuses while that `run.lock` has a live owner or is malformed, or
  when it cannot resolve the run at all, or when the probe finds no claim
  owned by this node.
  When the row records no run directory (`--no-artifacts`, or a claim
  predating the lease columns), `--path` proves deadness via the
  pointed-at workspace's own `run.lock` instead of requiring `--force`;
  a bare key still cannot verify that row.
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

### Hub request and claim bounds

The Hub checks declared request length before reading or parsing a JSON body.
Bodies above 8 MiB return HTTP 413. Missing, ambiguous, or invalid body framing
returns HTTP 400 and closes the connection, except that a Worklore DELETE with
no body still succeeds. Transfer-Encoding and duplicate
Content-Length headers are refused. Worklore retains its smaller 256 KiB body
limit and 1 MiB import limit. Dashboard enrollment retains its 16 KiB body limit.

Before JSON decoding, regex scans check nesting and aggregate complexity.
A document accepts at most 32 levels of containers, 32768 objects and arrays in
total, and 131072 aggregate items. Each array element or object member counts as
one item. Duplicate object keys count separately during the scan. Punctuation
inside strings does not count. Decoded objects and arrays also accept at most
1000 items each, with aggregate checks repeated after decoding. These checks
cover fields that domain validators ignore. Violations return HTTP 400 before
ingestion starts. An invalid item rejects the entire event batch. Existing event
string bounds still apply.

Each node may hold at most 1024 active claims. A new acquisition beyond that
limit returns HTTP 409 with `reason=claim-cardinality-exceeded`. Direct API callers
receive `ClaimCardinalityError`. Existing claims remain held. Retry, renewal,
release, and replacement of an existing target stay available. The quota check
runs after existing-target decisions inside the acquisition write transaction.
Expired claims do not consume the active quota. An index on
`claims(owner_node, expires_at)` supports the count and is created idempotently
at startup, including on current-version databases, without a schema version bump.

The response includes an empty `owner` object so deployed clients classify the
409 as a definite refusal instead of falling back to local-only work. New fleet
clients classify this refusal as `quota`. `repo_claim` raises
`FleetClaimQuotaError`, a subclass of `FleetClaimHeldError`, before admitting
work. An ambiguous initial acquire followed by quota refusal schedules cleanup
fenced to the original holder token. CLI JSON includes the decision reason.
A heartbeat quota refusal terminates the lease through the existing claim
loss channel. The default policy cancels guarded work.

The 1 MiB Worklore import ceiling predates these changes. A contract-shaped batch
of 500 observations can serialize to about 4.27 MB and exceed it. This
incompatibility still needs a follow-up. The import ceiling remains 1 MiB.

These request bounds leave GET payloads and fleet list clients unchanged.
Claim fencing failures still return HTTP 503 on read paths, including `/status`.
Result-set bounds, keyset pagination, and a current-status projection move to
[the 0.29.0 follow-up](https://github.com/escoffier-labs/brigade/issues/1738).
Retention, POST throttling, per-node fairness behind a proxy, and the fronting
proxy runbook also remain deferred to that issue.


## Adoption statistics collector and Deck page

`GET /deck/stats` uses existing read-only browser enrollment, admin bearer or
trusted proxy identity. It renders the latest snapshot and a weekly series from
the Hub database through the shared Deck theme. Rendering makes no provider calls.
`POST /stats` requires an enrolled node token whose node is in
`fleet.stats.collector_nodes`. Admin tokens are refused even with admin writes
enabled. Replayed snapshots are ignored. `admin_stats_snapshots` keeps the snapshot with the most successful sources per
UTC day, then prefers recency, at most 402 rows within the 400-day retention window.
HTTP 200 acknowledges validation and processing. It does not guarantee that the
returned `snapshot_id` remains stored after same-day compaction. Daily activity
and downloads can therefore come from an older snapshot with more successful
sources, while weekly observations reflect a newer upload.
`admin_stats_weeks` keeps the best observation for each of the latest 60 weeks.
Complete observations beat partial ones, then the newest observation wins.
Hub schema version 36 adds `admin_stats_snapshots` and `admin_stats_weeks`
through the normal startup migration. Startup also preserves weekly history
from the earlier branch schema when present.

Configure the collector and Hub separately in the operator's fleet.toml:

```toml
[fleet.stats]
collector_nodes = ["example-node"]  # Set on the Hub.
database = "example-db"            # Set on the collector.
# Optional collector settings, supplied by the operator:
# account = "example-account"
# worker = "example-worker"
# status_panel = true             # Enable read-only Cloudflare status collection.
# repository_head = "example-git-sha" # Expected source revision shown beside deployment.
# analytics_token_env = "EXAMPLE_ANALYTICS_TOKEN"
# database_id = "example-db-id"      # Select D1 REST instead of wrangler.
# d1_token_env = "EXAMPLE_D1_TOKEN"
# downloads_url = "https://example.invalid/downloads.json"
```

The collector uses its existing wrangler OAuth login for D1 reads when only
`database` is set. Setting `database_id` selects D1 REST, using the account and
the D1 Read token in the environment variable named by `d1_token_env`. GraphQL
requires Account Analytics Read in the variable named by `analytics_token_env`.
Both names may reference the same account token with both read permissions,
injected by the approved secrets render. Wrangler receives neither configured
token variable, standard Cloudflare API token variables, nor Brigade variables.
Credentials never enter argv, snapshots, the Hub store or tool error messages.
Run `brigade fleet stats collect` explicitly from an authorized collector node.
`--no-publish` prints aggregates for inspection. It still reads configured sources.
If all sources fail or none are configured, the command exits 1 without publishing
or printing a snapshot. Partial snapshots retain only successful sources.

The closed `brigade.admin-stats.v2` snapshot and page expose these fields:

- `schema`, `snapshot_id`, `taken_at`, `source_set`.
- `weeks`, `daily`, `downloads`, `digest`.
- `weeks.week`, `weeks.partial`, `weeks.active_sources`, `weeks.retained_2d_sources`.
- `weeks.retained_3d_sources`, `weeks.versions`, `weeks.upgrades`, `weeks.versions.version`.
- `weeks.versions.active_sources`, `weeks.upgrades.from_version`, `weeks.upgrades.to_version`, `weeks.upgrades.active_sources`.
- `daily.day`, `daily.worker_requests`, `daily.worker_errors`, `downloads.pypi`.
- `downloads.npm`.
- `cloudflare`, `cloudflare.collected_at`, `cloudflare.credential`.
- `cloudflare.worker_version`, `cloudflare.worker_published_at`, `cloudflare.repository_head`.
- `cloudflare.d1_rows`, `cloudflare.last_purge`, `cloudflare.requests_24h`, `cloudflare.errors_24h`.

The Hub continues to read retained v1 snapshots during the transition.

The Cloudflare panel uses only fixed Wrangler reads (`deployments status`,
`deployments list`, `versions list`, `d1 info`, and one fixed aggregate SELECT)
and a fixed GraphQL viewer check plus the 24-hour Analytics query. The collector
publishes normalized fields only. API tokens, raw CLI output, and error text do
not enter the snapshot. A missing credential is labeled `credential unavailable`;
failed cells and snapshots older than 15 minutes render as unknown. The panel
follows the newest retained snapshot and is hidden when that snapshot carries no
Cloudflare observation (the panel is off, or it is a v1 snapshot). Wrangler
does not report a D1 purge event timestamp, so `cloudflare.last_purge` remains
unknown until the Worker records that event. The latest retained observation
date is not a purge timestamp.

An operator-triggered mutating path is a separate follow-up, out of scope here.
It would require an admin bearer, an exact argv preview before each action, a
Brigade receipt for every action, and no unattended execution. Deploy, rollback,
secret changes, migration application, and deletion remain denied.

`weeks`, `daily`, version cells and upgrade cells are arrays. Counts are nullable
nonnegative integers. Source counts below 5 become `null`, including zero.
Gap checks compare only containing groups: active sources contain both retention
groups and each version, 2+ day sources contain 3+ day sources, and each version
contains its upgrade cells. A gap of 1 to 4 sources hides the week's source counts
and both histograms. Excess version observations revealing 1 to 4 sources using
multiple versions also hide the week, checked before and after removing small cells.
Other small or unknown histogram cells lose their category names without hiding
the week's totals or valid peers. The page always includes an "other (suppressed)"
row with an unknown count in each histogram, even when no cells were omitted.
It never reports the omitted cells' sum or whether any existed. Incomplete
version or upgrade reads hide both histograms. These rules also apply to uploaded
snapshots. Known retention counts must satisfy active >= 2+ day >= 3+ day sources.
Each known version or upgrade count must not exceed the active-source total.
The POST body ceiling is 4 MiB to cover the full 60-week schema maximum.
The page renders `null` and missing observations as unknown. Worker request,
Worker error and public download counts are event counts and may be zero.
Weekly counts mean active sources, not people or installs. Retention is activity
on 2+ or 3+ distinct days in the same ISO week. Possible upgrades pair versions
observed in time order within a week, restricted to increasing numeric releases.
The current week and the oldest week truncated by the 90-day query are labelled partial.
Source identities rotate weekly. Worker counts cover 30 completed UTC days.
Each malformed version or upgrade cell is omitted individually, preserving valid cells
subject to weekly suppression. Histograms over 100 versions or 200 upgrade
pairs render as unknown. Development versions may use eight-digit date suffixes.
`source_set` contains only `d1`, `worker` and `downloads` for successful reads.
Nonempty weeks require `d1`, nonempty daily data requires `worker`, and known
download counts require `downloads` in `source_set`.
`digest` is SHA-256 of canonical JSON without `digest` or `snapshot_id`, with
sorted keys and compact separators. `snapshot_id` equals that digest.

Controller and erasure contact: Solomon Neas. Contact the maintainer at the
address in [SECURITY.md](../SECURITY.md). No hashes, install IDs, addresses,
hostnames or deployment identifiers enter snapshots. The Hub rejects unknown fields at every depth. It keeps only
aggregate snapshots and weekly observations for 400 days and purges older rows
on each accepted POST. Invalid stored payloads are skipped when rendering.
Opt-in usage aggregates are not available yet. Future opt-in counts will
undercount usage.
Small-cell suppression is defense in depth for screenshots and shared views, not
protection against an enrolled viewer on the operator's own Hub who can compare snapshots.
