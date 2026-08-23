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
| 3. Web dashboard served by the hub | #1124 | not started |
| 4. Server-arbitrated claims with TTL | #1125 | not started |
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
have different directory names and do not arbitrate against each other.

Claims are best-effort protection layered over the local run lock. If the hub
rejects the configured token with HTTP 401, the client logs one WARNING and
continues under the local lock alone, just as it does when the hub is
unreachable.

Run the hub under a process supervisor on the CT, e.g.
`brigade fleet serve --host "$(tailscale ip -4)" --token-file /etc/brigade/fleet-token`.
