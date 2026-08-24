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
- Hub address and token file live in `~/.brigade/fleet.toml`
  (`[fleet] hub_url = ..., token_file = ...`); `BRIGADE_FLEET_HUB_URL` and
  `BRIGADE_FLEET_TOKEN` override. The token is a dedicated fleet secret and is
  never written into a Brigade config value.

## Decisions (closed)

| Question | Decision |
| --- | --- |
| Hub host | hogwarts, dedicated LXC CT (isolated from the working box) |
| Token source | dedicated secret via `BRIGADE_FLEET_TOKEN` env or a `token_file` path; never a config value |
| Git-ref journal backup per machine | no; local journal + hub is enough |
| Dependencies | stdlib only (`http.server`, `sqlite3`, `urllib`) |

## Phases

| Phase | Issue | Status |
| --- | --- | --- |
| 1. Machine identity, namespaced run ids, lease reaper | #1122 | shipped (#1129) |
| 2. Fleet hub (`brigade fleet serve`), event POST, store-and-forward, `brigade fleet status` | #1123 | **built, hub CT pending** — code, tests, and CLI are in; the hogwarts CT that hosts `brigade fleet serve` is not provisioned yet |
| 3. Web dashboard served by the hub | #1124 | shipped |
| 4. Server-arbitrated claims with TTL | #1125 | shipped (#1140); crash self-lockout recovery #1141 |
| 5. Dolt sink for versioned history (optional) | #1127 | not started |
| 6. Cross-repo campaigns on the hub | #1128 | not started |

## Phase 2 surface

Hub (`src/brigade/fleet_hub.py`):

- `GET /health` — no auth, liveness.
- `POST /events` — bearer auth; one event object or an array (max 1000);
  a batch with any invalid event is rejected whole; responds
  `{"accepted": n, "duplicate": m}`.
- `GET /status` — bearer auth; latest event per `(node_id, run_id)` for runs
  whose latest state is not terminal (`run.completed`, `run.failed`,
  `run.interrupted`); `?all=1` includes terminal runs.
- SQLite in WAL mode, `PRAGMA user_version` = schema version; a database from a
  newer hub is refused rather than reinterpreted.
- `brigade fleet serve --host <ip> [--port 3774] [--db PATH] [--token-file PATH]`;
  `--host` is required so the hub never binds all interfaces by accident.

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
  `brigade.fleet` logger). A non-retryable 4xx (anything but 401/408/429)
  drops that batch as poison instead of retrying forever; 401, 5xx, and
  network failures keep the spool.
- When a spool exists, new events are appended to it and the spool is flushed
  in order (100 per request) so the hub never sees a node's events out of
  sequence. `brigade fleet flush` drains it explicitly.
- `brigade fleet status [--all] [--json]`.

## Phase 4 claims

The claim key is the bare workspace directory name, matching the `repo` key
on fleet events. Unrelated repositories with the same directory basename
therefore arbitrate against each other. Separate worktrees of one repository
have different directory names and do not arbitrate against each other when
each resolves its own workspace identity. If they instead resolve the
per-user `~/.brigade/node.toml` fallback, every such worktree under the home
directory uses the home directory name and arbitrates on that shared key.

Claims are best-effort protection layered over the local run lock. If the hub
rejects the configured token with HTTP 401, the client logs one WARNING and
continues under the local lock alone, just as it does when the hub is
unreachable.

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
  the run at all. The release then carries the inspected row's
  `acquired_at`, and the hub deletes only that exact row (one write
  transaction), so a claim re-acquired in between is refused with the
  current row. `--node` other than this machine's identity needs
  `--force`; `--force` (`scope: "force"`) skips the proof and the JSON
  receipt's `forced` reflects the flag. Renew is never token-less.
- `brigade run --no-fleet-claim` skips the hub claim entirely and relies on
  the local run lock alone, logged once on the `brigade.fleet` logger.
- Trust boundary: `node_id` and the leases are caller-asserted under the one
  shared bearer token, so `scope` is an intent marker that keeps honest
  clients from stealing each other's claims, not an authorization — the
  bearer token already authorizes `force` and arbitrary claims.
