# Brigade v2: Rust Fleet Hub, hub memory, reflect and push connectivity

Design for the 0.31.0 release, "Brigade v2". Besides the rebrand, the release
ships four pieces that depend on each other:

1. A Rust Fleet Hub (axum, tokio, rusqlite) that replaces the Python hub with
   no client changes during the cutover.
2. Shared memory hosted on the hub, with no single owner, that every agent can
   use without setting up an MCP server or a tunnel.
3. `brigade memory reflect` (#1856) proposals and review on hub memory, with
   policy-gated promotion once the hub can verify evidence.
4. Memory and messages pushed to cloud agents through calls they already make,
   instead of waiting for them to pull.

This page records the decisions, the design, and a build plan of PR-sized
slices. It changes no code. Related work: the frozen wire contract and its
drive (#2204, [phase-hub-contract.md](phase-hub-contract.md)), contract v2
(#2207), BM25 search and prompt recall (#2201, #2196), the Tier 0 preflight
(#2187), hub work dependency edges (#2210), native engine distribution
(#2148), the engine rename plan (#2213,
[phase-engine-rename.md](phase-engine-rename.md)), the native PCRE2 guard
(#2212), and sealed messages (#2005, [phase-agent-mesh.md](phase-agent-mesh.md)).

Revision 2 (2026-11-03) answers two independent design reviews of the first
draft. Both asked for targeted rework. The largest changes: every commit stays
at `synchronous=FULL` as today (D3), memory cards are keyed by scope and
compare-and-set uses a revision (D22), memory commits are linearized against
credential and policy changes (D23), the proxy has a failure contract (D26),
the production drive gate uses a read-only profile that this plan builds
(S29), and oversized slices are split. [Review resolution](#review-resolution)
lists every finding and where it was resolved.

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
- [Work in flight](#work-in-flight)
- [Build plan](#build-plan)
- [Release gate for 0.31.0](#release-gate-for-0310)
- [Risks](#risks)
- [Open questions](#open-questions)
- [Rollback plan](#rollback-plan)
- [Evidence](#evidence)
- [Review resolution](#review-resolution)

## Goals and non-goals

Goals:

1. During the cutover the Rust hub passes the v1 contract drive exactly,
   recorded quirks included, on the same database file. Operators change one
   serve flag and clients change nothing. Later, the connectivity slices (S23
   to S26) add fields to connector tool responses and hooks.
2. Write throughput at least 10 times the Python hub's, measured by the same
   harness on the same machine and allotment (S6a), with every commit at
   `synchronous=FULL`. The release target is 4,600 or more single-event
   `POST /events` requests per second with one TCP connection per request on
   the reference machine, up from about 460. That target is unverified until
   S6 measures it.
3. Long-poll capacity in the thousands: 3,072 waiters in the default profile
   and 10,000 in a high-capacity profile, up from 8 across the whole fleet.
4. Shared memory on the hub that any enrolled principal can read and write,
   curated by a policy that names who may promote what.
5. Ranked retrieval that beats the current BM25 on
   `python -m brigade.memory_retrieval_eval` before any default changes.
6. Reflect proposals and review on hub memory, with the #1856 tier rules as
   tested invariants. Tier 0 auto-apply on the hub stays off until the hub can
   verify evidence (S19e, Q6).
7. Cloud agents receive relevant memory without calling a memory tool.
8. One real-world event produces one report across all agents, with a stated
   at-least-once bound across crashes.
9. Every step can roll back to the Python hub on the same database until
   schema authority moves to Rust.
10. No acknowledged write is lost on power loss. This is today's behavior, and
    v2 keeps it.

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
- Unattended edits to skills, instruction files, policy, or the claim text of
  canonical cards. Those always wait for review.
- Exactly-once delivery of external reports. The findings ledger allows one
  reservation at a time and says how retries behave after a crash.
- Memory log compaction in 0.31.0. Its invariants are recorded here (#2210).
- A second task database. Duties extend the existing owners charters.
- New runtime dependencies for the Python package.

## Decisions

Status `Decided` means the maintainer already made the call. `Proposed` means
this document proposes it, and review of this document is the decision point.
`Proposed, changes Dn` marks a proposal that reverses part of an earlier
decision and needs the maintainer's explicit answer.

| ID | Decision | Basis | Status |
|---|---|---|---|
| D1 | The Fleet Hub moves to Rust on axum, tokio and rusqlite. | Same handler logic in three servers: Rust 15k to 18k req/s, async Python about 5.3k, threaded Python about 2.3k (WAL, `synchronous=NORMAL`, keep-alive). Today's hub peaks near 460 writes/s because of its design: 32 handler threads, 8 per source, `request_queue_size=5`, and a new connection plus a schema read on every request. | Decided |
| D2 | One writer thread with group commit, a reader pool, statements prepared once per connection, explicit admission caps and long-poll limits, WAL. Durability is D3. | Profiling put about 0.6 ms of the 0.96 ms per-request cost in opening a connection and reading the schema. A reused connection answers the same query in about 0.001 ms. | Decided, except the `synchronous=NORMAL` setting, which D3 replaces |
| D3 | Every commit on `fleet-hub.db` and `fleet-hub-memory.db` runs at `synchronous=FULL`, as the Python hub does today. Group commit pays one WAL sync per batch. A family may move to `NORMAL` later only by a separate maintainer decision backed by S6 numbers, and never claims, fences, credentials, messages, questions, the audit log, findings or the memory log. | The Python hub sets `PRAGMA journal_mode=WAL` and never sets `synchronous`, so it runs at SQLite's WAL default, `FULL` (2). The first draft's `NORMAL` default would have lowered durability without saying so: after a power cut SQLite can lose acknowledged commits and hand out the same message id or memory `seq` again. Cost with group commit: [Durability](#durability). | Proposed, changes D2 (Q2) |
| D4 | Storage sits behind one interface so Postgres can be added later. SQLite is the default and the only backend in v2. | Keeps v2 small while leaving room for multi-node installs. | Decided |
| D5 | The Rust hub fronts all traffic from its first deploy. Each route family is `native`, `shadow` (Python answers, Rust compares) or `proxy` (forwarded to the Python hub on loopback). | 54 JSON routes and 37 HTML routes, backed by more than 20,000 lines of Python hub code. Porting the hot set first and proxying the rest lets the cutover start long before a full port. | Proposed (Q1) |
| D6 | During the rollback window the Python hub owns the hub database schema. Rust checks `user_version` and the structure it needs, triggers and constraints included, and never runs DDL on that file. Rust's own migrations later start at the version Python reached, plus one. | Rollback to Python needs a database Python accepts. Python refuses any `user_version` above its own. | Proposed |
| D7 | Memory v2 lives in its own SQLite file, owned by the Rust hub. | The Python hub never opens it, so rollback stays clean, and memory gets its own writer and size profile. Authority across the two files is D23. | Proposed |
| D8 | Memory v2 is an event log on the hub. Content hashes address bytes, blobs and vectors. The index is flat postings plus dense matrices per embedder version. Retrieval is BM25 plus dense, fused with reciprocal rank fusion (k=60), with an optional LLM rerank for top-1 answers. Any principal with a grant writes events. Curation is a policy. Derived rows carry fingerprints and lineage. | Judged real prompts: BM25 hit@5 0.66 to 0.74, hybrid with a 0.6B embedder 0.83 to 0.94, an LLM rerank of the top 20 lifted first-result correctness from about 0.64 to about 0.91. | Decided. Card identity amended by D22 |
| D9 | The LLM rerank runs outside the hub, in the caller's own read-only seat. | Keeps model calls and provider credentials out of the hub process and the request path. Matches #1856: no provider integration enters core. | Proposed |
| D10 | Two embedder backends behind one interface: a 0.6B-class model on the hub's CPU, and a separate GPU host that computes and ships vectors. Measure both, then pick the default. | The 0.6B hybrid nearly matched an 8B model on real prompts. The reference hub allotment is 1 vCPU and 512 MB, which probably cannot hold a CPU embedder beside the hub. S16a measures it. | Decided |
| D11 | Both backends talk HTTP to an embedding server: on loopback for the hub-CPU backend, over the private network for the GPU backend. The hub binary links no ML runtime. | Keeps model runtimes out of the five-platform native release matrix. Loopback HTTP adds about a millisecond to a query embedding that measured 61 ms. | Proposed |
| D12 | Memory is pushed to agents. Hub-built packets and the tool responses agents already call (`fleet_digest`, `inbox_list`) carry the top memory hits. Session-start and stop hooks inject and capture. Cloud connectors need no new tools. | Connector test: a hosted assistant used Brigade memory on 4 of 10 prompts and gave a card-backed answer on 3 of 10. Two other connectors used it on 0 of 2 each. | Decided |
| D13 | Grok Bot gets a read-mostly memory profile (`memory_search`, `memory_card`, `handoff_write`) and a separate inbox profile with sender allowlists. Grok Bot does X work only. | Small quota, and a cloud agent should not hold write or dispatch authority it does not need. The connectivity PR in flight ships the read-only part first. | Decided |
| D14 | One owner per duty: the hub's owners charters (`brigade.fleet_owners.v1`) plus a findings ledger keyed by fingerprints (email Message-ID, canonical job URL) with one reservation per finding at a time. | Duplicate reports from several agents watching the same inboxes and feeds. The roadmap forbids a second task database. | Decided. Reservations proposed here |
| D15 | The #1856 tier rules become tested invariants on hub memory: Tier 0 may auto-promote under policy, Tier 1 is review-only until a calibration record exists, Tier 2 is always review. A failed refresh never replaces a healthy card. | #1856 acceptance criteria. On main the tiers are not implemented yet: reflect candidates carry Tier 0 or Tier 2 only, with no Tier 1, calibration record, policy `memory` section or invariant tests. #2187 adds a diagnostic preflight. | Decided as direction. Built in S19a to S19e |
| D16 | Guard scanning moves to a native PCRE2 engine with JIT, built in parallel. The hub links the same engine to scan memory writes, with a Unicode mode the hub needs (S18a). | About 24 MB/s against 4.3 MB/s for Python `re` on the same 20 default rules, with identical match counts. #2212 scans ASCII input and hands the rest to Python `re`, and the hub has no Python fallback. | Decided (engine), Proposed (hub use) |
| D17 | Native engines are renamed `brigade-code-graph` and `brigade-evidence`, with the old names kept as shims for at most two releases. The hub binary is `brigade-hub` and has no legacy alias. The `brigade` command and Python package keep their names. | Naming convention set by the engine rename plan (#2213). | Decided |
| D18 | No runtime dependency is added to the Python core. The Rust hub ships through the native-engine release path (#2148). | Zero-dependency core. | Decided |
| D19 | Cutover runs the Rust hub beside the Python hub, mirrors read traffic, then flips writes. Rollback is the existing per-SHA virtualenv pin. | Lowest-risk path that keeps one database. | Decided |
| D20 | Contract v1 stays frozen for the cutover. Contract v2 (#2207) fixes the recorded quirks after it. | Decided 2026-11-03. | Decided |
| D21 | New routes (`/memory`, `/findings`, `/metrics`, `/ready`) are additive under v1 and each lands with contract drive cases in the same PR. Until v2 adds `GET /version`, a client discovers memory support by probing. `404 {"error": "not found"}` means this hub has no memory. A hub with memory configured but unavailable answers `503` with `reason: "memory-unavailable"`. | The v1 contract defines added routes as non-breaking. The signed hub identity has a closed capability list that the Python hub validates, so it cannot advertise memory under v1. A 404 for an outage would make clients drop `sync` writes. | Proposed |
| D22 | A memory card is identified by `(scope, card_id)`. Every change to a card's head (text, retirement, revival, trust) creates a new `revision`, and compare-and-set uses `base_revision`. The content hash is for deduplication only. | Hash-only compare-and-set cannot see a trust change on identical bytes, a retire and revive, or a return to earlier bytes, and a card id alone collides across repositories. | Proposed |
| D23 | Each memory commit runs while the memory writer holds the hub database write lock, after re-reading the caller's credential and the policy revision under that lock. The memory writer never writes to the hub file. | Credentials and policy live in `fleet-hub.db`. No SQLite transaction spans two files atomically, so without a lock a revocation could commit between the check and the memory commit. | Proposed |
| D24 | Only `decide`, the hub's policy apply and `import` raise a card's trust, and each needs a clean scan of that content. A `put` from any caller, admin included, stores `agent-unreviewed` or `flagged`. If the scanner is unavailable, trust raises and pushes fail closed. | Every node token can write the `fleet` scope. Without this rule a scan-clean write from any node would be pushed to every agent. | Proposed |
| D25 | Hub Tier 0 auto-apply stays off until the hub verifies evidence itself (S19e). Until then every hub proposal waits for review. | Local evidence ids cannot be resolved on the hub, and three submitted strings are not proof. | Proposed (Q6) |
| D26 | The Rust front answers upstream failures with 502 or 504, never 503. `/health` keeps its v1 liveness meaning. A new `GET /ready` reports readiness. Proxied requests that may write are never retried. | The v1 claim client treats a 503 as an answer and maps any other HTTP error to `hub-unavailable` (`fleet_client.py`, the `claim` path). A dead upstream must look like an unreachable hub to that client, so it retries instead of reporting a refusal. | Proposed |
| D27 | Hub work dependency edges and the ready view (#2210) are built on the Rust hub after the Worklore family port. The memory log compaction invariants from #2210 are recorded now. | #2210 asks for one build, in the Rust hub, with a Python version only if the Rust schedule slips. | Proposed (Q7) |

## System overview

```text
  local harness hooks     CLI (fleet, memory, run)     Dot and Duet connectors
  T3 dispatch adapter     Grok Bot relay               embed worker (GPU host)
            \                      |                          /
             \                     |  HTTP/1.1, Bearer token  /
              v                    v                         v
  +----------------------------- brigade-hub (Rust) ------------------------------+
  | accept + admission: connection, source, waiter and body-byte caps, timeouts   |
  |   |                                                                           |
  | route table: family -> native | shadow | proxy                                |
  |   |                    |                      |                       |       |
  | conventions +     shadow comparator      upstream proxy         memory        |
  | native handlers   (Rust reads only)      (raw bytes, no retry   handlers      |
  |   |        |                              of writes)               |          |
  | reader   writer thread    maintenance          |            memory writer     |
  | pools    (group commit,   connection           |            + CPU pool        |
  | (bounded FULL, savepoint  (checkpoints,        |            + embed scheduler |
  | queues)  per command)     retention sweeps)    |                   |          |
  +---|--------|-----------------|-----------------|--------------------|---------+
      v        v                 v                 v                    v
  +------------------------------+     +---------------------+   +----------------------+
  | fleet-hub.db (WAL, FULL)     |<--->| Python hub upstream |   | fleet-hub-memory.db  |
  | schema owned by Python (D6). |     | (loopback): cold    |   | (WAL, FULL), owned   |
  | The memory writer holds its  |     | routes, Command     |   | by Rust              |
  | write lock around each       |     | Deck, webhook       |   +----------------------+
  | memory commit (D23)          |     | delivery            |
  +------------------------------+     +---------------------+
       supervisor (planned `fleet serve --engine rust`): restarts the upstream,
       exits when the Rust hub exits so the service manager restarts both
```

Two processes write `fleet-hub.db` during the rollback window: the Rust writer
for native families and the Python upstream for proxied ones. SQLite
serializes them. The memory writer takes the same write lock around each
memory commit but writes nothing to that file. The proxied families are the
low-rate ones, and S6 measures the mixed case with claims in it.

## Rust Fleet Hub

### Route ownership

Every request enters the Rust process. The route table maps each method, path
and, for `POST /messages`, the body's `action` to a family and a mode.

| Family | Routes | Python housekeeping writes on reads | First mode | Native in slice | Flips |
|---|---|---|---|---|---|
| conventions | auth envelopes for native routes, unknown-path 404, query-token refusal, JSON headers, body framing and bounds | none | native | S3 | phase 1 |
| health | `GET /health` (liveness, v1 shape), `GET /ready` (new) | none | native | S3, S3b | phase 1 |
| registration | `POST /nodes`, `GET /nodes`, `POST /message-principals` | none known | proxy, then shadow for `GET /nodes` | S5 | phase 3, last |
| heartbeat | `POST /events`, `GET /status`, `POST /sessions`, `GET /sessions` | none known on the GET routes | proxy, then shadow | S6 | phase 3, first |
| claims | `POST /claims`, `GET /claims`, `GET /owners` | none known on the GET routes | proxy, then shadow | S7 | phase 3, second |
| messages | `POST /messages`, every action except `events_*` (the `question_*` actions included, because `question_create` inserts a message) | `list`, `count` and `sent` prune expired messages first. Every `question_*` action expires pending questions | proxy, then shadow | S8 | phase 3, third |
| metrics | `GET /metrics` (new) | none | native | S9 | phase 1 |
| memory | `POST /memory` (new) | none | native only | S13a | phase 4 |
| findings | `POST /findings` (new) | not yet built | Python first, proxied | S22, then S12h | after 0.31.0 |
| cold JSON | `/preference`, `/audit`, `/mesh/*`, `/cloud`, `/models`, `/policy*`, `/grokbot`, `/skills/report`, `/mcp/report`, `/projects/board`, `/stats`, `/ci*`, `/actions`, `/work/*`, and the `events_*` message actions | per family | proxy | S12a to S12i, one family per PR | after 0.31.0 |
| browser | `/`, `/deck*`, `/view/*`, `/enroll`, `/dashboard/*`, static assets | none | proxy | not planned | never |

"None known" means review of main found no writes. S9's test checks every
shadowed route on a Python hub and lists any write it finds.

Shadow rules:

- In `shadow` mode the client gets Python's answer, and Python serves the
  request exactly as today, housekeeping writes included. After Python
  responds, Rust computes its own answer on a `query_only` reader and compares
  the two. Rust never writes in shadow mode, and a test fails if a shadowed
  Rust handler opens a write. Python's housekeeping commits before its
  response, so Rust's later read sees the same pruned rows.
- Comparison is field by field from a comparator file. Values read from the
  database compare exactly, fencing generations and stored expiry times
  included. Generated ids map to first-seen order. Values a handler computes
  from its own clock (a remaining lease time, a response timestamp) compare
  within the time between the two reads.
- On a mismatch, Rust checks `PRAGMA data_version` on a dedicated watch
  connection, sampled before it forwarded the request and after its own read.
  If no other connection committed in that window, the mismatch is
  **definite**. If one did, Rust retries both sides up to twice: a later match
  is **transient**, and repeated mismatches with commits in between are
  **inconclusive**. Pruning that Python did during the request also counts as
  a commit, which is rare because messages expire after the retention days.
- Byte-budgeted pages: when both answers stopped at a byte budget and one page
  is a prefix of the other, the comparator compares the common prefix and
  records a **boundary** result. The budgets are `JSON_RESPONSE_BUDGET_BYTES`
  (480 KiB) in `src/brigade/fleet_owners.py` for `GET /owners` and
  `MAX_LIST_RESPONSE_BYTES` in `src/brigade/fleet_hub_messages.py` for message
  pages.
- The comparator file and the drive's shape rules live in one data file that
  the drive and the Rust comparator both read, so the two cannot drift.
- A mismatch record holds the route, action, both status codes, the
  classification, the JSON path of the first difference and SHA-256 digests of
  both bodies. It never holds a body.
- Long-poll list requests (`wait_ms` above 0) are never shadowed.

### Process model

One process, one tokio multi-thread runtime for HTTP, and dedicated OS threads
for SQLite and CPU-heavy work, because rusqlite calls block.

| Component | Threads | Owns |
|---|---|---|
| HTTP | tokio workers (default: CPU count) | sockets, parsing, admission, encoding small JSON responses |
| Hub writer | 1 OS thread | the only Rust write connection to `fleet-hub.db`, and the `data_version` check for commits by other processes |
| Hub readers | N OS threads (default: CPU count, at most 8), queue of 1,024 jobs | one `query_only` connection each |
| Maintenance | 1 OS thread | its own connection for checkpoints, retention sweeps and vector cleanup, plus a watch connection that samples `data_version` for the shadow comparator |
| Memory writer | 1 OS thread | the write connection to `fleet-hub-memory.db`, and a hub connection used only to hold the hub write lock (D23) |
| Memory readers | M OS threads (default 2), queue of 256 jobs | read connections to the memory file |
| CPU pool | min(CPU count, 4) threads, queue of 256 jobs | lexical scoring, dense scans, index builds, encoding large responses |
| Embed scheduler | tokio task | embedding jobs and calls to the embedding server |
| Upstream proxy | tokio tasks | the HTTP client to the Python upstream |

Connections live for the life of the process. Each connection prepares its
statements once and keeps them in its statement cache, so no request pays for
opening a database or parsing the schema. On the reference hub allotment (1
vCPU, 512 MB) the defaults resolve to one HTTP worker, one hub reader and one
CPU-pool thread. S4b sizes the defaults from measured query time and resident
memory.

### Write path

Handlers never touch the write connection. A write becomes a command sent to
the writer over a bounded channel, with a one-shot reply channel and a
deadline.

```text
loop:
    command = receive()                          # wait for one command
    BEGIN IMMEDIATE                              # SQLITE_BUSY: nothing applied yet, back off and
                                                 # retry, at most 5 s in total (see Lock waits)
    batch = []
    while command and batch within budget:       # 256 commands, 4,096 events, 8 MiB of payload,
                                                 # 50 ms of apply time
        if command.deadline passed:
            reply 503 or 504 (durable), not applied; command = receive_without_waiting(); continue
        SAVEPOINT command
        result = command.apply(transaction)      # reads and writes, no I/O outside SQLite
        if connection is back in autocommit:     # SQLite aborted the whole transaction
            reply 503 "storage-error" to this command
            re-apply the earlier commands of this batch once, in a new transaction
            (a second abort fails all of them with 503)
        RELEASE or ROLLBACK TO command
        batch.append(command, result)
        command = receive_without_waiting()
    keep any unapplied command for the next batch
    COMMIT                                       # synchronous=FULL: one WAL sync for the batch
    on COMMIT error: reply 503 "commit-failed" to every command, reopen the connection,
                     run quick_check before taking new commands
    reply to every command with its result
    post_commit(batch)                           # bump wake epochs, wake waiters, metrics
```

- There is no linger timer. A batch is whatever queued while the previous
  commit ran, so a single client sees no added latency and a busy hub gets
  larger batches.
- A failing command rolls back to its own savepoint and returns its error. The
  rest of the batch commits. SQLite aborts the whole transaction, not one
  statement, on `SQLITE_FULL`, `SQLITE_IOERR`, `SQLITE_NOMEM`,
  `SQLITE_INTERRUPT` and some `SQLITE_BUSY` cases. The writer checks
  `sqlite3_get_autocommit()` after every command. Nothing from an aborted
  transaction committed, so re-applying the earlier commands once is safe. No
  command is ever answered 2xx unless its batch committed.
- A command does all of its reads inside the write transaction. Handlers that
  read and then decide (claim acquire, idempotent message send, fencing
  checks) are one command each, so they see a consistent state.
- Write commands re-check the caller's credential row inside the transaction,
  so a token revoked between authentication and the write cannot write. This
  works because credentials and these writes live in the same file. Memory
  writes use D23.
- Durable commands: claim acquire, renew and release, fence updates, node add
  and revoke, principal add, grant and revoke. The writer queue keeps 512 of
  its 4,096 slots for them, so event floods cannot crowd them out. A durable
  command is never shed with 503. If it cannot get a slot or its deadline
  passes, it gets `504 {"error": "hub busy", "reason": "writer-timeout"}`,
  which a v1 claim client handles like an unreachable hub.
- Side-table writes happen in the same command as the main row: the audit
  log, the message send ledger, and the MCP Events outbox rows that the Python
  hub writes when callback hosts are configured. The Python upstream keeps
  delivering webhooks from that outbox until the events family is ported.
- Triggers do part of this work, and Rust relies on them firing:
  `fleet_questions_reply` answers a pending question when the operator
  replies, three `fleet_audit_log_no_*` triggers keep the audit log
  append-only, and eleven `fleet_read_visibility_*` and `fleet_read_snapshot_*`
  triggers maintain the credential-scoped paging snapshots from #1939. The
  startup check verifies all 15.
- Lock waits: the Python upstream holds `BEGIN IMMEDIATE` across its own work
  (message pruning, claim retirement budgets, Grok Bot queue operations, the
  events dispatch thread). `SQLITE_BUSY` at `BEGIN` means nothing was applied,
  so the writer retries the same batch with backoff for at most 5 s, then
  answers 503 for non-durable commands and 504 for durable ones. Lock wait
  time is a `/metrics` histogram, and S6 measures it with claims in the mix.
- Graceful shutdown stops accepting, wakes every long-poll waiter with an
  empty page, drains the writer queue for at most 10 s, answers the rest with
  503, and runs a final passive checkpoint.

### Durability

- Every write connection, hub and memory, sets `PRAGMA synchronous=FULL` when
  it opens and checks that it reads back `2`. SQLite refuses to change the
  level inside a transaction, and nothing changes it per batch.
- In WAL mode, `FULL` syncs the WAL at every commit, so an acknowledged commit
  survives an OS crash or a power cut. A process crash alone loses nothing at
  any level, which is why a `kill -9` test proves integrity and says nothing
  about durability.
- Ids stay unique across crashes. `fleet_messages.id` and `memory_events.seq`
  use `AUTOINCREMENT`, and under `FULL` an acknowledged id cannot roll back
  and be handed out again, so a recipient's `after` cursor never skips a
  message. A restore from backup is the one way ids repeat. The memory log
  answers that with `log_epoch`. For the hub file, the runbook says a restore
  rewinds message ids, as it does today.
- Tests (S4a):
  1. Integrity: 20 `kill -9` runs under load leave `integrity_check` ok and
     lose no acknowledged write.
  2. Power cut: a fault-injecting VFS shim holds every write since the file's
     last sync. At a random point after a commit returns, it drops the unsynced
     bytes, whole or as a torn prefix. This is the technique SQLite's own crash
     tests use. An external ledger records each acknowledged command, and after
     reopening every acknowledged command must be present and
     `integrity_check` must pass, across 500 cut points. The same test run at
     `synchronous=NORMAL` must report lost commits, which shows the test can see
     the failure it guards against.
  3. A manual VM reset procedure in the runbook, for operators who want a
     hardware check. It is not a CI gate.
- Cost: FULL costs one WAL sync per batch, and batches grow with load. A model
  of this writer loop on a shared machine under heavy I/O load reached 757 to
  1,227 commands/s at FULL against 1,084 to 4,870 at NORMAL, with mean batches
  of 21 to 88 commands at 64 to 256 clients ([Evidence](#durability-cost-with-group-commit)).
  Those numbers come from a loaded machine and a Python model, so they show
  that FULL throughput scales with batch size and say little about the hub's
  own storage. S6 measures the real writer on quiet hardware at the hub
  allotment. If FULL misses the release target there, the remedy is a
  maintainer decision about named low-value families. The default stays `FULL`.

### Read path

A read handler queues a job for a reader (at most 2 s in a queue of 1,024),
runs its queries inside one `BEGIN DEFERRED` transaction so a multi-query view
(the status board, the owners view) is one snapshot, and returns rows to the
tokio side for JSON encoding. A read transaction is capped at 2 s: a progress
handler interrupts it and the request gets 503. A queue timeout answers
`503 {"error": "hub busy", "reason": "reader-queue-full"}`, the same
`{error, reason}` shape the Python hub uses for its existing 503 answers.

Python reads pick up enrollments and revocations by opening a new connection
per request. The Rust readers see them because every request starts a new
read transaction, which sees all committed data. There is no token cache and
no result cache anywhere in the hub.

### Admission and backpressure

| Limit | Rust default | High-capacity profile | Python today |
|---|---|---|---|
| Open connections | 4,096 | 12,288 | 32 handler threads |
| Connections kept for requests that do not wait | 1,024 | 2,288 | none |
| Connections per source address | 256 | 256 | 8 |
| Listen backlog | 1,024 | 4,096 | 5 |
| In-flight requests per credential | 64, at most 16 of them on `/memory` | same | none |
| In-flight request body bytes, all connections | 64 MiB | 256 MiB | none |
| Writer queue | 4,096 commands or 64 MiB, 512 commands kept for durable commands | same | none (5 s busy timeout per request) |
| Reader queue | 1,024 jobs, 2 s wait | same | none |
| CPU pool queue | 256 jobs | same | none |
| Long-poll waiters, total | 3,072 | 10,000 | 8 |
| Long-poll waiters per principal | 2 | 2 | 2 |
| Long-poll waiters per non-loopback source | 64 | 256 | 2 |
| `wait_ms` maximum | 20,000 | same | 20,000 |
| Body size | 8 MiB plus the JSON bounds | same | same |
| Header read timeout | 10 s | same | 30 s socket timeout |
| Body read timeout | 30 s | same | 30 s socket timeout |
| Open-file limit the profile needs | 4,608 | 12,800 | none |

- Over the connection or per-source cap, the connection is closed without an
  HTTP response. That is the v1 behavior, and clients already treat it as an
  unreachable hub (events go to the local spool).
- A `list` with `wait_ms` above 0 becomes a waiter only while open connections
  stay below the total minus the kept connections, and under every waiter cap.
  Otherwise it gets the v1 error with `code: "message_wait_capped"`, the same
  answer Python gives at its own caps. Idle polls can therefore never take the
  connections that sends and control requests need.
- A full non-durable writer queue, a reader timeout, a full CPU queue, a body
  over the in-flight byte budget, or a credential over its in-flight cap
  answers `503 {"error": "hub busy", "reason": ...}` with `Retry-After`.
  Durable commands follow the [Write path](#write-path) rule instead. Event
  clients already keep a batch in the spool on any 5xx.
- Source caps use the TCP peer address, never a header, as Python does.
- The per-principal waiter cap of 2 stays, because clients can see it: the
  third waiter gets the v1 error with `code: "message_wait_capped"`.
- The JSON bounds match `fleet_hub_event_bounds`: at most 32,768 containers,
  131,072 aggregate items and 1,000 items per container. A body with
  `Transfer-Encoding`, or without exactly one `Content-Length`, is refused as
  today.
- At startup the binary raises its soft open-file limit to the hard limit and
  refuses to start if the result is below the profile's need. The runbook also
  lists `net.core.somaxconn`, because the kernel silently caps the backlog.
- Defaults fit the reference allotment. The high-capacity profile is opt-in
  (`--profile high-capacity`), and S8 measures both.

### Long-poll

`POST /messages` `list` with `wait_ms` follows the same protocol as the Python
hub's `_wake_epoch` loop, without a thread per waiter:

1. Authenticate, register a waiter for the recipient (it counts toward the
   caps), and record the recipient's current wake epoch.
2. Read. If there are rows, return them.
3. Wait until the recipient's epoch changes or the deadline passes.
4. On wake, re-check the principal. A principal revoked or changed while
   waiting gets the v1 auth error and no rows. Then read again. If there are
   rows, return them. Otherwise record the new epoch and go back to step 3.

Registering before the first read closes the race where a send commits
between the read and the wait: that send bumps the epoch, so the waiter
re-reads. A woken waiter holds no transaction while it waits.

Wake sources:

- Native commits: `post_commit` bumps the epoch of every recipient the batch
  touched. Woken waiters re-read in groups, up to 256 recipients per query, so
  a batch that wakes thousands of waiters does not flood the reader queue.
- Commits by other processes: only the messages module inserts into
  `fleet_messages`, and S8 ports the question actions with it, so after S8 no
  proxied route adds a message. As a safety net, while any waiter exists the
  writer checks `PRAGMA data_version` on its own connection between batches
  and every 250 ms when idle. On the writer's connection that value changes
  only when another connection commits, so Rust's own commits never trigger
  it. On a change, one query finds recipients with message ids above the last
  id seen, and only their epochs are bumped.
- Targets: a native send wakes its waiter within 50 ms at p99, and a message
  committed by another process within 400 ms at p99.

Expiry on native reads: Python's `list`, `count` and `sent` delete expired
messages and clear `reply_to` links to them before reading, and the question
actions expire pending questions. Native Rust reads do not write. They filter
expired rows with the same expiry predicate and show a `reply_to` that points
at an expired message as null, and the maintenance thread purges expired rows
every 60 s and before any storage-parity dump. S1b's drive cases pin the
visible result, so a client cannot tell the two apart. If a case cannot be
matched without a write, that action stays a writer command.

The idle cost measured 15 to 18 KB of task memory per connection on the same
runtime, where the threaded Python server failed past about 8,400 threads. S8
measures resident memory and kernel socket memory separately, since task
memory leaves both out.

### Storage interface

```text
trait HubStore {
    read(view)        -> result   // reader pool, one read transaction
    write(command)    -> result   // writer, one savepoint inside a batch
}
```

Each domain module (registration, heartbeat, claims, messages, findings)
defines its commands and views against two small transaction traits,
`ReadTx` and `WriteTx`. SQL text lives per backend under
`engines/hub/sql/sqlite/`. The correctness rule for every command is that it
must be correct under serializable isolation and must not rely on running
alone. SQLite gets that from the single writer. A future Postgres backend
would run serializable transactions with retry on serialization failures, one
transaction per command, and would skip group commit. Memory has its own
store with the same traits. The memory index is not part of the store: the
store persists the log, revisions, blobs and vectors, and the index is rebuilt
from them.

### Schema, migrations and data compatibility

Hub database (`fleet-hub.db`):

- The Python hub at this design's base is at `user_version` 47 with 75 user
  tables (76 rows of type `table` counting `sqlite_sequence`), 146 indexes and
  15 triggers.
- During the rollback window Python is the schema authority (D6). Startup of
  the planned `fleet serve --engine rust` runs the existing Python `init_db`
  before starting the Rust binary, as `fleet serve` does today.
- `init_db` runs DDL at every start, even at the same `user_version`: additive
  `ALTER TABLE` columns and each module's `ensure_schema`. Rust therefore
  re-runs its structural check when a statement fails with `SQLITE_SCHEMA` and
  after every upstream restart, and refuses writes with
  `503 {"error": "hub busy", "reason": "schema-changed"}` until the check
  passes.
- The Rust binary is built for one `user_version`. At startup it refuses to
  serve unless the file's `user_version` equals that number and the structure
  it relies on is present: tables, columns with type, `NOT NULL` and default
  (`PRAGMA table_xinfo`), indexes with their columns and uniqueness
  (`index_list`, `index_xinfo`), foreign keys (`foreign_key_list`), and CHECK
  constraints and triggers, compared by name and by their SQL from
  `sqlite_schema` with whitespace collapsed. It is a structural check, not a
  hash of the whole schema, because a database migrated with `ALTER TABLE`
  stores different table SQL than a fresh one.
- `engines/hub/schema/required.json` lists that structure. A Python lockstep
  test builds a fresh hub database and upgrades checked-in fixtures from each
  `user_version` a supported release shipped, and fails when either no longer
  matches. Python's SQLite and Rust's bundled SQLite both open every fixture
  in that test. Any Python schema change, an added column included, forces a
  reviewed Rust update in the same PR.
- Tables a new hub feature needs on this file (the findings ledger, work
  edges) are added through the Python schema code, so Python can read them
  after a rollback.
- After the rollback window, schema authority moves to Rust with forward-only
  numbered migrations that start at the `user_version` Python reached, plus
  one. From then on a rollback to Python needs a restored backup, and the
  runbook says so.

Memory database (`fleet-hub-memory.db`, next to the hub file):

- Owned by Rust from the start (D7), with its own `user_version` series and
  forward-only migrations embedded in the binary.
- `memory_meta` holds `log_epoch`, a random value written when the file is
  created and replaced whenever the file is restored from a backup. Every
  `log`, `status` and write response carries it.
- The same file rules as the hub database: a 0700 directory and 0600 files,
  regular files only, no symlinks, as `_secure_hub_database_storage` enforces.
- The Python hub never opens it. A rollback leaves it untouched, and memory
  routes on the Python hub answer 404 until Rust returns.

Rows written by one hub must read correctly in the other. The drive checks the
wire, and a storage parity drive (S2) checks the rows. It runs one scripted
request sequence and dumps every table, including the trigger-maintained ones
(`fleet_questions` and the read-visibility and snapshot tables). Per column:

- Values that do not depend on the clock or on randomness compare exactly.
- Generated ids map to first-seen order, and the mapping is applied to every
  column that references them, so a wrong relationship shows up as a
  difference.
- Timestamps compare by format and by their relations: an expiry equals its
  creation time plus the configured retention, and a lease renewal is later
  than its acquisition.
- Fencing generations and expiry offsets compare exactly.
- Digests are recomputed from the stored canonical bytes and compared with
  the stored digest.

S2 runs Python alone (the golden), Rust alone, and mixed sequences where the
engines alternate on one file mid-sequence, so Python reads Rust rows and Rust
reads Python rows. The sequence also runs on a database upgraded from an older
fixture. Mutation tests change a value without changing its format (a
generation off by one, an expiry one second late, a wrong digest, a broken id
relationship) and must be caught. S2 runs once with default flags, as the
contract doc requires of a hub under test, and once more with the events
outbox enabled (`BRIGADE_FLEET_EVENTS_CALLBACK_HOSTS`).

| Item | Python behavior | Rust rule |
|---|---|---|
| Timestamps in text columns | `datetime.isoformat()` on aware UTC: `2026-11-03T12:34:56.123456+00:00`, with the fraction omitted when microseconds are 0. Some columns hold epoch floats. | Same bytes per column. SQL compares these as strings, so a different format reorders rows. |
| JSON in text columns | `json.dumps` with each call site's `sort_keys` and separators, and `ensure_ascii=True` | A Python-compatible encoder: same separators, `\uXXXX` escapes for non-ASCII, Python float formatting (`1e-05`, not `1e-5`) |
| Canonical JSON for digests and sealed envelopes | `sort_keys`, separators `(",", ":")`, `ensure_ascii`, `allow_nan=False` | Same, checked against golden vectors produced by Python |
| Token digests | SHA-256 hex of the token | Same |
| Request JSON | `json.loads` accepts `NaN` and `Infinity` literals. Duplicate keys are rejected on some routes and keep the last value on others. Integers are unbounded, then range-checked. | Match per route. A differential fuzz run (S8) sends the same generated bodies to both hubs and requires the same status and error text. |
| Byte-budgeted responses | `GET /owners` fills its payload against `JSON_RESPONSE_BUDGET_BYTES` (480 KiB, `fleet_owners.py`), message pages against `MAX_LIST_RESPONSE_BYTES`, each measured with Python's encoder | Budget measured with the same encoder settings the response uses, with golden size tests |
| Error text | Strings clients may match, some formatted inline | Every refusal path's exact text, captured from the Python hub by drive cases (S1b), not derived from constants |

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
  routes. On proxied routes Rust leaves auth to Python (see the proxy
  contract), so the quirks there come from Python itself.
- Dashboard sessions, enrollment codes and cookies stay with the proxied
  browser routes. Rust sets no cookies.
- `Tailscale-User-Login` is removed from every incoming request unless the
  Rust hub runs with `--trust-tailscale-identity` and a loopback `--host`,
  the same rule the Python hub applies. When trusted, the header is forwarded
  to the upstream for browser routes only. `--trust-forwarded-proto` follows
  the same rule.
- The upstream runs in a planned upstream mode (S10): loopback bind, no
  per-source cap (Rust enforces it on real peer addresses), the client address
  taken from a header that only the Rust front can set, keyed by a random
  value generated at each start, and `wal_autocheckpoint=0` on every
  connection it opens. Audit rows and waiter caps then keep real addresses.

### Upstream proxy contract

The first draft named a pooled client and nothing else. These rules belong to
S3b, or to S3's acceptance if S3 has not merged when this revision lands.

- Auth ownership: native routes use Rust's conventions layer. On proxied
  routes Rust applies only transport checks (admission, framing, body size,
  the query-token refusal the drive pins for every route) and forwards the
  request. Python produces every auth answer on those routes, so `/policy` and
  `/audit` keep their v1 differences.
- Bodies are forwarded byte for byte. For `POST /messages`, Rust reads
  `action` with a bounded parser and forwards the original bytes. A body Rust
  cannot parse goes to Python, which produces its v1 error. Rust never
  re-serializes a proxied body, so duplicate keys, `NaN` literals, large
  integers and sealed envelopes reach Python unchanged.
- Headers: hop-by-hop headers are removed (`Connection` and the headers it
  names, `Keep-Alive`, `TE`, `Trailer`, `Upgrade`, `Proxy-*`). Incoming copies
  of the internal client-address header, and of `Tailscale-User-Login` unless
  trusted, are dropped. `Authorization`, `Cookie` and `Content-Type` pass
  unchanged. Every `Set-Cookie` comes back as its own header. Status and body
  bytes come back unchanged. Redirects are not followed.
- Responses stream with backpressure. The proxy adds no response size cap of
  its own, since Python's budgets already apply.
- Timeouts: connect 1 s, response headers 30 s (for a proxied `list` with
  `wait_ms`, that wait plus 5 s), idle body 30 s, total 120 s.
- Retries: none for any request that may write, which is every request with a
  body. A `GET` is retried once only when the connection attempt itself
  failed, because nothing was sent.
- Failure answers (D26): upstream down or connect failure answers
  `502 {"error": "upstream unavailable", "reason": "upstream-down"}`. A header
  or body timeout answers `504 {"error": "upstream timeout", "reason": ...}`.
  A reset after the request was sent answers
  `502 {"error": "upstream unavailable", "reason": "upstream-outcome-unknown"}`
  with no retry, because Python may have committed. Browser routes get a
  short HTML 502 or 504 page. None of these is 503: the v1 claim client turns
  any HTTP error other than 400, 401, 403, 409 and 503 into
  `reason: "hub-unavailable"`, which is what it does today when the hub is
  down.
- Supervision: the planned `fleet serve --engine rust` runs a small Python
  supervisor with two children, the upstream server and the Rust hub. If the
  upstream exits, the supervisor restarts it with backoff (1 s, doubling to
  30 s) while Rust keeps serving native families. Proxied routes answer 502
  meanwhile, and webhook delivery pauses with its outbox rows kept. If Rust
  exits, the supervisor stops the upstream and exits non-zero so the service
  manager restarts the unit. Signals are forwarded, and shutdown drains Rust
  first.
- Readiness: `/health` keeps its v1 liveness meaning and shape. The new
  `GET /ready` is unauthenticated and answers `200 {"ready": true}` or
  `503 {"ready": false}` with no detail. The hub is ready when the writer, the
  readers and the schema check are healthy and the upstream passed its own
  health check within the last 5 s. Per-component state, including
  `upstream_up` and `webhook_delivery_paused`, is in `/metrics`.
- Tests: upstream down, reset after commit (502 outcome unknown, no retry,
  and Python's commit visible afterwards), a slow request body, cookie login
  with two `Set-Cookie` headers, `Transfer-Encoding` and duplicate
  `Content-Length` refusal, spoofed forwarding headers stripped, the upstream
  crashing while native writes continue, and `/ready` turning 503 and back.

### Sealed messages

The hub does no cryptography (#2005). For sealed mail it validates structure
and stores opaque bytes. Rust must reproduce the Python checks exactly: the
canonical form, the routing-header field set, duplicate-key rejection inside
envelopes, the digest fields (`ct_sha256`, `wraps_sha256`, `rc`), padding,
`--require-sealed` and `BRIGADE_FLEET_MESSAGES_REQUIRE_SEALED`, the storage
policy and retention, the key directory (`keys_publish`, `keys_get`) and
manifests. Envelopes are stored and returned byte for byte. The drive has no
sealed cases yet, so S1 adds them against the Python hub before S8 ports the
code. Building valid envelopes needs client crypto, and the drive may not
import `brigade.*`, so the cases use golden envelopes generated once by Python
and checked in with their matching published keys. The `/mesh/identity` and
`/mesh/statements` routes stay proxied until the mesh family is ported.

### Observability

- `GET /metrics` (new, admin token only) serves the standard metrics text
  exposition format. Labels are route family, action, status class and
  reason, never a principal name, claim target or card id.
- Counters and histograms:
  - requests: latency by family
  - writer: batch size, apply time, commit time, lock wait, queue depth by
    lane, aborted batches
  - readers and CPU pool: queue wait and depth
  - checkpoints: each result as busy flag, WAL frames and frames
    checkpointed, plus WAL size
  - long-poll: active waiters, wakes by source (native or external)
  - admission: refusals by reason
  - upstream: latency, errors by reason, `upstream_up`,
    `webhook_delivery_paused`
  - shadow: comparisons by family and result (match, definite, transient,
    inconclusive, boundary)
  - memory: documents, terms, vectors per embedder, `indexed_seq` lag,
    embedding queue depth, embedding failures by code, scan availability,
    authority lock wait, and query time split into lexical, embedding, scan
    and fusion
- One structured log line per request: request id, family, status, latency,
  credential kind (admin, node or principal) and byte counts. Logs never hold
  bodies, tokens, message subjects, card text or query text.
- `GET /health` keeps its v1 shape. Readiness is `GET /ready`.

### Checkpoints and disk

- The maintenance thread owns checkpoints on its own connection, never the
  writer. It runs a passive checkpoint when the hub has been idle for 1 s or
  the WAL passes 64 MiB, and a truncating checkpoint above 256 MiB with a busy
  handler bounded at 250 ms. A busy result is retried later and reported.
- Every Rust connection sets `wal_autocheckpoint=0`. Python's connections keep
  SQLite's default of 1,000 pages unless the upstream runs in upstream mode,
  which turns it off on every connection it opens, the events dispatch thread
  included. Until upstream mode ships (S10), Rust's policy covers only Rust's
  own commits.
- Read transactions are capped at 2 s and waiters hold no transaction, so no
  Rust reader pins the WAL for long. A Python reader still can, and the
  checkpoint metrics show it.
- Disk pressure: when the WAL passes 1 GiB, or free space on the database
  volume falls below 1 GiB, non-durable writes get
  `503 {"error": "hub busy", "reason": "disk-pressure"}`. Durable writes go on
  until SQLite reports `SQLITE_FULL`. Reads continue.
- Backups use the SQLite online backup API or `VACUUM INTO`, never a copy of
  the main file alone, because committed rows can sit in the WAL.
- Tests (S4b): a pinned reader that keeps a truncating checkpoint busy without
  stalling the writer, continuous Python writes, `SQLITE_FULL` injected through
  the VFS shim, and a rollback to Python with live `-wal` and `-shm` files.

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
family=native|shadow|proxy` (repeatable), `--profile default|high-capacity`,
`--memory-db`, `--reader-threads`, `--max-connections`,
`--reserved-connections`, `--max-waiters`, `--writer-queue`,
`--writer-queue-bytes`, and `--config` for an optional `hub.toml` holding the
memory and embedder settings. Precedence is flag, then environment, then file,
then profile default.

### Packaging and distribution

- Source: `engines/hub/`, package and binary `brigade-hub`, the same Rust
  edition and minimum version as the code-graph engine (edition 2024, Rust
  1.85). Dependencies: tokio, axum, hyper, hyper-util (the upstream and
  embedding client), rustls with platform root certificates for HTTPS
  embedding endpoints, rusqlite with bundled SQLite, serde, serde_json, toml,
  sha2, hmac, subtle, tracing, clap, and the vendored PCRE2 engine crate. No
  OpenSSL. The hub serves plain HTTP behind the operator's proxy, as the
  Python hub does. Advisory and license checks run in CI on the lockfile.
- Build: `.github/workflows/publish.yml` builds five Rust targets today, but
  its job and binary list are specific to `engines/code-graph`. S10 adds the
  hub crate to that job (or a sibling job), stages and attests `brigade-hub`,
  adds the component manifest entry, updates the manifest validators' complete
  set, and extends `setup` install acceptance. Pull requests that touch
  `engines/hub/` build all five targets. Both the minimum Rust version and
  current stable must build.
- Before a release tag, a pre-publication job installs each target's artifact
  and runs startup, the contract drive, an upgrade from an older schema
  fixture, an interrupted migration, backup and restore through the online
  backup API, and the unknown `user_version` refusal, so platform defects
  show up before anything is published.
- Naming follows the engine rename plan (#2213): canonical `brigade-*` names
  and no legacy alias for the hub. If the rename's manifest and installer
  slice (R5) lands first, S10 rebases on it.
- PCRE2 UTF support is reported as a capability in `brigade-hub --version
  --json`. A binary without it keeps memory trust raises and pushes closed
  (D24).
- Install: hub hosts opt in with the planned `setup --component brigade-hub`.
  Other hosts never download it.
- Run: the planned `fleet serve --engine rust` keeps every existing flag. It
  runs `init_db`, verifies the installed binary against the manifest
  checksum, and starts the supervisor described in the proxy contract.
  Rollback is removing `--engine rust`.
- Container: `ops/fleet-hub/Containerfile` adds the verified binary and uses
  `fleet serve --engine rust`. Python stays in the image for the upstream.

### Cutover

| Phase | What changes | Gate to move on |
|---|---|---|
| 0 | Python hub on the public port (today). Baseline: the S29 read-only probe every 60 s for 48 hours, plus the events spool depth nodes report | Release slices merged. Contract drive, storage parity and the S4a power-cut test pass against Rust in CI |
| 1. Front | Rust takes the public port with the upstream in upstream mode, every family in `proxy` mode | 48 hours with probe p95 latency within 10% of the phase 0 baseline, no more probe errors than phase 0, and `/ready` green |
| 2. Mirror reads | Heartbeat, claims and messages reads, and `GET /nodes`, in `shadow` | 7 days, at least 1,000 comparisons in each family, zero definite mismatches, and every inconclusive or boundary record reviewed |
| 3. Flip writes | Heartbeat to `native`, then claims, then messages, then registration, one family at a time | Each family runs 48 hours with no new errors and writer queue depth under 10% of its cap. The S29 profile passes hourly against production, and the full drive passes against a copy of production taken with the online backup API and served on a throwaway loopback port |
| 4. Memory | Memory routes enabled. Clients turned on per workspace | R0 passes on the production corpus snapshot, and the S18 scanner reports available |
| 5. Cold families | Ported one family per PR after 0.31.0 (S12a to S12i) | Same as phase 3 for each family |

The full drive enrolls nodes and principals, sends and acks messages, and
takes claims, so it never runs against production. Production checks use the
S29 read-only profile from the hub host itself against the loopback port,
because the drive refuses non-loopback URLs.

## Memory v2

### Model

Memory is an append-only event log on the hub. A card is identified by
`(scope, card_id)` (D22), so two repositories can each have a card named
`build`. Every change to a card's head appends an event and gives the card a
new `revision`, equal to that event's `seq`: new text, retirement, revival,
and a trust change from a decision. The content hash names bytes only. Blobs,
vectors and deduplication use it, and two revisions can share one hash. The
current head of each card is updated in the same transaction as the event.
The index and the vectors are derived after the commit and can be rebuilt from
the store at any time. Any principal with a write grant on a scope can append
events. Nothing hosts the cards on behalf of others. Curation is a policy in
the fleet policy document, which says who may promote what, at which tier,
under which gates.

Concurrent writers use compare-and-set on the revision. A `put` or `retire`
names the `base_revision` it edited, or `null` to create a card. If the head
has moved, the hub refuses with `409` and `code: "memory_base_mismatch"`, plus
`current_revision` and `current_hash`, and the writer re-reads. The revision
changes on every head change, so three cases that a hash-only check misses are
caught: bytes that go from A to B and back to A, a trust change on identical
bytes, and a retire followed by a revival. An offline client whose edit conflicts turns
it into a proposal for review and never overwrites.

Scope is part of identity, so there is no scope move. Moving a card is a
retire in one scope and a create in the other, each authorized against its own
scope, and linked by `supersede_card` when it goes through a proposal. A write
into a scope the caller cannot write fails with 403 even when the same
`card_id` exists in a scope the caller can write.

### Authority across the two files

Credentials and the fleet policy live in `fleet-hub.db`. Memory lives in
`fleet-hub-memory.db`. No SQLite transaction spans both files atomically, so
the first draft's promise that a memory write re-checks the credential "inside
the transaction" could not hold. D23 replaces it with this protocol for every
memory write batch:

1. The memory writer takes the hub database write lock: `BEGIN IMMEDIATE` on
   its own hub connection, waiting at most 2 s.
2. Inside that lock it re-reads each command's credential row, and for
   decisions and policy applies, the current policy revision and digest.
3. It applies the batch in one memory transaction and commits with `FULL`.
4. It ends the hub transaction with `ROLLBACK`. It never writes to the hub
   file.

While step 3 runs, no hub write can commit, from Rust or from Python. A
revocation or policy edit therefore either committed before step 2 and is seen
there, or commits after step 4 and applies to later batches. Lock order is
always hub, then memory. The hub writer never touches the memory file and
Python never opens it, so the order cannot deadlock. If the hub lock is not
free within 2 s, every command in the batch gets
`503 {"error": "hub busy", "reason": "authority-lock-timeout"}` and nothing
was applied.

The cost is that hub writes wait for one memory commit, about one sync, per
memory batch. Memory write rates are low next to heartbeat traffic, and S13a
records hub p99 with memory writes running. On a crash at any step the hub
file is untouched, because the hub transaction never writes, and the memory
file is at its last commit. No cross-file atomicity is claimed or needed.
S13a tests a revocation and a policy edit at every step boundary through test
hooks.

### Trust, grants and scan state

D24 makes trust a rule of the hub, not something a writer can ask for:

- `put` stores the version as `agent-unreviewed`, or `flagged` when the scan
  warns, for every caller, the admin token included. The scan decides whether
  the write is blocked, flagged or stored. It never raises trust.
- `reviewed` comes only from `decide`, by the admin token or a principal with
  the `review` grant.
- `policy-promoted` comes only from the hub's own Tier 0 apply (S19c, enabled
  by S19e).
- `imported` comes only from the `import` action, by the admin token or a
  principal with the `import` grant.
- Every trust raise requires a clean scan of that content hash under the
  current rule set. If the scanner is unavailable, the raise is refused with
  `503 {"error": "scan unavailable", "code": "memory_scan_unavailable"}`.
- Only `reviewed`, `policy-promoted` and `imported` versions are pushed to
  principals other than the writer. That was Q4 in the first draft, and both
  reviews agreed, so it is now part of the design.

Grants are per principal and scope: `read`, `write`, `handoff`, `embed`,
`reflect`, `review` and `import`. Defaults: an enrolled node token reads and
writes the `fleet` scope and the `repo:` scopes, because nodes are operator
machines. A message principal (a cloud agent) has no memory grant until the
admin adds one with the `grant` action, and the S23 and S26 runbooks add the
grants Dot, Duet and Grok Bot need. The admin token holds every grant.

### Event schema

`brigade.memory_event.v1`:

| Field | Type | Notes |
|---|---|---|
| `seq` | integer | hub-assigned log offset, `AUTOINCREMENT`, strictly increasing, never reused |
| `event_id` | text | hub-assigned |
| `client_event_id` | text | required on every write action. See [Idempotency](#idempotency) |
| `kind` | enum | `card.put`, `card.retire`, `card.trust`, `card.import`, `evidence.attach`, `handoff.submit`, `proposal.open`, `proposal.decide`, `proposal.apply`, `proposal.rollback`, `grant.change`, `recall.capture` |
| `principal` | text | taken from the credential, never from the body |
| `scope` | text | `fleet`, `repo:<repo_identity>`, or `principal:<name>` (visible to that principal and the admin only). Part of card identity |
| `card_id` | text | `[a-z0-9][a-z0-9._-]{0,127}`, from the card's front matter `id` or its file stem |
| `revision` | integer or null | the card's new revision (equal to `seq`) for events that change a head |
| `base_revision` | integer or null | compare-and-set precondition. Null creates a card. A revival names the retirement's revision |
| `content_hash` | text | SHA-256 hex of the normalized card bytes. The bytes live in `memory_blobs` |
| `trust` | enum | the head's trust after this event: `reviewed`, `policy-promoted`, `imported`, `agent-unreviewed`, `flagged` |
| `origin` | enum | `interactive`, `scheduled` or `worker`. A ranking hint from the client, capped by the credential's registered class. Never used for authorization or trust |
| `payload` | object | hashes and operation metadata only, never card bytes. For proposals: ops, namespaced evidence references, pinned block hashes and base revisions |
| `policy_digest` | text or null | the curation policy revision a decision was made under |
| `created_at` | text | hub clock, Python isoformat |

Normalization for `content_hash`: UTF-8, LF line endings, exactly one
trailing newline. Front matter is kept as written. A change to this recipe
bumps `normalizer_version`, which is part of every derived key.

### Idempotency

`memory_idempotency` is keyed by `(principal, client_event_id)` and holds the
SHA-256 of the canonical request and the exact response. It commits in the
same transaction as the operation. The order of checks:

1. Authenticate.
2. Look up the key. Same request digest: return the stored response, even if
   the card has changed since. Different digest: `409` with
   `code: "memory_client_event_conflict"`.
3. Only then run compare-and-set and apply.

Checking the key before compare-and-set means a retry of a committed write
gets its original answer instead of a base mismatch. Records are kept 30 days,
configurable but never below the client spool's maximum entry age, which is
also 30 days. The spool never flushes an entry older than that. It turns the
entry into a local proposal. A replay after the record expired is a new
request, and compare-and-set refuses it if the original moved the card, so it
cannot apply twice. S13a tests a retry after a later edit, a crash after
commit followed by a retry, a mismatched payload, and duplicate delivery after
retention.

The messages `client_id` rule has its own replay window in the Python hub. S1b
pins that window with a drive case, and Rust matches it.

### Routes

One route, `POST /memory`, with an `action` field, the same style as
`/messages` and `/claims`. Authentication failures keep the v1 conventions
(`401 {"error": "unauthorized"}`). Every other 4xx carries
`{error, code}`. A missing grant is `403` with `code: "memory_grant_missing"`.

| Action | Grant | Request | Success |
|---|---|---|---|
| `search` | read | `query` (at most 256 characters after normalization), `limit` (1 to 20, default 10), `mode` (`auto`, `lexical`, `hybrid`), `query_source` (`prompt`, `cwd`, `explicit`), optional `scopes`, `sources` and `min_seq` | `200 {matches, retrieval: {mode, degraded, stale}, indexed_seq}` |
| `card` | read | `card_id`, optional `scope`, `revision` and `offset` | `200 {card: {scope, card_id, revision, content_hash, text, next_offset, trust, seq}}`, text in 30,000-character chunks as Dot's `memory_card` returns it. Without `scope`, an id readable in two scopes answers `409 memory_card_ambiguous` with the scopes |
| `put` | write on the scope | `scope`, `card_id`, `base_revision`, `text`, `origin`, `client_event_id` | `201 {event: {seq, revision, content_hash, trust}, log_epoch}` |
| `retire` | write on the scope | `scope`, `card_id`, `base_revision`, `reason`, `client_event_id` | `200 {event, log_epoch}` |
| `import` | import | as `put`, one card per call | `201 {event, log_epoch}`, trust `imported` |
| `log` | read | `after_seq`, `limit` (1 to 500) | `200 {events, next_seq, log_epoch}`, ascending `seq`, `next_seq` equal to the last `seq` on the page, `after_seq` exclusive, card text omitted |
| `handoff` | handoff | `text`, `kind`, `client_event_id` | `201 {handoff_id}` |
| `propose`, `decide`, `rollback` | reflect, review | proposal v2 body | `201` or `200` with the proposal state and its dry-run diff |
| `preflight` | read | `proposal_id` | `200` with the `brigade.memory-tier0-preflight.v1` report (S19c). Writes nothing |
| `capture` | read | `session_ref` (a client-side HMAC), `pushed`, `opened`, optional `outcome` | `200 {recorded: true}` |
| `embed_lease`, `embed_submit` | embed | `embedder_id` and `max`, or `lease_id` and base64 float32 little-endian `vectors` | `200` |
| `grant` | admin token | `principal`, `scope`, `rights` | `200 {grant}` |
| `status` | read | none | `200 {cards, revisions, seq, indexed_seq, log_epoch, scan: {available, ruleset_version}, embedders: [{id, coverage}], default_mode}` |

Error codes: `memory_query_invalid`, `memory_card_not_found` (404),
`memory_card_ambiguous` (409), `memory_base_mismatch` (409, with
`current_revision` and `current_hash`), `memory_client_event_conflict` (409),
`memory_card_too_large` (413), `memory_content_blocked` (422),
`memory_proposal_invalid` (422), `memory_lease_expired` (409),
`memory_vector_invalid` (422) and `memory_scan_unavailable` (503).

Availability: memory routes answer `404 {"error": "not found"}` only when
memory is not configured on this hub. A configured memory store that cannot
serve (file missing, migration failed, memory route forced to a mode that has
no handler) answers
`503 {"error": "hub busy", "reason": "memory-unavailable"}` (D21).

### Storage

Tables in `fleet-hub-memory.db`:

| Table | Key | Holds |
|---|---|---|
| `memory_events` | `seq` (`AUTOINCREMENT`) | the log, hashes and metadata only |
| `memory_blobs` | `content_hash` | card bytes, stored once per distinct content |
| `memory_cards` | `(scope, card_id)` | the head: `revision`, `content_hash`, state (`live` or `retired`), trust, writer class, title, summary, tags |
| `memory_card_revisions` | `(scope, card_id, revision)` | `content_hash`, trust, state, writer, `seq`, proposal id, scan state. Trust, retirement and decisions are versioned here, apart from content |
| `memory_scans` | `(content_hash, ruleset_version)` | `clean`, `flagged`, `blocked` or `pending`, and rule ids |
| `memory_vectors` | `(content_hash, embedder_id, normalizer_version)` | float32 little-endian vector, L2-normalized |
| `memory_lineage` | `(scope, card_id, revision, artifact, artifact_key)` | which derived rows came from which revision |
| `memory_embed_jobs` | `(content_hash, embedder_id)` | state, lease holder and expiry, attempts, last error code |
| `memory_proposals` | `proposal_id` | state, tier, ops, pinned block hashes, evidence references, base revisions, policy digest, dry-run diff, decision |
| `memory_evidence` | `(namespace, local_id)` | artifact digest, attesting credential, observation time, revocation state (S19e) |
| `memory_handoffs` | `handoff_id` | submitted handoffs waiting for review |
| `memory_grants` | `(principal, scope)` | rights |
| `memory_idempotency` | `(principal, client_event_id)` | request digest, response, time |
| `memory_recall_log` | `rowid` | time, principal, query HMAC, query source, returned card keys, mode |
| `memory_meta` | `key` | `log_epoch`, default embedder, normalizer version, index format version, scan rule set version |

Fingerprint memoization: the vector key is the content hash, the embedder id
and the normalizer version, with no card identity in it. Two cards with the
same text share one vector, an unchanged card is never re-embedded, and a new
embedder means a deterministic backfill of exactly the rows missing for its
id. Lineage lets `card.retire` and supersession remove exactly the derived
rows a revision produced, without replaying the transform, even after the
embedder has changed. A shared vector is deleted only when no live revision of
any card, in any scope, still references its content hash for that embedder.
The maintenance sweep collects vectors whose last reference is gone.

### Log retention and compaction

v2 ships no compaction of `memory_events`. Events hold hashes and metadata,
not card bytes, so the log stays small next to the blobs. Retention that does
ship, run by the maintenance sweep:

- `memory_recall_log`: rows older than 90 days are deleted, and the table is
  capped at 1,000,000 rows, oldest first.
- `memory_idempotency`: records older than the retention above (30 days).
- `memory_embed_jobs`: finished jobs older than 7 days.

Invariants for any future compaction of `memory_events`, recorded from #2210
and tested when it is built:

1. Writes that arrive during compaction survive. Compaction works below a
   watermark captured when it starts and never touches events above it. New
   events append normally and are present after the swap.
2. Replay order is verified before the swap. Replaying the compacted range
   plus the tail must reproduce every card head (revision, hash, state and
   trust) and the same `seq` order. Any difference aborts the swap.
3. Dry run is the default. The command prints what it would remove and the
   verification digest, and changes nothing without an explicit force flag.
4. Out-of-order partitions are rejected. Compacted ranges must be contiguous
   and strictly increasing in `seq`. A gap or an overlap aborts.
5. `seq` is never renumbered or reused. A compacted range leaves a marker, so
   a `log` call with an `after_seq` below the horizon answers `410` with
   `code: "memory_log_compacted"` and the `seq` to restart from, instead of
   skipping events silently.
6. Compaction never removes events younger than the idempotency retention, so
   a retry inside the window always finds its record.
7. `log_epoch` does not change on compaction. It changes only when the file is
   restored from a backup or replaced, and a client that sees a new value
   resets its watermark and pulls again.

### Index structures

The index lives in memory and is derived state: deleting it changes speed,
never results. It is not a result cache.

- Segments: an immutable main segment plus a chain of small immutable delta
  segments. A snapshot lists its segments and carries a copy-on-write table of
  document state (live bit, scope id, trust, revision, writer class) as
  parallel arrays. Readers hold one reference-counted snapshot for a whole
  query, so a query never sees a half-applied update.
- Lexical: per segment, a sorted term array, one offsets array, one
  contiguous postings array of document numbers and one of term frequencies.
  Per-scope term statistics are kept beside the segments.
- Dense: per embedder, an immutable main matrix plus append segments, rows by
  dimension, a row-to-document map and a has-vector bit. Nothing is appended in
  place, so older snapshots stay valid.
- Publication: after each commit the memory writer builds a delta segment and
  publishes a new snapshot atomically. `indexed_seq` is the last `seq` in the
  published snapshot. A write response returns its `seq`, and `search` with
  `min_seq` waits up to 500 ms for `indexed_seq` to reach it, then answers
  with `retrieval.stale: true` if it has not.
- Rebuild: when the deltas pass 10% of the main segment, a CPU-pool job builds
  a new main segment from a watermark it captures at the start, without
  blocking the writer. The writer then replays the tail after that watermark
  onto it and publishes. Matrices are rebuilt the same way.
- Stale hits: before a hit goes into any push path, the hub re-reads its head
  from `memory_cards` and drops it if it was retired, lowered in trust or is
  no longer readable by the caller. Search results carry `revision` and
  `indexed_seq`, so a caller can tell how fresh they are.
- Failure: if an index update fails after a commit, the snapshot is marked
  stale, `/metrics` shows the lag, and a background rebuild from the store
  starts. Queries keep using the last good snapshot and say `stale`. The store
  is the source of truth, and a restart rebuilds from `memory_cards`,
  `memory_blobs` and `memory_vectors`.

Size at 20,000 cards and 1,024 dimensions: about 80 MB of float32 vectors per
embedder plus postings in the tens of megabytes. During an embedder change
both matrices exist, and a rebuild briefly holds two copies of one. Inside a
512 MB allotment that is tight at 20,000 cards. Today's corpus (1,891 cards, a
7.7 MB matrix) is far below it. S13b and S15 record build time and peak
resident memory, retained snapshots included. Beyond about 100,000 cards the
matrix needs half precision or an approximate index, which is outside v2.

### Lexical scoring

R0 promises the same ids in the same order as the local `bm25` scorer, so the
hub copies its semantics from `src/brigade/memory_bm25.py` and
`memory_cmd.py` exactly:

- Tokenizer: lowercase, then the Python `\w+` pattern. Python's and Rust's
  Unicode word classes differ on some code points, so S13b adds a golden file
  generated by Python over the public fixture and a Unicode sample (case
  expansion such as `ß`, combining marks, CJK, digits).
- Punctuation-only queries use the literal matching `memory_bm25.py` applies.
- Repeated query terms, the field built from title, tags, summary and text,
  and the front matter that field includes all follow Python. The goldens pin
  each one.
- k1=1.2, b=0.75, scores rounded to 4 decimals before ranking.
- Corpus statistics (document frequency, average length) cover the caller's
  visible corpus: live cards in the scopes the caller can read. The hub keeps
  per-scope statistics, updates them on every put, retire and revival, and
  sums them over the readable scopes per query. A retired or unreadable card
  therefore never changes another caller's scores, and a local index rebuilt
  over the same visible cards scores the same.
- Ties: the local scorer breaks ties by path. The hub breaks ties by the
  string `cards/<card_id>.md`, then by scope, which matches a local index of
  the same cards stored under that path. Path order and id order differ
  (`cards/a-b.md` sorts before `cards/a.md`, while `a` sorts before `a-b`),
  which is why the tie key is the path string. A local workspace with cards in
  subdirectories breaks ties differently, so R0 parity is defined on the
  `cards/<card_id>.md` layout.
- The hub does not use SQLite FTS5 ranking, whose tokenizer and statistics
  differ.
- Goldens (S13b): punctuation-only queries, Unicode case expansion, repeated
  terms, ties including the `a` and `a-b` pair, front matter, retired cards
  and disjoint scopes.

### Retrieval pipeline

1. Normalize the query as the recall hooks do: one line, at most 256
   characters.
2. Build the candidate set from live cards in the caller's readable scopes.
3. Lexical: BM25 as specified above, top 200. The scan is wider than the 20
   results returned so that collapsing and demotion still leave a full page.
4. Dense: embed the query within a 300 ms budget, take dot products against
   the matrix rows in the candidate set, top 200. A document without a vector
   for its current content is left out of the dense list and stays findable
   lexically.
5. Fuse with reciprocal rank fusion: score = sum over lists of
   weight / (60 + rank), weights 1.0 until the eval says otherwise.
6. Collapse lineage: cards linked by `supersede_card` or
   `merge_exact_duplicates`, and handoff-derived items from one chain, appear
   once, as the newest live head.
7. Demote repetitive automated sources: versions that are still
   `agent-unreviewed` and came from a `scheduled` or `worker` writer sort after
   every other match, unless the query sets `sources: ["automation"]`.
   Reviewed, promoted and imported cards are never demoted. The writer class
   comes from the credential's registered class. The `origin` hint on an event
   can lower it and never raise it. Node tokens serve people and timers on one
   machine, so a node write's class comes from its hint, which affects ranking
   only.
8. Return at most 20 matches with `scope`, `card_id`, `revision`, `title`,
   `summary`, `tags`, `score`, `content_hash`, `trust` and each list's rank.
   The response says which mode ran (`lexical`, `hybrid`) and why a hybrid
   query degraded (`embedder-timeout`, `embedder-unavailable`, `no-vectors`).
9. Optional rerank (D9): a caller that wants one answer reranks the top 20
   with its own read-only seat, through the planned `memory answer`. The hub
   is not involved.

R0 compares the raw lexical list from step 3. Steps 6 and 7 are a ranking
change like any other: they are on by default only if they do not lower hit@5
or MRR on the public fixture.

The `search_cards_payload` shape stays. `path` for a hub card is
`cards/<card_id>.md`, so existing callers that read a card by basename keep
working. Each match also carries `scope`.

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
through `--fixture-root` and report aggregate numbers only. Every gate record
names the `indexed_seq`, index format version, scorer version and policy
revision it ran against, so a Tier 0 gate can tell whether its eval record
still applies.

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

The HTTP client speaks two request shapes, `/api/embed` and
`/v1/embeddings`, selected in `hub.toml`. Embedding endpoints must be loopback
or on an allowlist of private hosts, the same pattern as the events callback
hosts, and HTTPS endpoints use rustls. The embed worker is the planned
`memory embed-worker`, stdlib Python. It holds a principal with only the
`embed` grant and pulls, so the GPU host needs no inbound route from the hub
for document work.

During an embedder change both matrices exist. The new one backfills in the
background, and the default flips by configuration only after the new id
covers every live card and passes R1. The old matrix is dropped one release
later.

### Embedder benchmark plan

Read-only, and never on the production hub host. S16a measures, and S16b
decides once S17's R1 numbers and the S6a load harness exist.

- Host for `hub-cpu`: a non-production machine with the hub host's CPU
  architecture, limited by cgroup to the reference hub allotment of 1 vCPU and
  512 MB, and once more to a larger allotment so the operator can see what
  `hub-cpu` would need. A 0.6B-parameter model holds about 1.2 GB of weights at
  16 bits and about 0.35 GB at 4 bits, so `hub-cpu` is not expected to fit the
  current allotment beside the hub.
- Host for `remote`: the GPU host.
- Data: a copy of a frozen corpus snapshot. Nothing writes to a live hub.
- Measure for each backend and model: document throughput (cards per
  second), query embedding latency p50, p95 and p99 at 16, 64 and 256
  tokens, cold start time, resident memory, CPU seconds per query, the S6a
  load test run at the same time with its writes per second and p99 compared
  with a run without embedding, and the behavior with the embedding server
  stopped (lexical answer within the budget).
- Quality: R1 numbers per model, from S17.
- Decision rule (S16b): `hub-cpu` becomes the default when its query p95 is at
  most 150 ms inside the allotment, the hub plus the embedder stay under 70%
  of the allotment's memory at peak, and the concurrent load test still meets
  the S6 gate. Otherwise `remote` is the default and `hub-cpu` stays
  available.
- Output: one results table in the S16a PR, aggregate numbers only, and the
  default S16b sets.

### Local memory and the hub

A workspace chooses one of three modes in its memory configuration:

| Mode | Search | Writes |
|---|---|---|
| `off` (default) | local cards only, as today | local only |
| `read` | hub and local results fused with RRF, deduplicated by `(card_id, content_hash)`, each match labeled `source: hub`, `local` or `replica` | local only |
| `sync` | as `read` | local card changes are also published to the hub as `card.put` events in the workspace's configured scope |

- Hub search has an 800 ms budget. On timeout or error the client answers
  from local cards and the replica, and says so in the result.
- The replica, `.brigade/memory/hub-replica/`, holds the current heads the
  principal can read, a `seq` watermark and the `log_epoch` it was built
  under, capped at 50 MB by default. It is per host, ignored by Git and
  excluded from vault sync, like the per-host index from #2196. A changed
  `log_epoch` resets the watermark, and the client pulls the log again from
  the start.
- The spool, `.brigade/memory/hub-spool/`, holds writes the hub has not
  acknowledged. They flush in order with their `client_event_id`. A
  `memory_base_mismatch` turns into a local proposal for review, and so does
  an entry older than 30 days.
- The client spools on transport failure and on `503`. A `404` means "no hub
  memory" only when this workspace holds no replica watermark for this hub. If
  it does, the hub has memory and is in a rollback window or misconfigured, so
  the client keeps spooling, serves reads from the replica and local cards,
  and warns. The spool is capped at 10,000 entries. Past the cap, writes stay
  local and the warning says so.
- Cards marked `visibility: private` in front matter are never published or
  imported.
- Turning on `sync` for the first time prints how many cards it will publish
  and to which scope, and needs `--yes`.
- Hub Lite and Python hubs answer 404 on `/memory`.

Privacy:

- Prompt-driven recall sends the query to the hub over the authenticated
  transport. The hub keeps it in memory for that request only.
- The hub keeps one key in a 0600 file beside the token file, never in the
  database. The recall log stores HMAC-SHA256 of the normalized query under
  `HMAC(key, "brigade.memory.recall.v1")`, and the findings ledger uses
  `HMAC(key, "brigade.findings.v1")`. A database copy alone cannot test
  guesses, and a recall-log dump cannot be joined with a findings dump.
- The hub never stores raw query text, including for explicit searches. The
  local log keeps its #2196 rules.
- Query text reaches an embedding server. That server is inside the trust
  boundary, which is why endpoints are limited to loopback or an allowlist.
- S14 repeats the #2196 sentinel test on the hub: after a recall with a
  sentinel prompt, the sentinel appears in no database page, WAL, log or
  metric.

### Content scanning

Scanning happens at two times: when text is written to memory, and when text
is about to enter a prompt.

Write time covers every `card.put`, `import`, `handoff.submit` and
`evidence.attach` text and the text of every proposal op:

- The guard's bundled default rule set only, pinned by version in
  `memory_meta`. Repository rules, baselines and allow markers do not apply on
  the hub, because the hub has no repository.
- The native PCRE2 engine (D16) in UTF mode (S18a). #2212 scans ASCII input
  and hands non-ASCII input back to Python `re`. The hub has no Python
  fallback, so S18a adds UTF mode with Unicode properties and a parity test
  against Python `re` on the guard's Unicode fixture. Rules whose results
  differ between the two are listed and run in both ASCII and UTF modes, and a
  hit in either counts.
- Invisible and bidirectional characters, checked without regex. Bidi
  embedding, override and isolate controls (U+202A to U+202E, U+2066 to
  U+2069), zero-width space and invisible operators (U+200B, U+2060 to
  U+2064), U+FEFF other than a leading byte order mark, and tag characters
  (U+E0000 to U+E007F) block the write. Zero-width joiner and non-joiner
  (U+200C, U+200D) store the version as `flagged`, because some scripts and
  emoji sequences need them.
- Role-override text: a rule family for phrases that try to replace an
  agent's instructions, for example "ignore previous instructions", "you are
  now" or "new system prompt". A hit stores the version as `flagged` and
  forces any proposal that touches the card to Tier 2.
- A blocking finding refuses the write with `422` and
  `code: "memory_content_blocked"` plus rule ids, never the matched text. A
  warning stores the version as `flagged`, which is searchable on request and
  never pushed.
- Scan unavailable (engine missing, UTF mode missing, or an engine error):
  the write is stored `agent-unreviewed` with scan state `pending`, and every
  trust raise and every push of that version is refused until a later scan
  passes.

Prompt-load time covers every path that puts memory text into a prompt: the
session-start and prompt recall hooks, `fleet_digest`, `inbox_list`, dispatch
packets and the Grok Bot profile tools. Each scans the exact title, summary or
text it is about to inject with the current rule set, locally for local cards
and on the hub for hub cards. A hit replaces the item with
`{card_id, blocked: "<rule id>"}`. This catches text written before a rule
existed, and local cards edited on disk.

### Reflect, proposals and promotion on hub memory

State on main: #1856 is open. Reflect candidates and proposal v2 ops exist
(`memory_reflect.py`, `memory_reflect_candidates.py`,
`memory_proposal_v2_ops.py`). The only tier logic marks a candidate Tier 2
when it is review-only and Tier 0 otherwise. There is no Tier 1, calibration
record, policy `memory` section, blast cap, cooldown, `care install` entry or
invariant test. #2187 adds `brigade memory reflect preflight`, a diagnostic
Tier 0 report (`brigade.memory-tier0-preflight.v1`) that always says
`eligible=false`. S19a to S19e build the tier rules on hub memory in five PRs,
and the local implementation of #1856 can reuse their tests.

Reflect stays a batch job (#1856). On a hub-backed workspace:

1. The job reads `log` from its `seq` watermark and builds candidates as it
   does locally. Evidence references are namespaced as
   `<node or principal>/<workspace id>/<local id>`, so ids from two
   workspaces never collide.
2. Dry run before staging: `propose` validates every op against the base
   revision's bytes and computes the unified diff before anything is stored.
   An invalid proposal is refused at once with `422 memory_proposal_invalid`
   and a reason per op, instead of waiting in a reviewer's queue. Ops that
   cannot be validated are dropped, never guessed. A proposal with every op
   dropped is `failed`, and no card changes.
3. Pinning: each op records the exact bytes and SHA-256 of the block it
   targets, plus the card's base revision. At apply time the hub requires
   those exact bytes at that block id. Any change fails the op closed. The hub
   never re-finds a target by substring or position.
4. The hub computes the tier itself from op types, scopes, trust and content
   flags, and ignores any tier the submitter claims. A claim-text change is
   Tier 1 at least. Opposite polarity, cross-scope, mixed trust, a flagged
   version, role-override text, or a human edit newer than the base revision
   makes it Tier 2.
5. Reflect proposes changes to cards only. No automated path edits skills,
   instruction files (`AGENTS.md`, `CLAUDE.md` and their kind) or policy, on
   the hub or locally. A test enumerates every op target and fails on anything
   other than a card block.
6. A decision is a reviewer's `decide` (the admin token or a principal with
   the `review` grant), or the hub's policy engine for Tier 0 once S19e
   enables it.
7. Apply is one memory transaction for the whole proposal: every
   compare-and-set on base revisions, every pinned block check, every card
   event and the decision state commit together or not at all. Inside that
   transaction, and under the authority lock (D23), the hub re-verifies the
   blockers: the current policy revision and digest, contrary evidence or
   negative outcomes attached after the proposal opened, cooldown and blast
   cap from proposal history, and the scan state of every resulting version.
   Any blocker marks the proposal `stale` or `held` and changes nothing.
8. `memory reflect rollback` appends a compensating proposal that restores the
   previous revision's content and trust. It goes through the same
   compare-and-set, so it ends `stale` if the card moved since, and the
   reviewer sees why.

| Tier 0 gate | Source of truth on the hub |
|---|---|
| Policy enables the op class | `memory` section of the current fleet policy revision. Its digest is bound into the proposal and re-read under the authority lock |
| Proof: at least 3 distinct evidence ids from at least 2 sources, no contrary ids | attested evidence (S19e): distinct namespaced ids, with sources counted as distinct attesting credentials, never as names written in the proposal |
| Sources trusted and scan-clean | the attesting credentials appear in the policy's reflect principals, and every touched version scans clean |
| Retrieval eval gate passed | the latest R0 or R1 record for the active mode, bound to `indexed_seq`, index format, scorer version and policy revision |
| Blast cap and per-card cooldown | `memory_proposals` history, read inside the apply transaction |
| Brigade is the configured memory owner | the policy's curation section names the hub. With an external owner or a handoff-only policy, reflect emits proposals and never applies |
| Preflight agrees | the hub `preflight` action returns the #2187 report schema, filled from hub facts, read-only |

Evidence attestation (S19e). An `evidence.attach` event carries a namespaced
id, the artifact kind, the SHA-256 of the artifact bytes and its observation
time, plus the bytes themselves for artifacts up to 64 KiB. The attesting
principal comes from the credential. Proof counts distinct ids attested by
distinct credentials within the policy's freshness window. When a credential
is revoked, its attestations stop counting. Until S19e lands, the hub refuses
a policy whose `memory` section enables auto-apply (422), and every proposal
waits for review (D25, Q6).

Invariant tests, in S19a to S19e:

- A Tier 1 op never auto-applies without a calibration record. A mutation
  test removes the check and must fail.
- Tier 2 always waits for review.
- Auto-apply cannot be enabled before S19e.
- A failed worker call, invalid ops, a failed eval gate or a failed write
  leaves every card revision and every vector byte-identical and records
  `failed`. Repeated failures pause the scheduled job until a success.
- A policy edit invalidates pending auto-promotions, because the policy
  digest is part of the binding.
- A block changed after `propose` fails its pinned check, and contrary
  evidence attached after `propose` blocks the apply.
- A multi-card proposal with one stale base revision changes no card.
- Auto-applied decisions are distinguishable from reviewed ones in the
  decision log and reversible by a compensating proposal.
- A new version without a vector yet keeps the card findable lexically. A
  failed embedding never removes a healthy vector of an older version until
  the new one exists.
- Fabricated, unresolved and cross-workspace aliased evidence never counts
  toward proof.

## Connectivity

### What each agent class gets pushed

| Agent class | Pushed content | Existing call it rides on | Size bound |
|---|---|---|---|
| Local harness (hooks in Claude Code, Codex and others) | hits for the repository context at session start, labeled `query_source: "cwd"`, and hits for each prompt | the existing SessionStart and UserPromptSubmit recall hooks | 10 lines, 5 s timeout (current caps) |
| Local harness, session end | capture: hashed session reference, card keys pushed, card keys opened, outcome if known | the existing Stop hook | one `recall.capture` event, ids only |
| Cloud connectors (Dot, Duet) | a `memory` section in `fleet_digest` with the top 5 for the principal's charter duties and oldest unacked messages, and `related_memory` on each `inbox_list` item with the top 3 for that message | `fleet_digest`, `inbox_list` | 5 and 3 items, summaries at most 300 characters, 4 KiB per response |
| Grok Bot | card references (scope, id and content hash) in job metadata, and card text only through its memory profile tools | the Grok Bot job envelope, plus `memory_search` and `memory_card` | 5 references |
| T3 children and `brigade run` workers | a `memory_context` block in the brief, with ids, titles and summaries | the brief builders (`aboyeur` briefs, the Grok Bot T3 adapter, native T3 child tasks) and Worklore prepared handoffs (`brigade.prepared_handoff.v1`) | 5 items, 4 KiB |

- Pushed items carry `scope`, `card_id`, `title`, `summary`, a short content
  hash, `trust`, `score` and `source`, never the card body. Agents read bodies
  with `memory_card`, which Dot and Duet already have.
- Push and search use one ranking. Push adds the trust filter, the head
  revalidation from [Index structures](#index-structures), and the
  prompt-load scan.
- The Grok Bot queue keeps its rule that hub payloads are metadata only, so
  its jobs carry card references only. The feed that enqueues a job computes
  them from the task text it already holds.
- A T3 child in a Brigade-wired worktree also runs the recall hooks. The
  prompt hook skips cards whose ids already appear in the brief's
  `memory_context` block.
- New fields in tool responses are additive. Connector tool snapshots are
  updated in the same PR.

### Interim connectivity

A connectivity PR is in flight. It adds a read-only Grok Bot memory profile,
fixes the principal inbox `request_denied` answer, and adds bounded memory
hits to the `fleet_digest` and `inbox_list` responses from the memory that
exists today. It is the first step of S23 and S26, on these terms, so that the
later slices only swap the source to hub memory:

- The same field names and bounds as S23: a `memory` section in
  `fleet_digest` with at most 5 items, `related_memory` on `inbox_list` items
  with at most 3, summaries at most 300 characters, 4 KiB per response. Items
  carry ids, titles, summaries, a short hash, a score and a source, never
  bodies.
- Items carry `content_trust: "untrusted"`, and the connector instructions
  keep the sentence that memory text is data, never instructions.
- The prompt-load scan applies. Until S18 ships, the interim PR scans with
  the local guard's default rules plus the invisible and bidi character check.
- The read-only profile is S26's memory profile without `handoff_write`. S26
  adds `handoff_write`, moves the profile onto hub grants, and retires the
  vault-backed Grok tools.
- The `request_denied` fix is a separate correction and a precondition for
  S26's inbox profile tests.

### Trust filter

| Card trust | Pushed | Searchable |
|---|---|---|
| `reviewed`, `policy-promoted`, `imported` | yes, after a clean scan of that version | yes |
| `agent-unreviewed` | only to the principal that wrote it | yes, labeled |
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
| memory | `memory_search`, `memory_card`, `handoff_write` | read on the `fleet` scope and the `handoff` grant. `handoff_write` lands in `memory_handoffs` for review and never writes a card. The interim PR ships this profile without `handoff_write` |
| inbox | `message_send`, `inbox_list`, `inbox_ack`, `inbox_reply` | message send grants (`may_send_to`) as the sender allowlist. A send outside it gets the v1 `message_recipient_not_permitted` code |

Neither profile can see dispatch, claim or fleet-control tools. The Grok Bot
charter covers X work only. After the card import (S20), these profiles
replace the vault-backed Grok tools in `grokbot_cerebro.py`.

### Duty registry and findings ledger

The duty registry is the owners charters in the fleet policy document. No new
registry is built. The findings ledger is new: tables on the hub database,
added through the Python schema (D6), and one route.

```text
agent sees a real event (an email, a job post, a pull request)
  -> canonical key, computed locally
       email: Message-ID, lowercased, angle brackets removed
       job: canonical URL (https, lowercase host, no fragment, no utm_* or
            other tracking parameters, no trailing slash), or the ATS job id
       pull request: owner/repo#number
  -> POST /findings {action: "observe", kind, key}
  -> hub stores HMAC(findings key, kind + ":" + key), never the key itself
  -> answer: {finding_id, first_seen, owner, state, report_ref, reservation}
       reservation is set only for the owner principal, only while no report
       exists and no other reservation is live:
       {reservation_id, generation, expires_at}, 10 minutes by default
  -> the holder reports externally, carrying
       report_key = HMAC(findings key, "report:" + finding_id)
     wherever the destination can hold it
  -> POST /findings {action: "reported", finding_id, reservation_id, generation, report_ref}
  -> everyone else sees report_ref or a live reservation and stays quiet
  -> POST /findings {action: "resolve", finding_id} when done
```

- `observe` takes the reservation in one writer command, so two sessions of
  the same principal can never both hold one. A session that observes while
  another holds the reservation gets `reservation: null` and the expiry time.
- `reported` needs the live reservation id and generation. After expiry it
  answers `409` with `code: "finding_reservation_expired"` unless the finding
  is still unreported and unreserved, in which case the report is accepted. A
  second `reported` answers `409` with `code: "finding_already_reported"` and
  the existing reference.
- A crash after the external report and before `reported` lets the
  reservation expire, and another session may report again. Delivery is
  therefore at least once across crashes. A holder whose reservation has a
  generation above 1 searches the destination for `report_key` first when the
  destination can be searched (sent mail, an issue tracker), and if it finds
  it, records that reference instead of reporting again.
- Owner reassignment: a charter change applies to new reservations, and a
  live reservation runs to its expiry.
- Authentication: node tokens, and message-principal tokens with a `findings`
  grant (added with the ledger through the Python schema). The admin token
  holds it. A principal whose charter names no finding kind can observe and
  read but never receives a reservation.
- Charters gain an additive `findings` match key listing finding kinds, and
  the owners view lists open findings as holdings.
- Code placement: `src/brigade/fleet_hub.py` is 1,990 lines and
  `src/brigade/fleet_hub_http.py` is 1,998, against the 2,000-line ceiling in
  `tests/test_module_size_ratchet.py`, and neither may be added to its legacy
  list. S22a first extracts a route-registration seam from `fleet_hub_http.py`
  so new routes live in their own modules. S22 then adds
  `fleet_hub_findings.py` (schema and logic) and `fleet_hub_findings_http.py`
  (the route), wired through that seam and the existing `ensure_schema` calls
  in `init_db`. S11's Python half and S28's tables use the same seam.
- The ledger ships on the Python hub first (S22), so the job-leads relay can
  move onto it before the Rust port. Rust proxies it until S12h.

## Wire contract

- Contract v1, frozen: the existing 77 cases must pass against the Rust hub
  exactly, quirks included, through every phase.
- More v1 coverage before porting: S1 adds drive cases for the hot set (claim
  node and force scopes, leases and fencing, the message actions `reply`,
  `sent_lookup`, `who`, `claims`, keys and manifests, the question actions,
  sealed envelopes and `wait_ms`). They record what Python does today, so
  `contract_version` stays 1. S1 pins only the per-principal waiter cap of 2
  (`MAX_WAITERS_PER_CALLER`). Rust raises the total and per-source caps,
  which the contract treats as capacity limits. S1b adds expiry visibility, the
  message `client_id` replay window, and the exact text of every refusal
  path.
- Profiles: `core` (the default), `memory` (S13a), `findings` (S22), `ops` for
  `/metrics` and `/ready` (S9), and `prod-readonly` (S29). Each case pins
  status, keys, types and `code` values, as v1 does. New routes use
  `{error, code}` for every 4xx, since they have no legacy shape to match. The
  drive stays hub-agnostic (no `brigade.*` imports).
- Error text: one golden table captured from the Python hub by S1b over every
  refusal path, text formatted inline included. Rust is checked against that
  table.
- Contract v2 (#2207), after phase 3: one auth-failure envelope and status, a
  `code` on every 4xx JSON error, JSON refusals on JSON routes, 403 for a
  principal token on node routes, and `GET /version` reporting
  `contract_version`, the engine, and capabilities. The identity capability
  list gains `memory` and `findings`. If the Python hub is still inside its
  rollback window, it changes in the same PR through the S22a route seam, and
  the goldens move to 2. #2207's own plan says Rust targets v2 from the start.
  D20 replaced that on 2026-11-03, and the issue's plan line needs the same
  change.

## Work in flight

Several pieces of this plan are already being built. This table says how each
one fits and what it needs from this revision.

| Work | State | Fits as | What changes |
|---|---|---|---|
| Contract hot-set coverage | PR coming | S1 | Pin only the per-principal waiter cap of 2, never the total cap (8) or the per-source cap (2). Build sealed cases from envelopes generated once by Python and checked in with their published keys. Expiry, `client_id` window and error-text cases go to S1b if S1 merges first |
| `brigade-hub` proxy crate | PR coming | S3 | The proxy contract (auth ownership, raw bodies, header rules, timeouts, no retry of writes, 502 and 504 answers, supervisor, `/ready`) joins S3's acceptance if S3 has not merged, and is S3b otherwise |
| Embedder benchmark | in progress | S16a | Measure at the 1 vCPU / 512 MB allotment, where a CPU embedder is not expected to fit, and at a larger allotment so the operator sees what `hub-cpu` would need. The default decision moves to S16b, after S17's R1 runs and the S6a harness exist |
| Connectivity PR: read-only Grok Bot memory profile, principal inbox `request_denied` fix, bounded memory hits in digest and inbox responses | in flight | first step of S23 and S26 | The terms in [Interim connectivity](#interim-connectivity): S23's field names and bounds, `content_trust: "untrusted"`, a prompt-load scan, and the profile defined as S26's memory profile without `handoff_write` |
| #2187 Tier 0 preflight | draft | local diagnostic | S19c's hub `preflight` action returns the same `brigade.memory-tier0-preflight.v1` report, filled from hub facts. The hub adds the pinned-block check and blocker re-verification inside the apply transaction |
| #2213 engine rename plan | merged plan | naming | The hub binary is `brigade-hub` with no alias. S10 rebases on the rename's manifest and installer slice (R5) if R5 lands first |
| #2212 native PCRE2 guard | open, ASCII only | the engine D16 names | S18a adds a UTF mode with parity tests for hub use. The guard's own behavior stays as #2212 ships it |
| #2210 hub work edges | open | S28 | Built in Rust after the Worklore family port, with a Python version only if that port slips (D27, Q7). Its compaction invariants are in [Log retention and compaction](#log-retention-and-compaction) |
| #2207 contract v2 | open | S11 | Its plan line about Rust targeting v2 from the start needs to match D20 |

## Build plan

Each slice is one PR. Acceptance tests are what the PR must show. "Parallel
with" lists slices that touch different code and can run at the same time.
Slice ids from the first draft keep their meaning, because PRs in flight use
them. A split adds a letter (S4a, S4b), and a new slice takes the next number.

### Hub track

| ID | Slice | Depends on | Acceptance | Parallel with |
|---|---|---|---|---|
| S1 | Contract drive: hot-set coverage on the Python hub (claim scopes, leases and fencing, message and question actions, sealed envelopes from checked-in Python golden envelopes, `wait_ms` wake and the per-principal waiter cap) | none | New cases pass on the Python hub. Inventory and `COVERED_ROUTES` updated. The drifting-proxy test still finds each injected drift. No case pins the total or per-source waiter cap | S2, S3, S16a, S22a |
| S1b | Contract drive follow-up: message and question expiry as clients see it (expired messages absent from `list`, `count` and `sent`, a `reply_to` to a purged message reads null, expired questions), the message `client_id` replay window, and the exact text of every refusal path | S1 | Cases pass on the Python hub. A golden error-text table replaces any table derived from constants | S2, S3, S29 |
| S29 | Read-only production profile for the drive: `--profile prod-readonly`, an audited allowlist of read cases (the GET routes, plus `list`, `count` and `identity` for an operator-supplied probe principal). A case outside the allowlist stops the run before any request. Credentials come from the operator, and the drive never enrolls | S1 | An integration test runs the profile against a Python hub on a copy of a seeded database and finds every table's content digest unchanged, except rows that message retention would remove anyway. A unit test fails if any profile case writes. The runbook names it as the only drive run allowed against production | S1b, S3 |
| S2 | Storage parity drive: one scripted sequence, every table dumped (trigger-maintained ones included), exact values, id mapping, timestamp relations, recomputed digests, mixed-engine and migrated-fixture runs, a defaults run and an events-outbox run | none | Deterministic golden on two runs. Each mutation is caught: a timestamp format, a JSON separator, a generation off by one, an expiry one second late, a wrong digest, a broken id relationship | S1, S3 |
| S3 | `brigade-hub` crate: config and flag parity, conventions layer, admission, native `/health`, everything else proxied, and a Linux CI build | none | The full drive passes with Rust in front of the Python upstream. Flags are rejected or required exactly as `fleet serve` does | S1, S2, S16a, S22a |
| S3b | Proxy transport contract and supervision, as in [Upstream proxy contract](#upstream-proxy-contract), plus `GET /ready` | S3 | The proxy contract tests pass. The drive still passes with Rust in front, and the `/policy` and `/audit` quirks survive the proxy | S4a, S4b, S6a, S9 |
| S4a | Writer: group commit at `FULL`, savepoint per command, abort detection with one re-apply, COMMIT failure handling, `BEGIN` retry with a bounded wait, the durable lane, batch budgets and command deadlines | S3 | One failing command does not affect its batch. An injected `SQLITE_FULL` mid-batch leaves no command answered 2xx unless its rows committed. 20 `kill -9` runs keep `integrity_check` ok with no acknowledged loss. The VFS power-cut test passes 500 cut points at `FULL` and reports losses at `NORMAL`. A Python write lock held for 3 s delays a claim batch without failing it. Overload at three times capacity keeps resident memory bounded and answers the excess with 503 | S3b, S4b, S6a, S9 |
| S4b | Readers and schema: reader pools and queues, the read-transaction cap, the CPU pool, the structural check with triggers and constraints, `required.json`, the Python lockstep test on fresh and migrated fixtures, the `SQLITE_SCHEMA` recheck, the `data_version` check on the writer connection, and the maintenance connection with checkpoints and disk-pressure admission | S3 | A schema mismatch, a missing trigger included, refuses to start. The lockstep test fails on a Python schema bump and on an added column. Both SQLite builds open every fixture. A pinned reader keeps a truncating checkpoint busy without stalling the writer. A slow query does not hold up other readers beyond its own slot. Rollback to Python with live `-wal` and `-shm` files passes the drive. Reader pool defaults are set from measured query time and resident memory | S3b, S4a, S6a, S9 |
| S5 | Native registration and authentication: admin, node and principal tokens, `/nodes`, `/message-principals`. Ships in `proxy` mode and flips last in phase 3 | S4a, S4b | Drive cases for these routes pass natively. Storage parity matches. A revoked token is refused on the next request | S6a, S9 |
| S6a | Load harness in `engines/hub/bench/` (development only, not released) with pinned conditions, closed-loop and open-loop modes, and every attempt recorded | S3 | Runs against the Python hub and against Rust in proxy mode, three runs each, on the reference machine and in a 1 vCPU / 512 MB cgroup. The report records machine, allotment, payload, database size, flags, connection mode, run length, warmup, client cores, offered rate, every error, latency percentiles, queue depth, batch sizes and checkpoint stalls. One end-to-end run through the operator's TLS proxy is reported and not gated | S3b, S4a, S4b, S9 |
| S6 | Native heartbeat: `/events` (batches up to 1,000), `/status`, `/sessions` | S5, S6a | Drive and storage parity pass. Under S6a conditions, at `FULL`: at least 10 times the Python hub's OK single-event writes/s at the same allotment, and at least 4,600/s on the reference machine at 16 to 64 clients with p99 under 250 ms. No refused connections at 256 clients. The mixed run with the Python upstream writing cold routes and claims running is recorded, lock wait included | S7, S8, S13a |
| S7 | Native claims and owners, plus a claim client that retries a `hub busy` 503 or a 504 with the same lease token within its timeout | S5 | S1 claim cases pass. A property test with 64 concurrent clients finds no two holders and no repeated generation, also across a VFS power cut. 4,096 queued events do not delay a claim past 50 ms at p99 | S6, S8, S13a |
| S8 | Native messages: every action except `events_*`, the `question_*` actions included, with sealed validation, retention and the long-poll protocol | S5, S1b | S1 and S1b cases pass, expiry included. Default profile: 3,072 idle waiters inside the 512 MB allotment. High-capacity profile: 10,000 idle waiters from 5,000 principals over 64 loopback source addresses, with resident and kernel socket memory recorded separately. A send wakes its waiter within 50 ms at p99, and within 400 ms after a commit by another process. The register-before-read race, revocation while waiting, a 3,000-waiter wake storm and client disconnects (slot freed within 1 s) pass. The differential JSON fuzz (10,000 bodies) gives the same status and error text on both hubs | S6, S7, S13a |
| S9 | Route modes, the shadow comparator and its file, `/metrics`, structured logs | S3 | Comparator tests cover definite, transient, inconclusive and boundary results. An injected mismatch with no commit in between is definite. Rust never writes in shadow mode, and every shadowed route is checked on a Python hub for housekeeping writes. `ops` drive cases cover `/metrics` authentication and `/ready` | S3b, S4a, S4b, S6a |
| S10 | Packaging and operations: the release job for five targets, PR builds for `engines/hub/`, pre-publication acceptance, manifest entry and validators, `setup --component brigade-hub`, `fleet serve --engine rust` with the supervisor, Python upstream mode, container recipe, cutover and rollback runbook | S6, S3b | Pre-publication acceptance passes on each target. `fleet serve` without `--engine` is unchanged. Upstream mode sets the client-address header and `wal_autocheckpoint=0`. Rollback test: stop Rust, start Python on the same files, the drive passes and every row reads | S7, S8 |
| S11 | Contract v2 (#2207) in both hubs | cutover phase 3, S22a | Goldens move to version 2 and `GET /version` answers. Every client caller from the contract doc checked with `brigade code callers` | S12a |
| S12a to S12i | Cold families, one PR each: (a) preference and audit, (b) mesh, (c) policy, reads first, (d) models and cloud, (e) the Grok Bot queue, (f) CI with reports and actions, (g) Worklore, (h) findings, (i) webhook delivery with retention sweeps | S9, and that family's drive cases written first | That family's drive cases pass natively. Storage parity matches. Shadow clean for 7 days before native | each other |
| S28 | Hub work dependency edges and ready view (#2210) | S12g | Blocked-by edges with the local ledger's semantics: open blockers on an ancestor propagate, completion releases dependents, and a cycle is refused. A ready view in the CLI and in the agent tool response lists only claimable work. A claim on blocked work gets 409 with the blocker ids. Local ledger edges sync on import. Additive drive cases. The edges table lands through the Python schema | S12 families |

### Memory track

| ID | Slice | Depends on | Acceptance | Parallel with |
|---|---|---|---|---|
| S13a | Memory store and routes: log, revisions, blobs, compare-and-set on revision, idempotency, grants, scopes, trust rules, the authority lock, `log_epoch`, the 404 and 503 rules, a scan hook that reports unavailable, and the `memory` drive profile | S4a, S5 | Drive cases pass. Same id in two scopes, a trust change on identical bytes, a stale update after retire and revival, bytes A then B then A, and a write into a scope the caller cannot write all behave as specified. An admin `put` stores `agent-unreviewed`, and an import without the grant gets 403. A revocation and a policy edit at every step of the authority protocol give the specified outcome. Hub p99 with memory writes running is recorded | S6, S7, S8 |
| S13b | Lexical index and scorer: segmented snapshots, `indexed_seq` and `min_seq`, per-scope statistics, the scorer and tokenizer goldens, R0 | S13a | R0 passes on the public fixture. Every scorer golden passes. Writes during a rebuild, an index failure after commit, and restart replay behave as specified. Rebuild time and peak resident memory at 20,000 synthetic cards are recorded | S6, S7, S8, S18 |
| S14 | Python client: the planned `memory hub status`, `push`, `pull` and `import`, search fusion and fallback, the replica, the spool, the hub recall log, and Dot and Duet memory tools routed through it | S13b | With the hub stopped, search answers locally and labels it. The sentinel prompt appears nowhere on the hub or the client. A conflicting offline edit becomes a proposal. A 404 from a hub where the workspace holds a replica watermark spools instead of dropping. A changed `log_epoch` resets the watermark. First `sync` prints the card count and needs `--yes`. `off` mode output is byte-identical to today | S15, S19a |
| S15 | Embedder interface, both HTTP backends, embed jobs and `memory embed-worker`, vectors, lineage, segmented dense index. Default stays `lexical` | S13b | An embedder change backfills only missing rows and leaves the old matrix intact. Retiring a card removes exactly its lineage rows, and a vector shared with a card in another scope stays until its last reference goes. A failed embedding keeps the card findable lexically. Endpoints outside the allowlist are refused | S14, S16a |
| S16a | Embedder benchmark measurements | none | The results table at the 1 vCPU / 512 MB allotment and at a larger one, aggregate numbers only | everything |
| S16b | Embedder default decision | S16a, S17, S6a | The decision rule applied to S16a's numbers, S17's R1 per model and an S6a run with embedding active. The default mode flips to `hybrid` only if R1 passes | S19b, S20 |
| S17 | Hybrid retrieval, lineage collapse and source demotion, the `hub-lexical` and `hub-hybrid` eval adapters, RRF weight tuning, and R1 runs per candidate model | S15 | R1 report attached. Collapse and demotion do not lower hit@5 or MRR on the public fixture. A superseded chain shows once, and an unreviewed card from a scheduled writer sorts after interactive results | S19a, S20 |
| S18a | Guard engine UTF mode for hub use | #2212 | Parity with Python `re` on the guard's Unicode fixture, with the divergent rules listed. ASCII behavior unchanged | everything |
| S18 | Content scanning on memory writes and the prompt-load scan | S13a, S18a | A blocking rule refuses with rule ids only. A warning stores `flagged`. Bidi and invisible characters block, and zero-width joiners flag. Role-override text flags and forces Tier 2. With the engine missing, writes store scan state `pending` and every trust raise and push is refused. The prompt-load scan replaces a hit with a placeholder | S13b, S14, S15 |
| S19a | Proposals on hub memory: `propose` with dry-run validation, `decide`, `rollback`, pinned block checks, all-card atomic apply | S13a, S18 | Invalid proposals are refused at submit with a reason per op. A block changed after `propose` fails closed. A multi-card proposal with one stale base changes no card. A rollback after an intervening edit ends `stale` | S14, S17 |
| S19b | Tier classifier and the policy `memory` section with digest binding | S19a | Claimed tiers are ignored. Flagged, cross-scope, mixed-trust and newer-human-edit cases are Tier 2. A policy edit invalidates pending decisions | S16b, S20 |
| S19c | Tier 0 gate engine: the hub `preflight` action, blockers re-verified inside the apply transaction, the invariant tests, and auto-apply refused until S19e | S19b, #2187 | Every invariant in [Reflect](#reflect-proposals-and-promotion-on-hub-memory) that does not need attestation passes, including the Tier 1 mutation test and the test that auto-apply cannot be enabled. No op can target anything but a card block | S20, S26 |
| S19d | Scheduled reflect on hub memory: the `memory-reflect` entry in `care install`, pause after repeated failures, `memory reflect rollback` against the hub | S19c, S14 | `memory reflect run --dry-run` writes nothing to the hub. Three failures in a row pause the timer until a success | S19e, S27 |
| S19e | Evidence attestation and hub Tier 0 auto-apply under policy | S19c | Fabricated, unresolved and cross-workspace aliased evidence never counts. A revoked credential's attestations stop counting. Auto-apply runs only when every gate holds | S19d, S27 |
| S20 | Card import: dry run, idempotent, keeps card ids, deduplicates by content hash, skips `visibility: private` | S14, S18 | A second import appends nothing. The dry run lists exactly what the real run appends. Imported versions need a clean scan and the `import` grant | S16b, S17, S19b |
| S21 | `memory answer`: client-side rerank through a read-only seat | S17 | R2 report. Off by default unless R2 passes | after 0.31.0 |

### Connectivity track

| ID | Slice | Depends on | Acceptance | Parallel with |
|---|---|---|---|---|
| S22a | Python hub route-registration seam, so new routes live in their own modules | none | Behavior-preserving: the drive and every hub test pass unchanged, and `fleet_hub_http.py` drops enough lines for the S22, S11 and S28 hooks | S1, S2, S3 |
| S22 | Findings ledger on the Python hub: tables, `/findings` with reservations, canonical keys, the `findings` grant, charter `findings` match, `findings` drive profile | S22a | Drive cases pass. Two concurrent sessions of one principal get one reservation. The expiry rule for `reported` holds. A duplicate report gets 409. Only HMACs under the findings key are stored. A crash between the external report and `reported` leads to a generation-2 reservation and the report-key search | S1, S2, S3 |
| S23 | Push in tool responses on hub memory: `fleet_digest` memory section, `inbox_list` `related_memory`, trust filter, head revalidation, prompt-load scan, size bounds | S14, S18, the connectivity PR | Connector snapshots updated. Responses stay under the bounds with a 2,000-card fixture. Unreviewed cards from other principals never appear. A stale snapshot hit is dropped after revalidation. The runbook step grants Dot and Duet read on `fleet` | S24, S25 |
| S24 | Hooks: hub recall at session start and per prompt, Stop capture, deduplication against brief context | S14, S18 | The capture holds ids only. Hooks keep their 5 s cap with the hub stopped. Local card text goes through the prompt-load scan too | S23, S25 |
| S25 | Dispatch packets: `memory_context` in `aboyeur` briefs, the Grok Bot T3 adapter, native T3 child tasks and Worklore prepared handoffs. References only in Grok Bot job metadata | S14, S18 | Each packet stays under 4 KiB. Grok Bot job payloads still contain no task text | S23, S24 |
| S26 | Grok Bot memory and inbox profiles on hub memory, and retirement of the vault-backed Grok tools | S13a, S20, the connectivity PR | Each profile's tool discovery lists only its tools. A send outside the allowlist gets `message_recipient_not_permitted`. No dispatch tool is reachable. `handoff_write` lands in `memory_handoffs` only. The runbook step grants the memory principal read on `fleet` and `handoff` | S23 to S25 |
| S27 | Connector measurement, repeated with the same 10-prompt method | S23 to S26 | Card-backed answers on at least 8 of 10 prompts, up from 3 of 10 | none |

### Order and lanes

Waves, where every slice in a wave depends only on earlier waves:

```text
wave 0 (some in flight): S1  S2  S3  S16a  S22a  S18a (after #2212)  connectivity PR
wave 1:                  S1b  S29  S3b  S4a  S4b  S6a  S9  S22
wave 2:                  S5
wave 3:                  S6  S7  S8  S13a
wave 4:                  S10  S13b  S18
wave 5:                  S14  S15  S19a
wave 6:                  S17  S20  S19b  S23  S24  S25
wave 7:                  S16b  S19c  S26  S21
wave 8:                  S19d  S19e  S27
after phase 3:           S11, then S12a to S12i one family per PR, S28 after S12g
```

Dependency list, one slice per line, for tooling and review:

```text
S1: none
S1b: S1
S29: S1
S2: none
S3: none
S3b: S3
S4a: S3
S4b: S3
S5: S4a S4b
S6a: S3
S6: S5 S6a
S7: S5
S8: S5 S1b
S9: S3
S10: S6 S3b
S11: phase-3 S22a
S12a..S12i: S9
S28: S12g
S13a: S4a S5
S13b: S13a
S14: S13b
S15: S13b
S16a: none
S16b: S16a S17 S6a
S17: S15
S18a: #2212
S18: S13a S18a
S19a: S13a S18
S19b: S19a
S19c: S19b #2187
S19d: S19c S14
S19e: S19c
S20: S14 S18
S21: S17
S22a: none
S22: S22a
S23: S14 S18 connectivity-pr
S24: S14 S18
S25: S14 S18
S26: S13a S20 connectivity-pr
S27: S23 S24 S25 S26
```

## Release gate for 0.31.0

Proposed, and it depends on Q1, Q2 and Q6:

- Hub: S1, S1b, S2, S3, S3b, S4a, S4b, S5, S6a, S6, S7, S8, S9, S10 and S29,
  with cutover phase 3 complete for at least the heartbeat and claims
  families.
- Memory: S13a, S13b, S14, S15, S16a, S16b, S17 (the default stays `lexical`
  if R1 fails), S18a, S18, S19a, S19b, S19c (hub auto-apply refused) and S20.
- Connectivity: S22a, S22, and S23 to S26.
- If S18a or S18 is not ready, memory ships with nothing pushed except each
  principal's own writes, and S20 and S23 to S26 wait, because every trust
  raise needs a scan (D24).
- After the release: S11 (contract v2), S12a to S12i (cold families), S19d,
  S19e, S21, S27 as soon as S23 to S26 are live, and S28 after S12g.

## Risks

| Risk | Effect | Mitigation |
|---|---|---|
| Behavior the drive does not pin differs between hubs (timestamps, JSON encoding, parser edge cases, side-table and trigger writes) | Rows Python misreads after a rollback. Silent client differences | Storage parity with exact values and mutation tests (S2), differential JSON fuzz (S8), shadow with classified results before every native flip, error text from goldens (S1b) |
| Group commit at `FULL` misses the throughput target on the hub's storage | Release target missed | S6 measures `FULL` at the allotment. Any `NORMAL` family needs its own maintainer decision and never covers durable families (D3) |
| A credential revocation or policy edit races a memory commit | A revoked principal writes memory, or a stale policy promotes | Authority lock protocol (D23) and interleaving tests (S13a) |
| Two writers on one file during the rollback window | Writer stalls behind the Python upstream | Proxied families are the low-rate ones. `BEGIN` retries with nothing applied. Durable commands keep their own lane. S6 measures the mix with claims |
| A Python schema change lands without a Rust update | Rust refuses to start after an upgrade | The lockstep test fails the Python PR, on fresh and migrated fixtures, triggers included. The structural check names what is missing |
| The upstream dies or resets in the middle of a write | Proxied routes fail, and one write's outcome is unknown | 502 and 504 answers that v1 clients treat as an unreachable hub, no retry of writes, supervisor restart, `/ready` (D26) |
| The WAL grows without bound | The disk fills | Maintenance checkpoints, upstream mode, capped read transactions, disk-pressure admission |
| Pushed memory carries prompt injection between agents | One agent's text steers another | Trust rule (D24), push only reviewed, promoted or imported versions with a clean scan, invisible and bidi characters blocked, role-override text flagged, a second scan at prompt load, `content_trust: "untrusted"`, summaries only within size caps |
| The scanner is unavailable | Nothing can be promoted or pushed | Fails closed by design, and S18a and S18 are on the release path |
| Fabricated evidence drives a promotion | A bad card reaches every agent | Hub auto-apply off until attestation (S19e), Tier 0 limits, blast cap, cooldown, compensating rollback |
| Query text reaches an embedding server | A privacy boundary outside the hub | Loopback or allowlisted private endpoints only. Documented as inside the trust boundary |
| The hub becomes a single point of failure for memory | Agents lose recall when it is down | Local replica and labeled fallback, spooled writes, `off` mode unchanged |
| Index memory under the 512 MB allotment | Out of memory during an embedder change or a rebuild at 20,000 cards | Measured in S13b, S15 and S16a. Today's corpus is far below the limit |
| The hub-CPU embedder starves the writer | Throughput or latency regressions | The S16b decision rule includes a concurrent S6a run |
| A finding is reported twice after a crash | A duplicate external report | Reservations with generations and a report-key search before a retry. At least once is stated, not hidden |
| New Rust dependencies in a network-facing service | Supply-chain exposure | Small dependency set, no OpenSSL, lockfile reviews, advisory and license checks in CI |
| Scope: 54 JSON and 37 HTML routes | A full port delays the release | Strangler routing (D5), hot set first, cold families after the release |

## Open questions

These need the maintainer. Each has a recommendation. Q4 from the first draft
(unreviewed cards are never pushed to other agents) is now a design rule
(D24), since both reviews agreed with it, so it is not asked again.

- **Q1, release scope.** May 0.31.0 ship with the Python hub serving the
   cold JSON families and the HTML deck as a loopback upstream behind the
   Rust hub? Recommended: yes, with the proxy contract (D26, S3b) in place and
   release notes that say which routes Python serves. Requiring every JSON
   route to be native puts S12 on the release path, including the policy and
   routing modules (about 4,700 lines of Python).
- **Q2, durability (changes D2).** Keep `synchronous=FULL` on every commit,
   as the Python hub runs today, instead of the `NORMAL` setting D2 recorded?
   Recommended: yes. The first draft offered `FULL` as an extra for claims on
   top of a `NORMAL` default, but the shipped hub never sets `synchronous` and
   so already runs `FULL` everywhere. `NORMAL` would lower durability for
   messages, questions, the audit log and events, and after a power cut could
   hand out a message id a recipient's cursor already passed. Group commit
   pays one sync per batch. The measurement so far comes from a loaded
   machine, so S6 confirms the cost on quiet hardware at the hub allotment
   before release. If `FULL` misses the target there, a later decision can
   name low-value families for `NORMAL`.
- **Q3, publishing default.** Once a workspace has a hub, should its local
   cards publish only after an explicit per-workspace `sync` opt-in?
   Recommended: yes, with the first enable printing the card count and scope
   and requiring `--yes`. Publishing by default would copy every existing card
   set to the hub on upgrade.
- **Q5, rollback window.** Keep the Python hub shipped and drive-tested for
   at least two releases after cutover, and close the window only when the
   S10 rollback test has stayed green in CI throughout and a dated backup from
   before the first Rust migration exists? Recommended: yes. Until the window
   closes, `fleet serve --engine rust` refuses to run Rust schema migrations
   on the hub file.
- **Q6, hub reflect scope (D25).** Ship 0.31.0 with hub reflect limited to
   proposals and review, and keep Tier 0 auto-apply on the hub off until
   evidence attestation (S19e) lands after the release? Recommended: yes.
   Local evidence ids cannot be checked on the hub, and a promoted card
   reaches every agent.
- **Q7, #2210 timing (D27).** Build hub work dependency edges and the ready
   view in Rust right after the Worklore family port (S12g), as the first cold
   family after 0.31.0, and land a Python version only if that port has not
   merged by the 0.32.0 cut? Recommended: yes. #2210 asks for one build, in
   Rust, and the Worklore routes stay proxied until their port.

## Rollback plan

- One family: set its mode back to `proxy` and restart. Takes seconds. The
  Python upstream runs through every phase.
- The whole Rust hub: drop `--engine rust` from the serve command, or start
  the pinned per-SHA virtualenv's `brigade fleet serve` on the same port and
  files. This works because Rust never runs DDL on the hub file (D6), writes
  rows that pass storage parity, and keeps memory in a separate file Python
  never opens. Python opens the hub file with live `-wal` and `-shm` files,
  which S4b tests.
- Memory during a rollback: the Python hub answers 404 on `/memory`. Clients
  that hold a replica watermark for this hub keep spooling `sync` writes,
  serve reads from the replica and local cards, and flush when Rust returns.
  Clients without one fall back to local search.
- Memory backups: the runbook adds `fleet-hub-memory.db` and the hub key file
  to the operator's backup set, taken with the online backup API. A restore
  writes a new `log_epoch`, so clients reset their watermarks. The index and
  vectors rebuild from the store, and promotions are reversed with
  compensating proposals.
- Contract v2 lands only after phase 3, in both hubs at once, so a rollback
  never mixes contract versions.
- After the rollback window, schema authority moves to Rust, and a rollback
  to Python needs a database backup from before the first Rust migration.

## Evidence

### Hub throughput

Measured 2026-11-01 and 2026-11-02 on a 24-thread desktop-class machine with
NVMe storage, over loopback, with throwaway databases. No live hub was used.

Hub, as shipped (one event per request, one connection per request, `FULL`
because the hub never sets `synchronous`): 220 req/s at 1 client, a peak of
about 460 req/s at 8 to 16 clients with p99 of 333 to 535 ms, and refused
connections from 32 clients up. Batching 1,000 events per request reached
37,500 events/s, so the per-request cost dominates. Setting only
`synchronous=NORMAL` did not move the peak.

Same logic in three servers (WAL, `NORMAL`, keep-alive): Rust 15,053 to
18,039 req/s at 16 clients, async Python 4,624 to 5,331, threaded Python 2,293
to 2,462. With `synchronous=FULL` and one commit per request, all three landed
in the same band, about 400 to 1,500 req/s with wide run-to-run swings, bound
by disk syncs. Those servers had no group commit. Idle long-polls: Rust and
async Python served 10,000 connections, and threaded Python failed past about
8,400 threads.

### Durability cost with group commit

Measured 2026-11-03 on the same machine (ext4 on NVMe, SQLite 3.45.1) with
throwaway databases. The machine was not quiet: load average was about 150 on
24 threads, and the kernel reported I/O pressure of 17 to 24% from other jobs.
The test is a Python model of the planned writer loop, since the Rust hub does not exist yet.
Closed-loop clients each submit one command and wait for its reply. One writer
thread takes up to 256 queued commands per transaction with no linger, and
each command inserts an event row and an audit row inside its own savepoint,
against tables with three secondary indexes. Automatic checkpoints are off,
with a passive checkpoint every 200 commits. Each configuration ran 4 s, twice.

| Clients | Sync | Commands/s, run 1 | Commands/s, run 2 | Mean batch | Commits/s | p99 ms |
|---|---|---:|---:|---|---|---|
| 1 | FULL | 10 | 0 | 1.0, 1.0 | 10, 0 | 719, 2,611 |
| 1 | NORMAL | 424 | 18 | 1.0, 1.0 | 424, 18 | 22, 963 |
| 16 | FULL | 82 | 44 | 6.6, 6.8 | 13, 6 | 2,152, 1,593 |
| 16 | NORMAL | 707 | 1,274 | 7.1, 9.8 | 100, 129 | 659, 402 |
| 64 | FULL | 778 | 757 | 28.1, 21.2 | 28, 36 | 1,122, 1,026 |
| 64 | NORMAL | 4,870 | 1,729 | 27.3, 34.9 | 178, 50 | 120, 820 |
| 256 | FULL | 1,227 | 1,190 | 88.0, 82.1 | 14, 15 | 1,298, 796 |
| 256 | NORMAL | 3,578 | 1,084 | 50.1, 47.6 | 71, 23 | 771, 3,827 |

Pairs in a cell are run 1, then run 2. In run 2 at `FULL` with one client, the
only commit in the window took 2.6 s, so the rate rounds to 0.

A direct probe at the same time, 4 KiB writes each followed by `fdatasync`,
two runs of 6 s: median 1.04 and 1.08 ms, p90 6.90 and 43.76 ms, p99 928 and
871 ms, maximum 2,843 and 1,316 ms.

On this machine the tail of the sync latency, not its median, set the `FULL`
rate: with one client, `FULL` managed about 10 commits per second. With 64 and
256 clients the batches grew to 21 to 88 commands, and `FULL` carried 757 to
1,227 commands/s on 14 to 36 commits/s. That is the group-commit effect D3
relies on. `NORMAL` varied by up to 4 times between runs of the same
configuration. These numbers do not predict the Rust hub on quiet storage and
are not a pass or a fail for the 4,600 target. S6 measures that.

### Retrieval

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
ms from 2,000 to 20,000 cards at 1,024 to 4,096 dimensions. Pure-Python BM25
at 100,000 cards took 10.3 s to build and 1.4 GB of memory, which is one
reason the index moves into the Rust hub.

### Guard rules

2 MB corpus: Python `re` 4.3 MB/s, PCRE2 with JIT about 24 MB/s with
identical match counts on every rule.

## Review resolution

Two independent design reviews of the first draft, both dated 2026-11-03,
returned "rework" and both called the architecture sound with targeted fixes.
Review A reported 2 blockers, 16 major and 3 minor findings. Review B reported
3 blockers, 14 major and 15 minor findings, plus 8 contradictions. Of those 61
items, 60 are fixed in this revision. One (A-F20) stands in this document, and
the issue it names needs a one-line change outside the doc. No minor finding
was deferred.

| ID | Review | Severity | Finding | Resolution | Sections changed |
|---|---|---|---|---|---|
| A-F01 | A | blocker | Card identity and compare-and-set ignored scope and authority state | Fixed: cards keyed by `(scope, card_id)`, compare-and-set on a per-card revision, trust and retirement versioned in `memory_card_revisions`, no scope moves, S13a tests for every case named (D22) | Decisions, Model, Event schema, Storage, S13a |
| A-F02 | A | blocker | Two databases break the atomic authorization check | Fixed: memory commits run under the hub write lock after re-reading credentials and policy (D23). No cross-file atomicity claimed. Multi-card applies are one memory transaction. Interleaving tests | Decisions, Authority across the two files, Reflect, S13a, S19a |
| A-F03 | A | major | The durability test cannot prove D3 | Fixed: `FULL` set at connection open and verified, `kill -9` kept for integrity only, VFS power-cut test with an external ledger and a `NORMAL` control, power cut in the S7 claims property test | Durability, S4a, S7 |
| A-F04 | A | major | Checkpoint control stops at the Rust connection | Fixed: a maintenance connection owns checkpoints and reports results, upstream mode turns off Python auto-checkpoint, capped read transactions, disk-pressure admission, online-backup rule, S4b tests | Checkpoints and disk, Authentication and tokens, S4b, S10 |
| A-F05 | A | major | Admission defaults cannot host 10,000 HTTP/1.1 waiters | Fixed: default profile of 4,096 connections and 3,072 waiters with 1,024 connections kept for other requests, a high-capacity profile of 12,288 and 10,000, open-file and backlog needs, S8 tests at 5,000 principals over 64 sources with memory split out | Goals, Admission and backpressure, Long-poll, S8 |
| A-F06 | A | major | Queue bounds do not bound memory or latency | Fixed: byte budgets, per-batch command, event, byte and time budgets, bounded reader and CPU queues, a separate memory quota, command deadlines with defined answers, a CPU pool, overload and slow-query tests | Process model, Write path, Read path, Admission, S4a, S4b |
| A-F07 | A | major | Long-poll wake needs a protocol | Fixed: register before read with wake epochs, principal re-check on wake, `data_version` on the writer connection, recipient-filtered external wakes, grouped re-reads, separate targets, race and storm tests | Long-poll, S8 |
| A-F08 | A | major | The proxy has no transport or failure contract | Fixed: route auth ownership, raw bodies, header rules, streaming, timeouts, no retry of writes, 502 and 504 answers, outcome-unknown on reset (D26) | Upstream proxy contract, S3b |
| A-F09 | A | major | Native `/health` can hide a dead upstream | Fixed: `/health` stays liveness, `GET /ready` added, supervisor restarts the upstream, upstream and webhook state in `/metrics` | Upstream proxy contract, Observability, S3b |
| A-F10 | A | major | The 4,600 writes/s goal has an incomparable baseline | Fixed: the gate is 10 times the Python hub under the same S6a harness and allotment, 4,600 stays a labeled release target, the harness lands first with pinned conditions, TLS reported once | Goals, S6a, S6 |
| A-F11 | A | major | Format-class storage parity discards needed values | Fixed: exact values, id mapping across references, timestamp relations, exact generations, recomputed digests, mixed-engine and migrated runs, mutation tests, and a field-specific shadow comparator | Schema, migrations and data compatibility, Route ownership, S2, S9 |
| A-F12 | A | major | The production read-only gate does not exist | Fixed: S29 builds a fail-closed read-only profile, and the full drive runs only on a copy of production | Cutover, S29 |
| A-F13 | A | major | Snapshot publication, rebuild and shared vectors are underspecified | Fixed: segmented immutable index, `indexed_seq` and `min_seq`, background rebuild with tail replay, head revalidation before push, reference-checked vector deletion, failure and restart rules, peak memory recorded | Index structures, Storage, S13b, S15 |
| A-F14 | A | major | R0 parity conflicts with the scorer | Fixed: scorer semantics copied from `memory_bm25.py`, ties on the `cards/<card_id>.md` string, per-scope visible-corpus statistics, listed goldens, no FTS5 | Lexical scoring, Retrieval pipeline, S13b |
| A-F15 | A | major | Reflect evidence is counted but not resolved | Fixed: namespaced evidence ids, attestation with credential-counted sources, freshness and revocation (S19e), eval records bound to index and policy revisions, hub auto-apply off until then (D25) | Reflect, Eval gates, S19e, Q6 |
| A-F16 | A | major | Findings lack one-report semantics | Fixed: atomic reservations with lease and generation, a report key, at-least-once stated, reassignment rule, two-session and crash tests | Duty registry and findings ledger, S22 |
| A-F17 | A | major | Dependencies and release criteria omit work | Fixed: S16 split into S16a and S16b, S18 required for trust raises and push, S12 named S12a to S12i, #2210 as S28, S4 and S13 split, pre-publication acceptance, dependency list | Build plan, Release gate, Packaging and distribution |
| A-F18 | A | major | Idempotency and compaction lack persistence rules | Fixed: `memory_idempotency` checked before compare-and-set, 30-day retention tied to spool age, required ids, `AUTOINCREMENT` `seq`, `log_epoch`, compaction invariants and the 410 cursor rule | Idempotency, Log retention and compaction, S13a, S14 |
| A-F19 | A | minor | Migration numbering and schema checks age badly | Fixed: Rust migrations start at Python's last version plus one, the check covers triggers, constraints and defaults, the lockstep test uses migrated fixtures and both SQLite builds | Schema, migrations and data compatibility, S4b |
| A-F20 | A | minor | #2207 still says Rust targets v2 from the start | Stands here: D20 (decided 2026-11-03) keeps Rust on v1 through phase 3, so the issue's plan line needs to change, which is flagged under Work in flight. The error-text part is fixed: every refusal path gets a golden in S1b | Wire contract, Work in flight, S1b |
| A-F21 | A | minor | The packaging matrix is not a hub matrix | Fixed: S10 names the workflow, manifest, validator and install changes, five-target PR builds, both toolchains, pre-publication acceptance, rustls and toml chosen, PCRE2 UTF as a capability | Packaging and distribution, S10 |
| B-B1 | B | blocker | D3 and Q2 misstate the durability baseline | Fixed: baseline stated, `FULL` on every commit of both files, marked as changing D2, cost measured with group commit, Q2 now asks whether to keep today's durability | Decisions, Durability, Evidence, Q2 |
| B-B2 | B | blocker | Shadow reads cannot run because Python's `list` writes | Fixed: Python answers with its housekeeping, Rust reads afterwards on a `query_only` reader and never writes, the route table names the housekeeping routes, native reads keep expiry visible without writing | Route ownership, Long-poll, S1b, S8, S9 |
| B-B3 | B | blocker | The phase 3 gate names a drive mode that does not exist | Fixed with A-F12: S29, and the full drive only on a production copy | Cutover, S29 |
| B-M1 | B | major | Memory `seq` can be reassigned after a crash | Fixed: memory commits at `FULL`, `log_epoch` returned on log, status and writes, clients reset on a change | Durability, Schema, Local memory and the hub |
| B-M2 | B | major | `kill -9` cannot detect the D3 failure | Fixed with A-F03 | Durability, S4a |
| B-M3 | B | major | `required.json` ignores the 15 triggers | Fixed: triggers by name and SQL, constraints and defaults checked, storage parity dumps trigger-maintained tables, the write path names the triggers Rust relies on | Write path, Schema, S2, S4b |
| B-M4 | B | major | Trust for a scan-clean `put` is unspecified | Fixed: only `decide`, policy apply and `import` raise trust, each with a clean scan, the scan never raises trust (D24), S13a drive cases, S23 push test | Trust, grants and scan state, S13a, S23 |
| B-M5 | B | major | Group commit ignores whole-transaction rollback | Fixed: autocommit check after every command, one re-apply, all-fail on a second abort, COMMIT failure path, `SQLITE_FULL` test | Write path, S4a |
| B-M6 | B | major | A Python write lock fails a whole Rust batch | Fixed: `BEGIN` busy retried with nothing applied, lock wait in `/metrics`, claims in the S6 mix, a 3 s lock test in S4a | Write path, Observability, S4a, S6 |
| B-M7 | B | major | New 503s on `/claims` read as refusals | Fixed: durable lane, 504 instead of 503 for durable commands, 502 and 504 for upstream failures, S7 claim client retries with the same lease token | Write path, Admission, Upstream proxy contract, S7 |
| B-M8 | B | major | #2210 is missing | Fixed: S28 and D27, compaction invariants recorded, recall-log retention, event payloads hold hashes only | Log retention and compaction, S28, Q7 |
| B-M9 | B | major | S19 is several PRs and assumes #1856 exists | Fixed: D15 states what main has, S19 split into S19a to S19e, release takes S19a to S19c with auto-apply off | Decisions, Reflect, Build plan, Release gate |
| B-M10 | B | major | S22 collides with the module size ratchet | Fixed: S22a adds a route seam, findings live in new modules, S11 and S28 reuse the seam | Duty registry and findings ledger, S22a, S22, S11 |
| B-M11 | B | major | A 404 cannot tell "no memory" from "memory down" | Fixed: 404 only when memory is not configured, 503 otherwise, clients spool on 503 and on 404 when they hold a replica watermark (D21) | Routes, Local memory and the hub, Rollback plan |
| B-M12 | B | major | Registration never flips in the cutover | Fixed: registration flips last in phase 3, and S5 ships in `proxy` mode | Route ownership, Cutover, S5 |
| B-M13 | B | major | The comparator ignores byte-budgeted pages, and the owners budget is uncited | Fixed: boundary results for budgeted pages, both budgets cited, golden size tests | Route ownership, Schema, migrations and data compatibility |
| B-M14 | B | major | Upstream death is undefined | Fixed with A-F08 and A-F09 | Upstream proxy contract, S3b |
| B-m1 | B | minor | S1 must not pin the raised waiter caps | Fixed: S1 pins only the per-principal cap, noted for the S1 PR | Wire contract, Work in flight, S1 |
| B-m2 | B | minor | Python runs DDL at every start | Fixed: recheck on `SQLITE_SCHEMA` and upstream restart, lockstep covers added columns | Schema, migrations and data compatibility, S4b |
| B-m3 | B | minor | Checkpoint control is partial in the window | Fixed: stated, and upstream mode closes the gap | Checkpoints and disk |
| B-m4 | B | minor | Waiters must re-check the principal on wake | Fixed | Long-poll |
| B-m5 | B | minor | Event payload bytes versus blobs | Fixed: events hold hashes only | Event schema, Storage |
| B-m6 | B | minor | Idempotency key scope and retention unstated | Fixed: `(principal, client_event_id)`, 30 days, replay rules, message `client_id` window pinned in S1b | Idempotency, S1b |
| B-m7 | B | minor | Recall-log and findings HMAC keys should differ | Fixed: domain-separated keys | Local memory and the hub, Duty registry and findings ledger |
| B-m8 | B | minor | `/findings` authentication unspecified | Fixed: node tokens and principals with a `findings` grant | Duty registry and findings ledger |
| B-m9 | B | minor | The 480 KiB owners budget is uncited | Fixed: `JSON_RESPONSE_BUDGET_BYTES` in `src/brigade/fleet_owners.py` | Route ownership, Schema, migrations and data compatibility |
| B-m10 | B | minor | "75 tables" is 76 with `sqlite_sequence` | Fixed | Schema, migrations and data compatibility |
| B-m11 | B | minor | Sealed cases conflict with a hub-agnostic drive | Fixed: golden envelopes generated once by Python and checked in with their keys | Sealed messages, S1 |
| B-m12 | B | minor | S1 and S2 run the hub with different flags | Fixed: S2 runs with defaults and separately with the outbox | Schema, migrations and data compatibility, S2 |
| B-m13 | B | minor | Phase 1 has no phase 0 baseline | Fixed: 48 hours of the S29 probe plus spool depth before phase 1 | Cutover |
| B-m14 | B | minor | Connector push needs grants nobody holds | Fixed: `grant` action, and the grant step is in S23 and S26 acceptance | Trust, grants and scan state, S23, S26 |
| B-m15 | B | minor | The hub's scan rule set is unspecified | Fixed: bundled default rules only, version pinned, repository allowlists do not apply, a second scan at prompt load | Content scanning |
| B-C1 | B | contradiction | Q2 called `NORMAL` the first decision while the shipped hub runs `FULL` | Fixed with B-B1 | Decisions D2 and D3, Q2 |
| B-C2 | B | contradiction | Shadow required purity that messages reads lack | Fixed with B-B2 | Route ownership |
| B-C3 | B | contradiction | The phase 3 gate named a missing drive mode | Fixed with B-B3 | Cutover |
| B-C4 | B | contradiction | D15 "Decided" against no tier code on main | Fixed with B-M9 | Decisions, Reflect |
| B-C5 | B | contradiction | Registration native in S5 but never flipped | Fixed with B-M12 | Cutover |
| B-C6 | B | contradiction | Event payload bytes against blob deduplication | Fixed with B-m5 | Event schema, Storage |
| B-C7 | B | contradiction | 75 tables against 76 | Fixed with B-m10 | Schema, migrations and data compatibility |
| B-C8 | B | contradiction | "Clients change nothing" against the connectivity slices | Fixed: goal 1 is scoped to the cutover and names S23 to S26 | Goals |
