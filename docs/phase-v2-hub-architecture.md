# Brigade v2: Rust Fleet Hub, hub memory, reflect and push connectivity

Design for the 0.31.0 release, "Brigade v2". Besides the rebrand, the release
ships four pieces that depend on each other:

1. A Rust Fleet Hub (axum, tokio, rusqlite) that replaces the Python hub with
   no client changes.
2. Shared memory hosted on the hub, with no single owner, that every agent can
   use without setting up an MCP server or a tunnel.
3. `brigade memory reflect` (#1856) consolidation and policy-gated promotion,
   running against hub memory.
4. Memory and messages pushed to cloud agents through calls they already make,
   instead of waiting for them to pull.

This page records the decisions, the design, and a build plan of PR-sized
slices. It changes no code. Related work: the frozen wire contract and its
drive (#2204, [phase-hub-contract.md](phase-hub-contract.md)), contract v2
(#2207), BM25 search and prompt recall (#2201, #2196), the Tier 0 preflight
(#2187), native engine distribution (#2148), and sealed messages (#2005,
[phase-agent-mesh.md](phase-agent-mesh.md)).

Planned commands and flags appear without the `brigade` prefix, for example
"the planned `fleet serve --engine rust`". Commands written with the prefix
exist today.

## Contents

- [Goals and non-goals](#goals-and-non-goals)
- [Decisions](#decisions)
- [System overview](#system-overview)
- [Rust Fleet Hub](#rust-fleet-hub)
- [Memory v2](#memory-v2)
- [Connectivity](#connectivity)
- [Wire contract](#wire-contract)
- [Build plan](#build-plan)
- [Release gate for 0.31.0](#release-gate-for-0310)
- [Risks](#risks)
- [Open questions](#open-questions)
- [Rollback plan](#rollback-plan)
- [Evidence](#evidence)

## Goals and non-goals

Goals:

1. The Rust hub passes the v1 contract drive exactly, recorded quirks
   included, on the same database file. Operators change one serve flag and
   clients change nothing.
2. Write throughput at least 10 times today's: 4,600 or more single-event
   `POST /events` requests per second with one connection per request, up
   from about 460.
3. Long-poll capacity in the thousands, up from 8 waiters across the whole
   fleet.
4. Shared memory on the hub that any enrolled principal can read and write,
   curated by a policy that names who may promote what.
5. Ranked retrieval that beats the current BM25 on
   `python -m brigade.memory_retrieval_eval` before any default changes.
6. Reflect and promotion on hub memory, with the #1856 tiers kept as tested
   invariants.
7. Cloud agents receive relevant memory without calling a memory tool.
8. One real-world event produces one update across all agents.
9. Every step can roll back to the Python hub on the same database.

Non-goals:

- Changing any v1 wire behavior during the cutover. That is contract v2
  (#2207), after the cutover.
- A Postgres backend. The storage seam exists in v2, and no second backend
  ships.
- Porting the HTML Command Deck to Rust. It stays on the Python renderer.
- Memory on Hub Lite. Hub Lite answers 404 on memory routes, and clients fall
  back to local search.
- Encrypted memory cards. Sealed messaging stays as #2005 designed it. Cards
  are stored in the clear on the hub, inside the operator's trust boundary.
- Approximate nearest-neighbor indexes. A brute-force scan measured 3 to 7 ms
  at 2,000 to 20,000 cards, which is inside the query budget.
- Any LLM call inside a hub request.
- Deleting or archiving cards automatically. Weaken and contradict never
  delete (#1856).
- A second task database. Duties extend the existing owners charters.
- New runtime dependencies for the Python package.

## Decisions

Status `Decided` means the maintainer already made the call. `Proposed` means
this document proposes it, and review of this document is the decision point.

| ID | Decision | Basis | Status |
|---|---|---|---|
| D1 | The Fleet Hub moves to Rust on axum, tokio and rusqlite. | Same handler logic in three servers: Rust 15k to 18k req/s, async Python about 5.3k, threaded Python about 2.3k (WAL, `synchronous=NORMAL`). Today's hub peaks near 460 writes/s because of its design: 32 handler threads, 8 per source, `request_queue_size=5`, and a new connection plus a schema read on every request. | Decided |
| D2 | One writer thread with group commit, a reader pool, statements prepared once per connection, explicit admission caps and long-poll limits, WAL with `synchronous=NORMAL`. | Profiling put about 0.6 ms of the 0.96 ms per-request cost in opening a connection and reading the 75-table schema. A reused connection answers the same query in about 0.001 ms. | Decided |
| D3 | Inside the group commit, a batch that contains a claim, fence, node or principal mutation commits with `synchronous=FULL`. Everything else commits with `NORMAL`. | `NORMAL` can drop the last commits on power loss or an OS crash. A lost claim grant or a repeated fencing generation would let two holders run. Group commit spreads one sync over the whole batch, so the extra cost is one fsync per batch that contains such a write. | Proposed (Q2) |
| D4 | Storage sits behind one interface so Postgres can be added later. SQLite is the default and the only backend in v2. | Keeps v2 small while leaving room for multi-node installs. | Decided |
| D5 | The Rust hub fronts all traffic from its first deploy. Each route family is `native`, `shadow` (proxied with a Rust comparison) or `proxy` (forwarded to the Python hub on loopback). | 54 JSON routes and 37 HTML routes, backed by more than 20,000 lines of Python hub code. Porting the hot set first and proxying the rest lets the cutover start long before a full port. | Proposed (Q1) |
| D6 | During the rollback window the Python hub owns the hub database schema. Rust checks `user_version` and the structure it needs and never runs DDL on that file. | Rollback to Python needs a database Python accepts. Python refuses any `user_version` above its own. | Proposed |
| D7 | Memory v2 lives in its own SQLite file, owned by the Rust hub. | The Python hub never opens it, so rollback stays clean, and memory gets its own writer and size profile. | Proposed |
| D8 | Memory v2 is an event log on the hub. Cards are rows keyed by `card_id` plus `content_hash`. The index is flat postings plus one contiguous embedding matrix per embedder version. Retrieval is BM25 plus dense, fused with reciprocal rank fusion (k=60), with an optional LLM rerank for top-1 answers. Any principal with a grant writes events. Curation is a policy. Derived rows carry fingerprints and lineage keyed by `card_id`, `content_hash` and embedder version. | Judged real prompts: BM25 hit@5 0.66 to 0.74, hybrid with a 0.6B embedder 0.83 to 0.94, an LLM rerank of the top 20 lifted first-result correctness from about 0.64 to about 0.91. | Decided |
| D9 | The LLM rerank runs outside the hub, in the caller's own read-only seat. | Keeps model calls and provider credentials out of the hub process and the request path. Matches #1856: no provider integration enters core. | Proposed |
| D10 | Two embedder backends behind one interface: a 0.6B-class model on the hub's CPU, and a separate GPU host that computes and ships vectors. Measure both, then pick the default. | The 0.6B hybrid nearly matched an 8B model on real prompts. CPU latency on hub-class hardware is unmeasured. | Decided |
| D11 | Both backends talk HTTP to an embedding server: on loopback for the hub-CPU backend, over the private network for the GPU backend. The hub binary links no ML runtime. | Keeps model runtimes out of the five-platform native release matrix. Loopback HTTP adds about a millisecond to a query embedding that measured 61 ms. | Proposed |
| D12 | Memory is pushed to agents. Hub-built packets and the tool responses agents already call (`fleet_digest`, `inbox_list`) carry the top memory hits. Session-start and stop hooks inject and capture. Cloud connectors need no new tools. | Connector test: a hosted assistant used Brigade memory on 4 of 10 prompts and gave a card-backed answer on 3 of 10. Two other connectors used it on 0 of 2 each. | Decided |
| D13 | Grok Bot gets a read-mostly memory profile (`memory_search`, `memory_card`, `handoff_write`) and a separate inbox profile with sender allowlists. Grok Bot does X work only. | Small quota, and a cloud agent should not hold write or dispatch authority it does not need. | Decided |
| D14 | One owner per duty: the hub's owners charters (`brigade.fleet_owners.v1`) plus a findings ledger keyed by fingerprints (email Message-ID, canonical job URL), so one real event produces one update. | Duplicate reports from several agents watching the same inboxes and feeds. The roadmap forbids a second task database. | Decided |
| D15 | The #1856 promotion tiers stay as tested invariants on hub memory: Tier 0 may auto-promote under policy, Tier 1 is review-only until a calibration record exists, Tier 2 is always review. A failed refresh never replaces a healthy card. | #1856 acceptance criteria. | Decided |
| D16 | Guard scanning moves to a native PCRE2 engine with JIT, built in parallel. The hub links the same engine to scan memory writes. | About 24 MB/s against 4.3 MB/s for Python `re` on the same 20 default rules, with identical match counts. | Decided (engine), Proposed (hub use) |
| D17 | Native engines are renamed `brigade-code-graph` and `brigade-evidence`, with the old names kept as shims for at most two releases. The hub binary is `brigade-hub` and has no legacy alias. The `brigade` command and Python package keep their names. | Naming convention set for the engine rename. | Decided |
| D18 | No runtime dependency is added to the Python core. The Rust hub ships through the native-engine release path (#2148). | Zero-dependency core. | Decided |
| D19 | Cutover runs the Rust hub beside the Python hub, mirrors read traffic, then flips writes. Rollback is the existing per-SHA virtualenv pin. | Lowest-risk path that keeps one database. | Decided |
| D20 | Contract v1 stays frozen for the cutover. Contract v2 (#2207) fixes the recorded quirks after it. | Decided 2026-11-03. | Decided |
| D21 | New routes (`/memory`, `/findings`, `/metrics`) are additive under v1 and each lands with contract drive cases in the same PR. Until v2 adds `GET /version`, a client discovers memory support by probing: a v1 `404 {"error": "not found"}` means no hub memory. | The v1 contract defines added routes as non-breaking. The signed hub identity has a closed capability list that the Python hub validates, so it cannot advertise memory under v1. | Proposed |

## System overview

```text
  local harness hooks     CLI (fleet, memory, run)     Dot and Duet connectors
  T3 dispatch adapter     Grok Bot relay               embed worker (GPU host)
            \                      |                          /
             \                     |  HTTP/1.1, Bearer token  /
              v                    v                         v
  +----------------------------- brigade-hub (Rust) ------------------------------+
  | accept + admission: connection caps, per-source caps, header and body          |
  | timeouts, body and JSON bounds                                                |
  |   |                                                                           |
  | conventions layer: query-token refusal, 401 envelopes, 404, JSON headers      |
  |   |                                                                           |
  | route table: family -> native | shadow | proxy                                |
  |   |                  |                       |                      |         |
  | native handlers   shadow comparator     upstream proxy        memory handlers |
  |   |        |                                 |                      |         |
  | reader   writer thread                       |              memory writer     |
  | pool     (group commit,                      |              + index snapshot  |
  | (N conns, savepoint per command,             |              + embed scheduler |
  | query    post-commit wakeups)                |                      |         |
  | only)                                        |                      |         |
  +---|--------|---------------------------------|----------------------|---------+
      v        v                                 v                      v
  +-----------------------+      +--------------------------+  +------------------------+
  | fleet-hub.db (WAL)    |<---->| Python hub, loopback     |  | fleet-hub-memory.db    |
  | schema owned by       |      | upstream: cold routes,   |  | (WAL), owned by Rust   |
  | Python (D6)           |      | Command Deck, webhook    |  +------------------------+
  +-----------------------+      | delivery, retention      |
                                 +--------------------------+
                                             ^
                                 embedding server: loopback (hub CPU)
                                 or private network (GPU host)
```

Two processes write `fleet-hub.db` during the rollback window: the Rust writer
for native families and the Python upstream for proxied ones. SQLite
serializes them. The proxied families are the low-rate ones, so contention is
small, and the plan measures it (S6).

## Rust Fleet Hub

### Route ownership

Every request enters the Rust process. The route table maps each method, path
and, for `POST /messages`, the body's `action` to a family and a mode.

| Family | Routes | First mode | Native in slice |
|---|---|---|---|
| conventions | auth envelopes, unknown-path 404, query-token refusal, JSON headers, body framing and bounds | native | S3 |
| health | `GET /health` | native | S3 |
| registration | `POST /nodes`, `GET /nodes`, `POST /message-principals` | proxy, then shadow for `GET /nodes` | S5 |
| heartbeat | `POST /events`, `GET /status`, `POST /sessions`, `GET /sessions` | proxy, then shadow | S6 |
| claims | `POST /claims`, `GET /claims`, `GET /owners` | proxy, then shadow | S7 |
| messages | `POST /messages`, every action except `events_*` (the `question_*` actions included, because `question_create` inserts a message) | proxy, then shadow | S8 |
| metrics | `GET /metrics` (new) | native | S9 |
| memory | `POST /memory` (new) | native only | S13 |
| findings | `POST /findings` (new) | Python first, proxied | S22, then S12 |
| cold JSON | `/preference`, `/audit`, `/mesh/*`, `/cloud`, `/models`, `/policy*`, `/grokbot`, `/skills/report`, `/mcp/report`, `/projects/board`, `/stats`, `/ci*`, `/actions`, `/work/*`, and the `events_*` message actions | proxy | S12, one family per PR |
| browser | `/`, `/deck*`, `/view/*`, `/enroll`, `/dashboard/*`, static assets | proxy | not planned |

Rules:

- `shadow` applies only to requests the Python hub serves on a `query_only`
  connection. A test runs every shadowed action against a Python hub whose
  database file is read-only and fails if any of them writes.
- In `shadow` mode the Python answer is returned to the client. Rust computes
  its own answer from the same database and compares the two with the
  contract drive's normalization rules (timestamps, generated ids, tokens and
  key order ignored). A mismatch is retried once on both sides, because a
  write can land between the two reads. Only a repeated mismatch is recorded.
- The normalization rules live in one data file that the drive and the Rust
  comparator both read, so the two cannot drift.
- A mismatch record holds the route, action, both status codes, the JSON path
  of the first difference and SHA-256 digests of both bodies. It never holds
  a body.
- Long-poll list requests (`wait_ms` above 0) are never shadowed.

### Process model

One process, one tokio multi-thread runtime for HTTP, and dedicated OS threads
for SQLite work, because rusqlite calls block.

| Component | Threads | Owns |
|---|---|---|
| HTTP | tokio workers (default: CPU count) | sockets, parsing, auth lookups through the reader pool, JSON encoding |
| Hub writer | 1 OS thread | the only Rust write connection to `fleet-hub.db` |
| Reader pool | N OS threads (default: CPU count, at most 8) | one read connection each, `PRAGMA query_only=ON` |
| Memory writer | 1 OS thread | the write connection to `fleet-hub-memory.db` |
| Memory readers | shares the reader pool size, separate connections | read connections to the memory file |
| Index | none of its own. Snapshots are published by the memory writer | the in-memory lexical and dense index |
| Embed scheduler | tokio task | embedding jobs and calls to the embedding server |
| Upstream proxy | tokio tasks | pooled HTTP client to the Python upstream |
| Maintenance | tokio timer plus writer commands | WAL checkpoints and, after S12, retention sweeps |

Connections live for the life of the process. Each connection prepares its
statements once and keeps them in its statement cache, so no request pays for
opening a database or parsing the schema.

### Write path

Handlers never touch the write connection. A write becomes a command sent to
the writer over a bounded channel, with a one-shot reply channel.

```text
loop:
    first = receive()                      # wait for one command
    batch = [first] + drain_without_waiting(limit = 255)
    synchronous = FULL if any(command.durable for command in batch) else NORMAL
    BEGIN IMMEDIATE
    for command in batch:
        SAVEPOINT command
        result = command.apply(transaction)  # reads and writes, no I/O outside SQLite
        RELEASE or ROLLBACK TO command
    COMMIT                                 # on failure every command gets 503, reconnect
    reply to every command with its result
    post_commit(batch)                     # wake long-poll waiters, publish metrics
```

- There is no linger timer. A batch is whatever queued while the previous
  commit ran, so a single client sees no added latency and a busy hub gets
  larger batches.
- A failing command rolls back to its own savepoint and returns its error.
  The rest of the batch commits.
- A command does all of its reads inside the write transaction. Handlers
  that read and then decide (claim acquire, idempotent message send, fencing
  checks) are one command each, so they see a consistent state.
- Write commands re-check the caller's credential row inside the
  transaction, so a token revoked between authentication and the write
  cannot write.
- Durable commands (D3): claim acquire, renew and release, fence updates,
  node add and revoke, principal add, grant and revoke.
- Side-table writes happen in the same command as the main row: the audit
  log, the message send ledger, and the MCP Events outbox rows that the
  Python hub writes when callback hosts are configured. The Python upstream
  keeps delivering webhooks from that outbox until the events family is
  ported.
- The writer disables automatic checkpoints. When idle for a second, or when
  the WAL passes 64 MiB, it runs a passive checkpoint. Above 256 MiB it runs
  a truncating checkpoint.
- The writer's `busy_timeout` is 5,000 ms, matching the Python hub, because
  the Python upstream also writes.
- Graceful shutdown stops accepting, wakes every long-poll waiter with an
  empty page, drains the writer queue, and checkpoints.

### Read path

A read handler waits up to 2 s for a reader, runs its queries inside one
`BEGIN DEFERRED` transaction so a multi-query view (the status board, the
owners view) is one snapshot, and returns rows to the tokio side for JSON
encoding. A waiting reader that times out answers
`503 {"error": "hub busy", "reason": "reader-queue-full"}`, the same
`{error, reason}` shape the Python hub uses for its existing 503 answers.

Python reads pick up enrollments and revocations by opening a new connection
per request. The Rust readers see them because every request starts a new
read transaction, which sees all committed data. There is no token cache and
no result cache anywhere in the hub.

### Admission and backpressure

| Limit | Rust default | Python today |
|---|---|---|
| Open connections | 4,096 | 32 handler threads |
| Connections per source address | 256 | 8 |
| Listen backlog | 1,024 | 5 |
| In-flight requests per credential | 64 | none |
| Writer queue | 4,096 commands | none (5 s busy timeout per request) |
| Reader wait | 2 s | none |
| Long-poll waiters, total | 10,000 | 8 |
| Long-poll waiters per principal | 2 | 2 |
| Long-poll waiters per non-loopback source | 64 | 2 |
| `wait_ms` maximum | 20,000 | 20,000 |
| Body size | 8 MiB plus the JSON bounds | same |
| Header read timeout | 10 s | 30 s socket timeout |
| Body read timeout | 30 s | 30 s socket timeout |

- Over the connection or per-source cap, the connection is closed without an
  HTTP response. That is the v1 behavior, and clients already treat it as an
  unreachable hub (events go to the local spool).
- A full writer queue, a reader timeout, or a credential over its in-flight
  cap answers `503 {"error": "hub busy", "reason": ...}`. Clients already
  treat a 5xx as retryable: event posts keep the batch in the spool, and the
  claim client surfaces a 503 as an answer.
- Source caps use the TCP peer address, never a header, as Python does.
- The per-principal waiter cap of 2 stays, because clients can see it: the
  third waiter gets the v1 error with `code: "message_wait_capped"`.
- The JSON bounds match `fleet_hub_event_bounds`: at most 32,768 containers,
  131,072 aggregate items and 1,000 items per container. A body with
  `Transfer-Encoding`, or without exactly one `Content-Length`, is refused as
  today.

### Long-poll

`POST /messages` `list` with `wait_ms` waits on a per-recipient notifier. The
writer's post-commit step wakes the notifier for every recipient touched by
the batch. A woken waiter re-reads through the reader pool and returns when it
has rows or its deadline passes, the same loop as the Python hub without a
thread per waiter. The idle cost measured 15 to 18 KB per connection on the
same runtime, where the threaded Python server failed past about 8,400
threads.

Only the messages module inserts into `fleet_messages`, and the question
actions reach it through `question_create`. S8 therefore ports the question
actions with the rest of the messages family, so no proxied route can add a
message that a Rust waiter would miss. As a safety net, while any waiter
exists Rust reads `PRAGMA data_version` every 250 ms, and when another process
has committed it re-reads the waiting inboxes.

### Storage interface

```text
trait HubStore {
    read(view)        -> result   // reader pool, one read transaction
    write(command)    -> result   // writer, one savepoint inside a batch
}
```

Each domain module (registration, heartbeat, claims, messages, memory,
findings) defines its commands and views against two small transaction
traits, `ReadTx` and `WriteTx`. SQL text lives per backend under
`engines/hub/sql/sqlite/`. The correctness rule for every command is that it
must be correct under serializable isolation and must not rely on running
alone. SQLite gets that from the single writer. A future Postgres backend
would run serializable transactions with retry on serialization failures,
one transaction per command, and would skip group commit. The memory index is
not part of the store. The store persists the log, versions and vectors, and
the index is rebuilt from them.

### Schema, migrations and data compatibility

Hub database (`fleet-hub.db`):

- The Python hub at this design's base is at `user_version` 47 with 75 tables,
  146 indexes and 15 triggers.
- During the rollback window Python is the schema authority (D6). Startup of
  the planned `fleet serve --engine rust` runs the existing Python `init_db`
  before starting the Rust binary, as `fleet serve` does today.
- The Rust binary is built for one `user_version`. At startup it refuses to
  serve unless the file's `user_version` equals that number and every table,
  column and index Rust uses exists with a compatible type. The check is
  structural (`PRAGMA table_xinfo`, `index_list`, `index_xinfo`), not a hash
  of the schema SQL, because a database migrated with `ALTER TABLE` stores
  different SQL text than a fresh one.
- `engines/hub/schema/required.json` lists that structure. A Python test
  builds a fresh hub database and fails when it no longer matches, so any
  Python schema bump forces a reviewed Rust update in the same PR.
- Tables a new hub feature needs on this file (the findings ledger) are added
  through the Python schema code, so Python can read them after a rollback.
- After the rollback window, schema authority moves to Rust with forward-only
  numbered migrations from 48. From then on a rollback to Python needs a
  restored backup, and the runbook says so.

Memory database (`fleet-hub-memory.db`, next to the hub file):

- Owned by Rust from the start (D7), with its own `user_version` series and
  forward-only migrations embedded in the binary.
- The same file rules as the hub database: a 0700 directory and 0600 files,
  regular files only, no symlinks, as `_secure_hub_database_storage` enforces.
- The Python hub never opens it. A rollback leaves it untouched, and the
  memory routes answer 404 until Rust returns.

Rows written by one hub must read correctly in the other. The drive checks the
wire, and a new storage parity drive (S2) checks the rows: it runs one
scripted request sequence against a hub on a fresh database, dumps every table
with values replaced by their format class (for example "isoformat with
microseconds"), and compares the dump with a golden taken from the Python hub.

| Item | Python behavior | Rust rule |
|---|---|---|
| Timestamps in text columns | `datetime.isoformat()` on aware UTC: `2026-11-03T12:34:56.123456+00:00`, with the fraction omitted when microseconds are 0. Some columns hold epoch floats. | Same bytes per column. SQL compares these as strings, so a different format reorders rows. |
| JSON in text columns | `json.dumps` with each call site's `sort_keys` and separators, and `ensure_ascii=True` | A Python-compatible encoder: same separators, `\uXXXX` escapes for non-ASCII, Python float formatting (`1e-05`, not `1e-5`) |
| Canonical JSON for digests and sealed envelopes | `sort_keys`, separators `(",", ":")`, `ensure_ascii`, `allow_nan=False` | Same, checked against golden vectors produced by Python |
| Token digests | SHA-256 hex of the token | Same |
| Request JSON | `json.loads` accepts `NaN` and `Infinity` literals. Duplicate keys are rejected on some routes and keep the last value on others. Integers are unbounded, then range-checked. | Match per route. A differential fuzz run (S8) sends the same generated bodies to both hubs and requires the same status and error text. |
| Byte-budgeted responses | `GET /owners` fills its payload against a 480 KiB budget measured with Python's encoder | Budget measured with the same encoder settings the response uses |
| Error text | Strings pinned by the drive, and others clients may match | One error table, generated from the Python constants and checked by a test |

### Authentication and tokens

- Admin token: from `--token-file` or `BRIGADE_FLEET_TOKEN`, held in memory
  only, compared in constant time.
- Node tokens: 32 random bytes, URL-safe. The hub stores only the SHA-256 hex
  digest and looks it up per request with a prepared statement. A revoked
  token gets the exact v1 text, `unauthorized: node token revoked`.
- Message-principal tokens: the same digest scheme. Valid on `/messages` as
  in v1, and on `/memory` when the principal holds a memory grant.
- Every v1 quirk stays: the `/policy` envelope, the 403 on `/audit`, the
  events wording on the admin refusal, the plain-text refusal of a `token`
  query parameter before routing, and the 401 for a principal token on node
  routes.
- Dashboard sessions, enrollment codes and cookies stay with the proxied
  browser routes. Rust sets no cookies.
- `Tailscale-User-Login` is removed from every incoming request unless the
  Rust hub runs with `--trust-tailscale-identity` and a loopback `--host`,
  the same rule the Python hub applies. When trusted, the header is forwarded
  to the upstream for browser routes only. `--trust-forwarded-proto` follows
  the same rule.
- The upstream runs in a planned upstream mode: loopback bind, no per-source
  cap (Rust enforces it on real peer addresses), and the client address taken
  from a header that only the Rust front can set, keyed by a random value
  generated at each start. Audit rows and waiter caps then keep real
  addresses.

### Sealed messages

The hub does no cryptography (#2005). For sealed mail it validates structure
and stores opaque bytes. Rust must reproduce the Python checks exactly: the
canonical form, the routing-header field set, duplicate-key rejection inside
envelopes, the digest fields (`ct_sha256`, `wraps_sha256`, `rc`), padding,
`--require-sealed` and `BRIGADE_FLEET_MESSAGES_REQUIRE_SEALED`, the storage
policy and retention, the key directory (`keys_publish`, `keys_get`) and
manifests. Envelopes are stored and returned byte for byte. The drive has no
sealed cases yet, so S1 adds them against the Python hub before S8 ports the
code. The `/mesh/identity` and `/mesh/statements` routes stay proxied until
the mesh family is ported.

### Observability

- `GET /metrics` (new, admin token only) serves the Prometheus text format.
  Labels are route family, action and status class, never a principal name,
  claim target or card id.
- Counters and histograms:
  - requests: latency by family
  - writer: batch size, commit time and queue depth
  - readers: wait time
  - long-poll: active waiters
  - admission: refusals by reason
  - upstream: latency and errors
  - shadow: comparisons and mismatches by family
  - memory: documents, terms, vectors per embedder, embedding queue depth,
    embedding failures by code, and query time split into lexical,
    embedding, scan and fusion
- One structured log line per request: request id, family, status, latency,
  credential kind (admin, node or principal) and byte counts. Logs never hold
  bodies, tokens, message subjects, card text or query text.
- `GET /health` keeps its v1 shape.

### Configuration

The Rust binary takes the same flags and environment as `brigade fleet serve`,
so units and container recipes keep their arguments:

| Flag or variable | Meaning |
|---|---|
| `--host` (required), `--port` (default 3774) | bind address. Never all interfaces by default |
| `--db` (default `~/.brigade/fleet-hub.db`) | hub database |
| `--token-file`, `BRIGADE_FLEET_TOKEN` | admin token |
| `--allow-admin-writes`, `--require-sealed`, `--deck-config`, `--trust-tailscale-identity`, `--trust-forwarded-proto` | as today |
| `BRIGADE_FLEET_MESSAGES_RETENTION_DAYS`, `BRIGADE_FLEET_MESSAGES_REQUIRE_SEALED`, `BRIGADE_FLEET_EVENTS_CALLBACK_HOSTS`, `BRIGADE_FLEET_EVENTS_ALLOW_LOOPBACK_HTTP`, `BRIGADE_FLEET_OPERATOR_RECIPIENT` | as today. Rust reads them where native handlers need them |

New, Rust only: `--upstream` (the Python loopback URL), `--route-mode
family=native|shadow|proxy` (repeatable), `--memory-db`, `--reader-threads`,
`--max-connections`, `--max-waiters`, `--writer-queue`, and `--config` for an
optional `hub.toml` holding the memory and embedder settings. Precedence is
flag, then environment, then file, then default.

### Packaging and distribution

- Source: `engines/hub/`, package and binary `brigade-hub`, the same Rust
  edition and minimum version as the code-graph engine. Dependencies: tokio,
  axum, hyper, rusqlite with bundled SQLite, serde, serde_json, sha2, hmac,
  subtle, tracing and clap. No OpenSSL: the hub serves plain HTTP behind the
  operator's proxy, as the Python hub does. Advisory and license checks run
  in CI on the lockfile.
- Build: the existing Rust native release job builds it for the five
  supported platforms, with checksums and attestations, and the component
  manifest gains a `brigade-hub` entry. The manifest validators learn the new
  complete set.
- Install: hub hosts opt in with the planned `setup --component brigade-hub`.
  Other hosts never download it.
- Run: the planned `fleet serve --engine rust` keeps every existing flag. The
  Python process runs `init_db`, binds the upstream on a loopback ephemeral
  port, verifies the installed binary against the manifest checksum, starts
  it with `--upstream`, forwards signals, and exits when the child exits.
  Rollback is removing `--engine rust`.
- Container: `ops/fleet-hub/Containerfile` adds the verified binary and uses
  `fleet serve --engine rust`. Python stays in the image for the upstream.
- PCRE2: the hub links the same vendored, statically built PCRE2 engine crate
  as the guard. The release job already needs a C compiler on every target
  for bundled SQLite.

### Cutover

| Phase | What changes | Gate to move on |
|---|---|---|
| 0 | Python hub on the public port (today) | S1 to S10 merged. Contract drive and storage parity pass against Rust in CI |
| 1. Front | Rust takes the public port, Python moves to loopback, every family in `proxy` mode | 48 hours with error rates and latency no worse than phase 0 |
| 2. Mirror reads | Heartbeat, claims and messages reads in `shadow` | 7 days, at least 1,000 shadowed requests in each family, and zero unexplained mismatches |
| 3. Flip writes | Heartbeat to `native`, then claims, then messages, one family at a time | Each family runs 48 hours with no new errors, writer queue depth under 10% of its cap, and the drive green against production in read-only mode |
| 4. Memory | Memory routes enabled. Clients turned on per workspace | Retrieval gate R0 passes on the production corpus snapshot |
| 5. Cold families | Ported one family per release after 0.31.0 (S12) | Same as phase 3 for each family |

The drive refuses non-loopback URLs, so production checks run it from the hub
host itself against the loopback port, read-only cases only.

## Memory v2

### Model

Memory is an append-only event log on the hub. A card version is the pair
`(card_id, content_hash)`. The current version of each card is derived from
the log and updated in the same transaction as the event that changes it. The
index and the vectors are derived after the commit and can be rebuilt from
the log at any time. Any principal with a write grant on a scope can
append events. Nothing hosts the cards on behalf of others. Curation is a
policy in the fleet policy document, which says who may promote what, at which
tier, under which gates.

Concurrent writers use compare-and-set. A `put` names the `base_hash` it
edited. If the current version has moved on, the hub refuses with `409` and
`code: "memory_base_mismatch"`, plus the current hash, and the writer
re-reads. An offline client whose edit conflicts turns it into a proposal for
review and never overwrites.

### Event schema

`brigade.memory_event.v1`:

| Field | Type | Notes |
|---|---|---|
| `seq` | integer | hub-assigned log offset, strictly increasing |
| `event_id` | text | hub-assigned |
| `client_event_id` | text, optional | idempotency per principal: the same id with the same payload digest returns the original result, and a different payload returns 409 (the message `client_id` rule) |
| `kind` | enum | `card.put`, `card.retire`, `evidence.attach`, `handoff.submit`, `proposal.open`, `proposal.decide`, `proposal.rollback`, `recall.capture` |
| `principal` | text | taken from the credential, never from the body |
| `card_id` | text | `[a-z0-9][a-z0-9._-]{0,127}`, from the card's front matter `id` or its file stem |
| `content_hash` | text | SHA-256 hex of the normalized card bytes |
| `base_hash` | text or null | compare-and-set precondition. Null creates a card |
| `scope` | text | `fleet`, `repo:<repo_identity>`, or `principal:<name>` (visible to the writer only) |
| `trust` | enum | `reviewed`, `policy-promoted`, `imported`, `agent-unreviewed`, `flagged` |
| `payload` | object | for `card.put`: card bytes (at most 1 MiB, the current card cap). For proposals: ops, evidence references and base hashes |
| `policy_digest` | text or null | the curation policy revision a decision was made under |
| `created_at` | text | hub clock, Python isoformat |

Normalization for `content_hash`: UTF-8, LF line endings, exactly one
trailing newline. Front matter is kept as written. A change to this recipe
bumps `normalizer_version`, which is part of every derived key.

### Routes

One route, `POST /memory`, with an `action` field, the same style as
`/messages` and `/claims`. Authentication failures keep the v1 conventions
(`401 {"error": "unauthorized"}`). Every other 4xx carries
`{error, code}`. A missing grant is `403` with `code: "memory_grant_missing"`.

| Action | Grant | Request | Success |
|---|---|---|---|
| `search` | read | `query` (at most 256 characters after normalization), `limit` (1 to 20, default 10), `mode` (`auto`, `lexical`, `hybrid`), `query_source` (`prompt`, `cwd`, `explicit`), optional `scopes` | `200 {matches, retrieval: {mode, degraded}, seq}` |
| `card` | read | `card_id`, optional `content_hash` and `offset` | `200 {card: {card_id, content_hash, text, next_offset, trust, scope, seq}}`, text in 30,000-character chunks as Dot's `memory_card` returns it |
| `put` | write on the scope | `card_id`, `base_hash`, `text`, `scope`, `client_event_id` | `201 {event: {seq, card_id, content_hash}}` |
| `retire` | write on the scope | `card_id`, `base_hash`, `reason`, `client_event_id` | `200 {event}` |
| `log` | read | `after_seq`, `limit` (1 to 500) | `200 {events, next_seq}`, ascending `seq`, `next_seq` equal to the last `seq` on the page, `after_seq` exclusive, card text omitted |
| `handoff` | handoff | `text`, `kind`, `client_event_id` | `201 {handoff_id}` |
| `propose`, `decide`, `rollback` | reflect, review | proposal v2 body | `201` or `200` with the proposal state |
| `capture` | read | `session_ref` (a client-side HMAC), `pushed`, `opened`, optional `outcome` | `200 {recorded: true}` |
| `embed_lease`, `embed_submit` | embed | `embedder_id` and `max`, or `lease_id` and base64 float32 little-endian `vectors` | `200` |
| `status` | read | none | `200 {cards, versions, seq, embedders: [{id, coverage}], default_mode}` |

Error codes: `memory_query_invalid`, `memory_card_not_found` (404),
`memory_base_mismatch` (409, with `current_hash`),
`memory_client_event_conflict` (409), `memory_card_too_large` (413),
`memory_content_blocked` (422), `memory_proposal_invalid` (422),
`memory_lease_expired` (409) and `memory_vector_invalid` (422).

Default grants: an enrolled node token reads and writes the `fleet` scope and
the `repo:` scopes, because nodes are operator machines. A message principal
(a cloud agent) has no memory grant until the admin adds one. The admin token
holds every grant, including review.

### Storage

Tables in `fleet-hub-memory.db`:

| Table | Key | Holds |
|---|---|---|
| `memory_events` | `seq` | the log |
| `memory_blobs` | `content_hash` | card bytes, stored once per distinct content |
| `memory_card_versions` | `(card_id, content_hash)` | first `seq`, writer, trust, scope |
| `memory_cards` | `card_id` | current `content_hash`, state (`live` or `retired`), scope, trust, title, summary, tags |
| `memory_vectors` | `(content_hash, embedder_id, normalizer_version)` | float32 little-endian vector, L2-normalized |
| `memory_lineage` | `(card_id, content_hash, artifact, artifact_key)` | which derived rows came from which version |
| `memory_embed_jobs` | `(content_hash, embedder_id)` | state, lease holder and expiry, attempts, last error code |
| `memory_proposals` | `proposal_id` | state, tier, ops, evidence references, base hashes, policy digest, decision |
| `memory_handoffs` | `handoff_id` | submitted handoffs waiting for review |
| `memory_grants` | `(principal, scope)` | read, write, handoff, embed and reflect rights |
| `memory_recall_log` | `rowid` | time, principal, query HMAC, query source, returned card ids, mode |
| `memory_meta` | `key` | default embedder, normalizer version, index format version |

Fingerprint memoization: the vector key is the content hash, the embedder id
and the normalizer version, with no card id in it. Two cards with the same
text share one vector, an unchanged card is never re-embedded, and a new embedder means a deterministic
backfill of exactly the rows missing for its id. Lineage lets `card.retire`
and supersession remove exactly the derived rows a version produced, without
replaying the transform, even after the embedder has changed.

### Index structures

The index lives in memory, is rebuilt at startup from `memory_cards` and
`memory_vectors`, and is then updated by the memory writer after each commit.
It is derived state: deleting it changes speed, never results. It is not a
result cache.

- Document table, as parallel arrays: card id, content hash, live bit, scope
  id, trust, and token length.
- Lexical: a sorted term array, one offsets array, one contiguous postings
  array of document numbers and one of term frequencies, plus a small delta
  segment for recent writes. Retired and superseded documents are masked by
  the live bit. When the delta segment passes 10% of the main segment, the
  writer rebuilds the main segment and swaps it in.
- Dense: one contiguous float32 matrix per embedder id (rows by dimension),
  a row-to-document map, and a has-vector bit per document. New vectors are
  appended and stale rows masked. Compaction rebuilds the matrix.
- Readers take a reference-counted snapshot for the whole query, so a query
  never sees a half-applied update.

Size at 20,000 cards and 1,024 dimensions: about 80 MB of float32 vectors plus
postings in the tens of megabytes. Beyond about 100,000 cards the matrix needs
half precision or an approximate index. That is outside v2.

Tokenization must match `memory_bm25.tokenize` exactly: lowercase, then the
Python `\w+` pattern. Python's and Rust's Unicode word classes differ on some
code points, so S13 adds a tokenizer golden file generated by Python over the
public fixture and a Unicode sample, and the Rust tokenizer must reproduce it.

### Retrieval pipeline

1. Normalize the query as the recall hooks do: one line, at most 256
   characters.
2. Build the candidate set from the caller's readable scopes and the live
   bit.
3. Lexical: BM25 with k1=1.2 and b=0.75 over one field (title, tags, summary
   and text), top 50, scores rounded to 4 decimals, ties broken by `card_id`.
4. Dense: embed the query within a 300 ms budget, take dot products against
   the matrix rows in the candidate set, top 50. A document without a vector
   for its current content is left out of the dense list and stays findable
   lexically.
5. Fuse with reciprocal rank fusion: score = sum over lists of
   weight / (60 + rank), weights 1.0 until the eval says otherwise.
6. Return at most 20 matches with `card_id`, `title`, `summary`, `tags`,
   `score`, `content_hash`, `trust`, `scope` and each list's rank. The
   response says which mode ran (`lexical`, `hybrid`) and why a hybrid query
   degraded (`embedder-timeout`, `embedder-unavailable`, `no-vectors`).
7. Optional rerank (D9): a caller that wants one answer reranks the top 20
   with its own read-only seat, through the planned `memory answer`. The hub
   is not involved.

The `search_cards_payload` shape stays. `path` for a hub card is
`cards/<card_id>.md`, so existing callers that read a card by basename keep
working.

Latency budget, warm: lexical under 1 ms and the dense scan under 10 ms at
20,000 cards. Query embedding dominates (61 ms median measured for the 0.6B
model on a desktop CPU). Target p95 for a hybrid query: 200 ms. A query that
falls back to lexical answers in under 20 ms.

### Eval gates

All gates run `python -m brigade.memory_retrieval_eval`, which gains two
adapters: `hub-lexical` and `hub-hybrid`. Each starts a `brigade-hub` on a
loopback port with a temporary memory database loaded from the fixture. They
report "skipped" when the binary or an embedding server is missing, like the
`semantic` adapter does today. A gate run requires them. Private gold sets run
through `--fixture-root` and report aggregate numbers only.

| Gate | Requirement | Blocks |
|---|---|---|
| R0, lexical parity | `hub-lexical` returns the same ids in the same order as `bm25` for every query in `evals/memory-retrieval`, and the same aggregate scores on a private corpus snapshot | enabling hub search for any client |
| R1, hybrid default | On the public fixture, `hub-hybrid` is at least `bm25` overall and in every category, and strictly better in `paraphrase` or `abbreviation`. On the private real-prompt gold set, hit@5 improves by at least 0.10 under at least two of three independent judge label sets, and MRR does not drop. On private synthetic keyword queries it loses at most 0.05 R@5. | switching the default mode to `hybrid` |
| R2, rerank | First-result correctness on the private gold set improves by at least 0.15 over `hub-hybrid` | enabling `memory answer` by default |

Measured so far: the 0.6B hybrid passes the real-prompt part of R1 by a wide
margin (hit@5 0.83 to 0.94 against 0.66 to 0.74), but loses on synthetic
keyword queries (R@5 0.892 against 0.967 for BM25). RRF weights must close
that gap before the default switch. Until R1 passes, the default mode is
`lexical` and `hybrid` is opt-in.

### Embedder interface and backends

```text
EmbedderId = (name, model_digest, dimensions, normalizer_version, query_prefix_version)

trait Embedder {
    id() -> EmbedderId
    embed(kind: query | document, texts, deadline) -> vectors or EmbedError
}
```

- `model_digest` is what the embedding server reports for the loaded model.
  If it changes, the id changes, and the hub never mixes vectors from two
  models in one matrix.
- Document text for normalizer 1: title, summary and body, cut at the
  model's input limit.
- Vectors are checked before storage: right dimension, all finite, nonzero
  norm. They are stored L2-normalized.

Backends:

| Backend | Document vectors | Query vectors | Where text goes |
|---|---|---|---|
| `hub-cpu` | the embed scheduler calls an embedding server on the hub's loopback | same server, synchronously, within the 300 ms budget | stays on the hub host |
| `remote` | an embed worker on the GPU host pulls jobs from the hub (`embed_lease`), embeds locally, and submits vectors (`embed_submit`) | the hub calls the GPU host's server directly when a `query_endpoint` is configured and reachable. Otherwise lexical only | the GPU host |

The HTTP client speaks two request shapes, the Ollama `/api/embed` shape and
the common `/v1/embeddings` shape, selected in `hub.toml`. Embedding
endpoints must be loopback or on an allowlist of private hosts, the same
pattern as the events callback hosts. The embed worker is the planned
`memory embed-worker`, stdlib Python. It holds a principal with only the
`embed` grant and pulls, so the GPU host needs no inbound route from the hub
for document work.

During an embedder change both matrices exist. The new one backfills in the
background, and the default flips by configuration only after the new id
covers every live card and passes R1. The old matrix is dropped one release
later.

### Embedder benchmark plan

Read-only, and never on the production hub host.

- Host for `hub-cpu`: a non-production machine with the hub host's CPU
  architecture, limited by cgroup to the hub's CPU and memory allotment.
- Host for `remote`: the GPU host.
- Data: a copy of a frozen corpus snapshot. Nothing writes to a live hub.
- Measure for each backend and model: document throughput (cards per
  second), query embedding latency p50, p95 and p99 at 16, 64 and 256
  tokens, cold start time, resident memory, CPU seconds per query, the hub
  load test (S6) run at the same time with its writes per second and p99
  compared with a run without embedding, and the behavior with the
  embedding server stopped (lexical answer within the budget).
- Quality: R1 numbers per model.
- Decision rule: `hub-cpu` becomes the default when its query p95 is at most
  150 ms inside the allotment, its resident memory leaves at least 30% of the
  allotment free, and the concurrent load test still clears 4,600 writes per
  second. Otherwise `remote` is the default and `hub-cpu` stays available.
- Output: one results table in the S16 PR, aggregate numbers only, and the
  default it sets.

### Local memory and the hub

A workspace chooses one of three modes in its memory configuration:

| Mode | Search | Writes |
|---|---|---|
| `off` (default) | local cards only, as today | local only |
| `read` | hub and local results fused with RRF, deduplicated by `(card_id, content_hash)`, each match labeled `source: hub`, `local` or `replica` | local only |
| `sync` | as `read` | local card changes are also published to the hub as `card.put` events |

- Hub search has an 800 ms budget. On timeout or error the client answers
  from local cards and the replica, and says so in the result.
- The replica, `.brigade/memory/hub-replica/`, holds the current versions the
  principal can read and a `seq` watermark, capped at 50 MB by default. It is
  per host, ignored by Git and excluded from vault sync, like the per-host
  index from #2196.
- The spool, `.brigade/memory/hub-spool/`, holds writes made while the hub is
  unreachable. They flush in order with their `client_event_id`. A
  `memory_base_mismatch` turns into a local proposal for review.
- Cards marked `visibility: private` in front matter are never published.
- Hub Lite and Python hubs answer 404 on `/memory`. The client then behaves
  as in `off` mode, with no error.

Privacy:

- Prompt-driven recall sends the query to the hub over the authenticated
  transport. The hub keeps it in memory for that request only.
- The hub recall log stores an HMAC-SHA256 of the normalized query under a
  hub key, plus the query source and returned ids. The key lives in a 0600
  file beside the token file, not in the database, so a database copy alone
  cannot test guesses.
- The hub never stores raw query text, including for explicit searches. The
  local log keeps its #2196 rules.
- Query text reaches an embedding server. That server is inside the trust
  boundary, which is why endpoints are limited to loopback or an allowlist.
- S14 repeats the #2196 sentinel test on the hub: after a recall with a
  sentinel prompt, the sentinel appears in no database page, WAL, log or
  metric.

### Reflect, proposals and promotion on hub memory

Reflect stays a batch job (#1856), scheduled by `brigade care install`. On a
hub-backed workspace:

1. The job reads `log` from its `seq` watermark and builds candidates as it
   does locally: evidence ids, proof counts, contrary ids, trust, scope and
   base hashes. Local evidence ids are resolved on the host that ran the job.
   Proposals carry the resolved references with their digests.
2. It submits `propose` with the proposal v2 typed ops against block ids
   (`append_evidence`, `replace_block`, `remove_block`, `add_previously`,
   `supersede_card`, `merge_exact_duplicates`).
3. The hub validates every op against the base bytes, computes the tier
   itself from the op types and scopes, and ignores any tier the submitter
   claims. Invalid ops are dropped. A proposal with every op dropped is
   `failed`, and no card changes.
4. A decision is either a reviewer's `decide` (an admin or a principal with
   the review grant) or the hub's policy engine for Tier 0, which applies only
   when every gate in the table below holds.
5. Applying a proposal appends one `card.put` per changed card, with trust
   `policy-promoted` or `reviewed`, lineage to the proposal, and the
   compare-and-set check on each base hash. A moved base marks the proposal
   `stale`, and the hub never rebases it automatically.
6. `memory reflect rollback` appends a compensating proposal that restores
   the prior content hash.

| Tier 0 gate | Source of truth on the hub |
|---|---|
| Policy enables the op class | `memory` section of the current fleet policy revision. Its digest is bound into the proposal |
| Proof: at least 3 distinct evidence ids from at least 2 sources, no contrary ids | proposal evidence references, recounted by the hub |
| Sources trusted and scan-clean | submitter in the policy's reflect principals. Card text scanned on write |
| Retrieval eval gate passed | the latest R0 or R1 record for the active mode |
| Blast cap and per-card cooldown | `memory_proposals` history |
| Brigade is the configured memory owner | the policy's curation section names the hub. With an external owner or a handoff-only policy, reflect emits proposals and never applies |

Invariant tests, run against the hub in S19:

- A Tier 1 op never auto-applies without a calibration record. A mutation
  test removes the check and must fail.
- Tier 2 always waits for review.
- A failed worker call, invalid ops, a failed eval gate or a failed write
  leaves every card version and every vector byte-identical and records
  `failed`. Repeated failures pause the scheduled job until a success.
- A policy edit invalidates pending auto-promotions, because the policy
  digest is part of the binding.
- Auto-applied decisions are distinguishable from reviewed ones in the
  decision log and reversible by a compensating proposal.
- A new version without a vector yet keeps the card findable lexically. A
  failed embedding never removes a healthy vector of an older version until
  the new one exists.

### Content scanning

Every `card.put` and `handoff.submit` payload is scanned with the guard rule
set through the native PCRE2 engine (D16). A blocking finding refuses the
write with `422` and `code: "memory_content_blocked"` plus rule ids, never the
matched text. A warning stores the version with trust `flagged`, which is
searchable on request and never pushed. Until the engine lands, versions
written by principals other than admin are stored as `agent-unreviewed`, and
Tier 0 promotion is off because the scan-clean gate cannot hold.

## Connectivity

### What each agent class gets pushed

| Agent class | Pushed content | Existing call it rides on | Size bound |
|---|---|---|---|
| Local harness (hooks in Claude Code, Codex and others) | hits for the repository context at session start, labeled `query_source: "cwd"`, and hits for each prompt | the existing SessionStart and UserPromptSubmit recall hooks | 10 lines, 5 s timeout (current caps) |
| Local harness, session end | capture: hashed session reference, card ids pushed, card ids opened, outcome if known | the existing Stop hook | one `recall.capture` event, ids only |
| Cloud connectors (Dot, Duet) | a `memory` section in `fleet_digest` with the top 5 for the principal's charter duties and oldest unacked messages, and `related_memory` on each `inbox_list` item with the top 3 for that message | `fleet_digest`, `inbox_list` | 5 and 3 items, summaries at most 300 characters, 4 KiB per response |
| Grok Bot | card references (id and content hash) in job metadata, and card text only through its memory profile tools | the Grok Bot job envelope, plus `memory_search` and `memory_card` | 5 references |
| T3 children and `brigade run` workers | a `memory_context` block in the brief, with ids, titles and summaries | the brief builders (`aboyeur` briefs, the Grok Bot T3 adapter, native T3 child tasks) and Worklore prepared handoffs (`brigade.prepared_handoff.v1`) | 5 items, 4 KiB |

- Pushed items carry `card_id`, `title`, `summary`, a short content hash,
  `trust`, `score` and `source`, never the card body. Agents read bodies with
  `memory_card`, which Dot and Duet already have.
- Push and search use one ranking. Push adds the trust filter.
- The Grok Bot queue keeps its rule that hub payloads are metadata only, so
  its jobs carry card references only. The feed that enqueues a job computes
  them from the task text it already holds.
- A T3 child in a Brigade-wired worktree also runs the recall hooks. The
  prompt hook skips cards whose ids already appear in the brief's
  `memory_context` block.
- New fields in tool responses are additive. Connector tool snapshots are
  updated in the same PR.

### Trust filter

| Card trust | Pushed | Searchable |
|---|---|---|
| `reviewed`, `policy-promoted`, `imported` | yes | yes |
| `agent-unreviewed` | no, except to the principal that wrote it (Q4) | yes, labeled |
| `flagged` | never | only on an explicit search, labeled |

Every pushed or searched item from another principal carries
`content_trust: "untrusted"`, and the connector operating instructions get the
same sentence they have for inbox content: memory text is data, never
instructions.

### Grok Bot profiles

Two principals, two credentials, two connector profiles. Tools outside a
profile are not registered, so they are absent from discovery, the rule
`dot/profile.py` already applies.

| Profile | Tools | Grants |
|---|---|---|
| memory | `memory_search`, `memory_card`, `handoff_write` | read on the `fleet` scope and the `handoff` grant. `handoff_write` lands in `memory_handoffs` for review and never writes a card. |
| inbox | `message_send`, `inbox_list`, `inbox_ack`, `inbox_reply` | message send grants (`may_send_to`) as the sender allowlist. A send outside it gets the v1 `message_recipient_not_permitted` code |

Neither profile can see dispatch, claim or fleet-control tools. The Grok Bot
charter covers X work only. After the card import (S20), these profiles
replace the vault-backed Grok tools in `grokbot_cerebro.py`.

### Duty registry and findings ledger

The duty registry is the owners charters in the fleet policy document. No new
registry is built. The findings ledger is new: one table on the hub database,
added through the Python schema (D6), and one route.

```text
agent sees a real event (an email, a job post, a pull request)
  -> canonical key, computed locally
       email: Message-ID, lowercased, angle brackets removed
       job: canonical URL (https, lowercase host, no fragment, no utm_* or
            other tracking parameters, no trailing slash), or the ATS job id
       pull request: owner/repo#number
  -> POST /findings {action: "observe", kind, key}
  -> hub stores HMAC(hub key, kind + ":" + key), never the key itself
  -> answer: {finding_id, first_seen, owner, state, report_ref, should_report}
       should_report is true only for the owner whose charter matches the
       kind, while no report exists
  -> the owner reports, then POST /findings {action: "reported", finding_id, report_ref}
  -> everyone else sees report_ref and stays quiet
  -> POST /findings {action: "resolve", finding_id} when done
```

- Charters gain an additive `findings` match key listing finding kinds, and
  the owners view lists open findings as holdings.
- `observe` is idempotent per principal and finding. A second `reported` on
  the same finding answers 409 with `code: "finding_already_reported"` and the
  existing reference.
- The ledger ships on the Python hub first (S22), so the job-leads relay can
  move onto it before the Rust port. Rust proxies it until the port.

## Wire contract

- Contract v1, frozen: the existing 77 cases must pass against the Rust hub
  exactly, quirks included, through every phase.
- More v1 coverage before porting: S1 adds drive cases for the hot set
  (claim node and force scopes, leases and fencing, the message actions
  `reply`, `sent_lookup`, `who`, `claims`, keys and manifests, the question
  actions, sealed envelopes and `wait_ms`). They record what Python does today, so `contract_version`
  stays 1.
- Additive routes get drive profiles: `memory` (S13), `findings` (S22) and
  `ops` for `/metrics` (S9). Each case pins status, keys, types and `code`
  values, as v1 does. New routes use `{error, code}` for every 4xx, since
  they have no legacy shape to match. The `core` profile stays the default,
  and the drive stays hub-agnostic (no `brigade.*` imports).
- Contract v2 (#2207), after phase 3: one auth-failure envelope and status, a
  `code` on every 4xx JSON error, JSON refusals on JSON routes, 403 for a
  principal token on node routes, and `GET /version` reporting
  `contract_version`, the engine, and capabilities. The identity capability
  list gains `memory` and `findings`. If the Python hub is still inside its
  rollback window, it changes in the same PR, and the goldens move to 2.

## Build plan

Each slice is one PR. Acceptance tests are what the PR must show. "Parallel
with" lists slices that touch different code and can run at the same time.

### Hub track

| ID | Slice | Depends on | Acceptance | Parallel with |
|---|---|---|---|---|
| S1 | Contract drive: hot-set coverage on the Python hub (claim scopes, leases and fencing, message and question actions, sealed envelopes, and the `wait_ms` wake and waiter cap) | none | new cases pass on the Python hub. Inventory and `COVERED_ROUTES` updated. The drifting-proxy test still finds each injected drift | S2, S3, S16, S22 |
| S2 | Storage parity drive: scripted sequence, normalized table dump, golden from Python, events outbox enabled | none | deterministic golden on two runs. A test that changes one timestamp format and one JSON separator is detected | S1, S3, S16, S22 |
| S3 | `brigade-hub` crate: config and flag parity, conventions layer, admission, native `/health`, everything else proxied, and a Linux CI build | none | the full drive passes with Rust in front of the Python upstream. Flags rejected or required exactly as `fleet serve` does | S1, S2, S16, S22 |
| S4 | SQLite store: writer with group commit and durability classes, reader pool, structural schema check and `required.json`, `data_version` watch, checkpoints | S3 | one failing command does not affect its batch. 20 `kill -9` runs under load leave `integrity_check` ok and lose no acknowledged write. A schema mismatch refuses to start. The Python lockstep test fails on a schema bump | S9 |
| S5 | Native registration and authentication: admin, node and principal tokens, `/nodes`, `/message-principals` | S4 | drive cases for these routes pass natively. Storage parity matches. A revoked token is refused on the next request | S9 |
| S6 | Native heartbeat: `/events` (batches up to 1,000), `/status`, `/sessions`, plus a load harness in `engines/hub/bench/` (development only, not released) | S5 | drive and storage parity pass. At least 4,600 OK single-event writes/s with one connection per request at 16 to 64 clients, p99 under 250 ms. No refused connections at 256 clients. Also reported with keep-alive. The run with the Python upstream writing cold routes is recorded | S7, S8, S13 |
| S7 | Native claims and owners: all scopes, leases, fencing, generations, `GET /owners` | S5 | S1 claim cases pass. Property test with 64 concurrent clients finds no two holders and no repeated generation. The `kill -9` test covers durable batches | S6, S8, S13 |
| S8 | Native messages: every action except `events_*`, including the `question_*` actions, with sealed validation, retention and long-poll | S5 | S1 message, sealed and long-poll cases pass. 10,000 idle long-polls at under 20 KB each. A send wakes its waiter within 50 ms at p99. The differential JSON fuzz (10,000 bodies) gives the same status and error text on both hubs | S6, S7, S13 |
| S9 | Route modes, shadow comparator, shared normalization file, `/metrics`, structured logs | S3 | comparator unit tests use the drive's normalization data. An injected mismatch is counted and retried once. `ops` drive cases cover `/metrics` authentication | S4 to S8 |
| S10 | Packaging and operations: release build for five platforms, manifest entry, `setup --component brigade-hub`, `fleet serve --engine rust`, upstream mode, container recipe, cutover and rollback runbook | S6 (S3 for the build part) | published-acceptance installs the binary and runs the drive. `fleet serve` without `--engine` is unchanged. Rollback test: stop Rust, start Python on the same files, drive passes and every row reads | S7, S8 |
| S11 | Contract v2 (#2207) in both hubs | cutover phase 3 | goldens move to version 2 and `GET /version` answers. Every client caller from the contract doc checked with `brigade code callers` | S12 |
| S12 | Cold families, one PR each: preference and audit, mesh, policy (reads first), models and cloud, the Grok Bot queue, CI with reports and actions, Worklore, findings, and webhook delivery with retention sweeps | S9, and S1-style drive cases for the family written first | that family's drive cases pass natively. Storage parity matches. Shadow clean for 7 days before native | each family independent |

### Memory track

| ID | Slice | Depends on | Acceptance | Parallel with |
|---|---|---|---|---|
| S13 | Memory store and routes, lexical: event log, versions, blobs, compare-and-set, idempotency, grants, scopes, lexical index, tokenizer golden, `memory` drive profile | S5 | drive cases pass. R0 passes on the public fixture. A stale `base_hash` gets 409 `memory_base_mismatch`. A replayed `client_event_id` returns the original result | S6, S7, S8 |
| S14 | Python client: the planned `memory hub status`, `push`, `pull` and `import`, search fusion and fallback, the replica, the spool, the hub recall log, and Dot and Duet memory tools routed through it | S13 | with the hub stopped, search answers locally and labels it. The sentinel prompt appears nowhere on the hub or the client. A conflicting offline edit becomes a proposal. `off` mode output is byte-identical to today | S15, S19 |
| S15 | Embedder interface, both HTTP backends, embed jobs and `memory embed-worker`, vectors, lineage, dense index. Default stays `lexical` | S13 | an embedder change backfills only missing rows and leaves the old matrix intact. Retiring a card removes exactly its lineage rows. A failed embedding keeps the card findable lexically. Endpoints outside the allowlist are refused | S14, S16 |
| S16 | Embedder benchmark and default decision | none (uses an embedding server and a corpus copy directly) | the results table and the default it sets, following the decision rule | everything |
| S17 | Hybrid retrieval and the `hub-lexical` and `hub-hybrid` eval adapters, RRF weight tuning, and the R1 run | S15, S16 | R1 report attached. The default flips to `hybrid` only if R1 passes | S18, S19 |
| S18 | Content scanning on memory writes | S13, the PCRE2 engine crate | a blocking rule refuses with rule ids only. A warning stores `flagged`. Flagged versions are never pushed | S17, S19 |
| S19 | Reflect on hub memory: `propose`, `decide`, `rollback`, hub tier classifier, Tier 0 gates, policy `memory` section | S13, #2187 merged | every invariant test in [Reflect](#reflect-proposals-and-promotion-on-hub-memory) passes, including the Tier 1 mutation test. `brigade memory reflect run --dry-run` writes nothing to the hub | S14, S17 |
| S20 | Card import: dry run, idempotent, keeps card ids, deduplicates by content hash | S14 | a second import appends nothing. The dry run lists exactly what the real run appends | S15 to S19 |
| S21 | `memory answer`: client-side rerank through a read-only seat | S17 | R2 report. Off by default unless R2 passes | after 0.31.0 is fine |

### Connectivity track

| ID | Slice | Depends on | Acceptance | Parallel with |
|---|---|---|---|---|
| S22 | Findings ledger on the Python hub: table, `/findings`, canonical keys, charter `findings` match, `findings` drive profile | none | drive cases pass. The second observer of one event gets `should_report: false`. A duplicate report gets 409. Only HMACs are stored | S1, S2, S3 |
| S23 | Push in tool responses: `fleet_digest` memory section, `inbox_list` `related_memory`, trust filter, size bounds | S14 | connector snapshots updated. Responses stay under the bounds with a 2,000-card fixture. Unreviewed cards from other principals never appear | S24, S25 |
| S24 | Hooks: hub recall at session start and per prompt, Stop capture, deduplication against brief context | S14 | the capture holds ids only. Hooks keep their 5 s cap with the hub stopped | S23, S25 |
| S25 | Dispatch packets: `memory_context` in `aboyeur` briefs, the Grok Bot T3 adapter, native T3 child tasks and Worklore prepared handoffs. References only in Grok Bot job metadata | S14 | each packet stays under 4 KiB. Grok Bot job payloads still contain no task text | S23, S24 |
| S26 | Grok Bot memory and inbox profiles, and retirement of the vault-backed Grok tools | S13, S20 | each profile's tool discovery lists only its tools. A send outside the allowlist gets `message_recipient_not_permitted`. No dispatch tool is reachable | S23 to S25 |
| S27 | Connector measurement, repeated with the same 10-prompt method | S23 to S26 | card-backed answers on at least 8 of 10 prompts, up from 3 of 10 | none |

### Order and lanes

```text
now:        S1   S2   S3   S16  S22
                       |
                       S4 ---- S9
                       |
                       S5
                 /     |     \       \
               S6     S7     S8      S13
               |                    /  |   \     \
              S10                 S14 S15  S18   S19 (needs #2187)
                                  / | \   \
                         S20  S23 S24 S25  S17 (needs S16) -> S21
                          |
                         S26 -> S27
after phase 3: S11, then S12 one family per release
```

## Release gate for 0.31.0

Proposed, and it depends on Q1:

- In the release: S1 to S10, with cutover phase 3 complete for at least the
  heartbeat and claims families, plus S13 to S17 (the default stays
  `lexical` if R1 fails), S19, S20, and S22 to S26.
- In the release if the PCRE2 engine is ready: S18. Without it, agent-written
  versions stay unpushed and Tier 0 stays off.
- After the release: S11 (contract v2), S12 (cold families), S21, and S27 as
  soon as S23 to S26 are live.

## Risks

| Risk | Effect | Mitigation |
|---|---|---|
| Behavior the drive does not pin differs between hubs (timestamps, JSON encoding, parser edge cases, side-table writes) | Rows Python misreads after a rollback. Silent client differences | Storage parity drive (S2), differential JSON fuzz (S8), shadow mode before every native flip, one error-text table |
| `synchronous=NORMAL` loses the last commits on power loss | A lost claim grant or a reused fencing generation | Durable batches commit with `FULL` (D3, Q2). A process crash alone loses nothing in WAL mode |
| Two writers on one file during the rollback window | Writer stalls behind the Python upstream | Proxied families are the low-rate ones. S6 measures the mixed case. The writer queue sheds load with 503 |
| A Python schema change lands without a Rust update | Rust refuses to start after an upgrade | The lockstep test fails the Python PR. The structural check names the missing table or column |
| Pushed memory carries prompt injection between agents | One agent's text steers another | Push only reviewed, promoted or imported cards, mark them `content_trust: "untrusted"`, send summaries only within size caps, and scan on write |
| Query text reaches an embedding server | A privacy boundary outside the hub | Loopback or allowlisted private endpoints only. Documented as inside the trust boundary |
| The hub becomes a single point of failure for memory | Agents lose recall when it is down | Local replica and labeled fallback, with `off` mode unchanged |
| Auto-promotion on shared memory reaches more agents than a local card did | A bad promotion spreads fast | Tier 0 only, blast cap, per-card cooldown, policy digest binding, compensating rollback |
| The hub-CPU embedder starves the writer | Throughput or latency regressions | The benchmark measures the concurrent load test. The decision rule requires 4,600 writes/s alongside embedding |
| New Rust dependencies in a network-facing service | Supply-chain exposure | Small dependency set, no OpenSSL, lockfile reviews, advisory and license checks in CI |
| Scope: 54 JSON and 37 HTML routes | A full port delays the release | Strangler routing (D5), hot set first, cold families after the release |

## Open questions

These need the maintainer. Each has a recommendation.

1. **Q1, release scope.** May 0.31.0 ship with the Python hub serving the
   cold JSON families and the HTML deck as a loopback upstream behind the
   Rust hub? Recommended: yes. Requiring every JSON route to be native puts
   S12 on the release path, including the policy and routing modules (about
   4,700 lines of Python).
2. **Q2, durability.** Approve `FULL` commits for batches with claim, fence
   and credential writes on top of the `NORMAL` default (D3)? Recommended:
   yes. The alternative is `NORMAL` everywhere, as first decided, which
   accepts a small window of lost claims after a power loss.
3. **Q3, publishing default.** Once a workspace has a hub, should its local
   cards publish only after an explicit `sync` opt-in? Recommended: opt-in
   per workspace. Publishing by default would copy every existing card set to
   the hub on upgrade.
4. **Q4, unreviewed cards.** Should agent-written cards that no one has
   reviewed stay search-only and never be pushed to other agents?
   Recommended: yes.
5. **Q5, rollback window.** Keep the Python hub shipped and drive-tested for
   two releases after cutover, matching the engine shim policy, before schema
   authority moves to Rust? Recommended: two releases.

## Rollback plan

- One family: set its mode back to `proxy` and restart. Takes seconds. The
  Python upstream runs through every phase.
- The whole Rust hub: drop `--engine rust` from the serve command, or start
  the pinned per-SHA virtualenv's `brigade fleet serve` on the same port and
  files. This works because Rust never runs DDL on the hub file (D6), writes
  rows that pass storage parity, and keeps memory in a separate file Python
  never opens. Clients see memory routes answer 404 and fall back to local
  search.
- Memory: the runbook adds `fleet-hub-memory.db` and its key file to the
  operator's backup set. The index and vectors can be rebuilt from the log,
  and promotions are reversed with compensating proposals.
- Contract v2 lands only after phase 3, in both hubs at once, so a rollback
  never mixes contract versions.
- After the rollback window, schema authority moves to Rust, and a rollback
  to Python needs a database backup from before the first Rust migration.

## Evidence

Measured 2026-11-01 and 2026-11-02 on a 24-thread desktop-class machine with
NVMe storage, over loopback, with throwaway databases. No live hub was used.

Hub, as shipped (one event per request, one connection per request): 220
req/s at 1 client, a peak of about 460 req/s at 8 to 16 clients with p99 of
333 to 535 ms, and refused connections from 32 clients up. Batching 1,000
events per request reached 37,500 events/s, so the per-request cost
dominates. Setting only `synchronous=NORMAL` did not move the peak.

Same logic in three servers (WAL, `NORMAL`, keep-alive): Rust 15,053 to
18,039 req/s at 16 clients, async Python 4,624 to 5,331, threaded Python 2,293
to 2,462. With `synchronous=FULL` all three landed in the same band, about
400 to 1,500 req/s with wide run-to-run swings, bound by disk syncs. Idle
long-polls: Rust and async Python served 10,000 connections, and threaded
Python failed past about 8,400 threads.

Retrieval, 1,891 real cards, 62 judged real prompts, three independent judges
(pairwise Cohen's kappa 0.55 to 0.73):

| Method | hit@5 range across judges | First result correct |
|---|---|---|
| Substring scorer (before #2201) | 0.39 to 0.53 | 0.08 |
| BM25 | 0.66 to 0.74 | 0.39 |
| Dense, 8B embedder | 0.90 to 0.94 | 0.73 |
| Hybrid RRF, 0.6B embedder | 0.83 to 0.94 | 0.60 |
| Hybrid RRF, 8B, with an LLM rerank of the top 20 | 0.97 to 0.98 | 0.90 to 0.92 |

First-result numbers in the first four rows come from one judge's labels. The
rerank row spans the judges.

Synthetic set, 240 queries over 120 sampled cards, R@5: BM25 0.812 (keyword
0.967, paraphrase 0.658), hybrid 0.6B 0.829 (keyword 0.892, paraphrase
0.767), hybrid 8B 0.854. Two CPU cross-encoder rerankers lowered
first-result correctness. Heading boosts did not beat plain BM25.

Cost: the 0.6B model indexed 17.5 cards/s (1.8 minutes for 1,891 cards), with
a 7.7 MB float32 matrix and a 61 ms median query embedding. The 8B model
indexed 2.9 cards/s with an 81 ms median. A brute-force scan took 3.0 to 7.0
ms from 2,000 to 20,000 cards at 1,024 to 4,096 dimensions. Pure-Python BM25 at 100,000 cards took 10.3 s
to build and 1.4 GB of memory, which is one reason the index moves into the
Rust hub.

Guard rules, 2 MB corpus: Python `re` 4.3 MB/s, PCRE2 with JIT about 24 MB/s
with identical match counts on every rule.
