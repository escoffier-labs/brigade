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
| 4. Server-arbitrated claims with TTL | #1125 | not started |
| 5. Export and Dolt sink for versioned history (optional) | #1127 | built |
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
replaced atomically after a successful commit. A missing or invalid watermark
causes a full event export. Set `events_incremental = false` to export the full
event log on every pass.

`brigade fleet sink [--db PATH]` exports deterministic CSV files. It runs
`dolt table import -u` for `fleet_events`, which is an append log, and `dolt
table import -r` for `fleet_claims`, which is a live snapshot. The claims
snapshot excludes expired rows. Replacing it removes released or expired claims
from Dolt on the next pass. The sink CSV keeps source values unchanged. After
both imports, the sink checks `dolt status --porcelain`. A changed working set
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
