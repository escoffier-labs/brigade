# brigade-hub

A Rust HTTP front for the Python Fleet Hub. Operators run it through
`brigade fleet serve --engine rust` (S10), which migrates the database, checks
the binary against its recorded checksum and supervises it. Install it with
`brigade setup --component brigade-hub` (Linux only). Cutover and rollback:
[docs/runbooks/fleet-hub-rust-engine.md](../../docs/runbooks/fleet-hub-rust-engine.md).
`brigade-hub --version` prints the version and starts nothing. Install the matching Python package
in the selected interpreter, or set `BRIGADE_HUB_PYTHON` to its interpreter.
Python children run with `-I` from `/`, so `PYTHONPATH` and the working directory
do not supply imports. For source installs, use an editable install with
`python3 -m pip install -e .` from the repository root. The managed upstream
runs the installed `brigade` module with `-m`. Interpreter settings with a path
separator resolve against the original working directory. Bare names such as
`python3` retain PATH lookup.

Relative `BRIGADE_FLEET_DECK_CONFIG` and `BRIGADE_FLEET_FINDINGS_KEY_FILE`
paths resolve against the original working directory before Python starts.
`--deck-config` takes precedence over its environment setting. Findings key
paths expand `~`, and a whitespace-only findings key setting uses the default.
If the original working directory is gone, relative startup paths fail with an
error. When every active startup path is absolute, the hub can fall back to `/`.

The benchmark harness requires an editable install from this checkout and
rejects interpreters that load another Brigade copy.

```sh
brigade-hub --host localhost --token-file ./admin-token --db ./hub.db
```

Managed mode launches Python on a numeric loopback ephemeral port. It creates
a random per-start key, passes it only to the child, and requires it on every
upstream request before routing. The managed switch must equal that key.
Every upstream SQLite connection disables automatic WAL checkpoints and reads
back the setting. Managed Rust sets `BRIGADE_HUB_CHECKPOINT_OWNER=rust` on its Python child,
which suppresses Python's checkpoint thread. Rust maintenance is the sole
checkpoint owner in this mode. Without Rust maintenance, a keyed upstream
keeps its Python checkpoint thread. Standalone Python keeps automatic
checkpoints. External `--upstream` mode starts no Rust maintenance, so its
operator must retain the upstream's checkpoint owner.

Rust resolves the database path through the matching Python runtime before
starting either storage or the managed child, using `Path.home()` for the
default and `expanduser()` for an explicit path. Both processes receive the
same absolute path, including when `HOME` is unset.

Rust replaces incoming internal key and client-address headers with its key
and the TCP peer address. Every Python request address consumer uses that
validated address, including browser trust checks and request logging.
Persisted per-request address auditing is deferred to a later schema slice.

With every family proxied, the front exits nonzero if its managed child dies,
and a service manager restarts the pair. Once a family other than `health`
and `metrics` is native, the front restarts a dead child instead, after 1
second and then doubling to 30 seconds, back to 1 second after a run of 60
seconds, with a fresh ephemeral port and the same per-start key. Native routes
keep serving meanwhile. Proxied routes answer 502 `upstream-down`, `/ready`
answers 503, and webhook delivery, which runs in the Python child, pauses.
`/metrics` reports `brigade_hub_upstream_up`,
`brigade_hub_webhook_delivery_paused` and
`brigade_hub_upstream_restarts_total`. `/health` retains the v1
liveness bytes and server header. `GET /ready` reads a background health probe that runs every
second on its own connection with a one-second deadline and returns only `{"ready":true}` or `{"ready":false}`,
with status 200 or 503.
Shutdown stops accepting, closes idle keep-alive connections, answers every
native long poll with its current page, lets in-flight requests finish for at
most 25 seconds, then stops the child, which gets 10 seconds for its own
shutdown. A container stop grace period must exceed 35 seconds.

`--upstream http://<numeric-loopback>:<port>` uses an existing Python hub.
Its operator must configure matching token, database, deck, sealing and trust
settings. Validation checks the front's supplied settings, not the upstream's
identity, and may initialize or migrate the supplied database. Supply `--db`
explicitly. External mode retains Python's per-source cap. Its lifecycle is
not managed here.

Connect failures return 502 `upstream-down`. Only bodyless GETs receive one
retry, only after a connect failure. Other transport failures return 502
`upstream-outcome-unknown`, since Python may have committed. Header timeouts
return 504. Connect timeout is 1 second, response header timeout 30 seconds,
response idle-body timeout 30 seconds and total response timeout 120 seconds.
The header timeout starts after the request body is buffered. For message lists the header deadline is the body
`wait_ms`, capped at 20 seconds, plus 5 seconds. The original bytes pass unchanged.
After response headers have reached a client, a body failure terminates the
stream. HTTP cannot replace that status with a 504. Clients must treat an
incomplete response as a transport failure.

Limits: 4,096 connections (`--max-connections` overrides), with 256 per TCP
source except a loopback peer of a loopback bind. D29 treats that peer as the
HTTPS front. Global connection limits still apply. A non-loopback bind keeps
the per-source cap even for loopback peers.

Before reading a body, Rust validates bearer digests against active node and
message-principal rows through the shared query-only reader pool. Admin digest
comparison uses constant-time equality. Revocation takes effect on the next
lookup. Lookup and pool queue wait together have a two-second deadline.
Lookup failure returns 503 `credential-lookup` with `Retry-After: 1`.

Requests have separate credential, browser and anonymous budgets. Validated
credentials admit 64 requests each and hold at most four of the 16 credential
proxy slots. Browser requests share four proxy slots and anonymous requests
share four. Unvalidated credentials share the anonymous budget of eight
requests, or the browser budget of four on browser routes. Trusted browser
logins hold at most two browser requests. Enrollment is anonymous.

Bodies have an 8 MiB limit and a 30-second read deadline. The 64 MiB request
budget is split into 61 MiB for credentials, 2 MiB anonymous and 1 MiB browser.
One credential reserves at most 16 MiB. Reservation waits at most one second.
A request without a validated credential declaring more than 256 KiB forwards
headers without body bytes, then closes the connection after Python answers.
Python's route-specific status and body survive. Body bytes are released when
upstream response headers arrive. Proxy slots remain held through the response.

JSON responses up to 8 MiB buffer before downstream headers. A separate
64 MiB response budget waits at most one second, then falls back to streaming.
`GET /metrics` reports `proxy_buffer_fallback_total`. Browser and oversized JSON
responses stream with backpressure. A buffered failure returns 502 or 504.
A streamed failure closes the connection and preserves `Content-Length`.

Python remains the route and authorization authority. Authorization and Cookie
pass unchanged, duplicate Set-Cookie headers survive, and hop-by-hop headers
are removed. Identity and scheme headers are stripped unless explicitly trusted
on browser routes. Incoming internal client-address headers are always dropped.
Request and response bytes pass unchanged, without JSON re-encoding.

Releases publish `brigade-hub-linux-amd64` and `brigade-hub-linux-arm64`
after `tests/prepublish.py` passes on each target. The binary supervises its
Python child with Linux process controls (`PR_SET_PDEATHSIG`), so macOS and
Windows builds wait on a port of that supervision.

Verification: execute `engines/hub/verify` through `brigade work verify run`.
The frozen contract drive launches the managed front with:

```sh
registry/skills/verify-brigade/control-brigade.py hub-contract --hub-cmd \
  'engines/hub/target/release/brigade-hub --host {host} --port {port} --db {db} --token-file {token_file}'
```

### Route modes and observations (S9)

Repeat `--route-mode family=native|shadow|proxy` to override a family. The last
value wins. `health` defaults to native and supports proxy and shadow on
`GET /health`. `/ready` always stays local. `metrics` requires native mode.
`registration`, `claims` and `messages` accept native or proxy. `memory` accepts native or proxy (see below).
`heartbeat`, `cold` and `browser` currently accept proxy only. Selecting an unavailable native or shadow handler refuses
startup. Native handlers and housekeeping audits for those families land in
S5 to S8 and S12. Long-poll requests cannot be shadowed by this slice.

`GET /metrics` requires the admin bearer token. Node, principal, cookie and
query credentials do not grant access. It serves metrics text version 0.0.4.
Writer queue depth, batch size, apply time, commit time, lock wait and aborted
batches read the writer's atomics when the process starts a writer and passes
`Writer::stats()` to `Telemetry::attach_writer`. They are gauges for the last batch
the writer finished, except aborted batches, a counter. Lock wait runs from taking a
command off the queue to holding the hub write lock, the arbiter turn included, and
is recorded when the wait fails too. The front runs as a pure proxy and starts no
writer, because the Python upstream commits every write, so these six stay `NaN`
with a help line that says so. Reader queue depth sums the jobs waiting in the
front's reader pools, and CPU queue depth reads the CPU pool. Both are 0 when idle,
and `NaN` only where no pool is attached. Each is a single unlabeled sample with no
`# TYPE` line, as S9 published it. Checkpoint metrics use S4b's samples when this
front owns maintenance, including busy results, failures, WAL frames,
checkpointed frames, WAL bytes and outstanding frames (`checkpoint_stall_frames`).
The external-upstream mode has no local checkpoint sampler and reports an
unavailable stall value. Long-poll, memory and webhook metrics arrive with their
own handlers. S9 itself changed no writer or bench code.

Shadow comparisons use the shared `hub-shadow-rules.json` drive policy. Stored
values are exact. A dedicated read-only, query-only watch connection samples
`data_version` before forwarding and after comparison. A stable mismatch is
definite. With intervening commits, at most two more reads distinguish a
transient match from an inconclusive mismatch. Boundary and projection helpers
require handler evidence for budget exhaustion or purge eligibility. Health
has neither exception. Mismatch JSON logs contain status codes, result, first
JSON pointer and SHA-256 body digests, with no bodies.

Request JSON logs count streamed bytes through completion or disconnect and
include a generated request ID, family, status, elapsed seconds and credential
kind. Labels and logs omit request queries, identities and bearer material.

S5 registration handlers run with `--route-mode registration=native` (proxy by
default). This serves `GET /nodes`, `POST /nodes` and `POST /message-principals`
natively. Registration flips last in cutover phase 3. The Python hub remains the schema owner throughout rollback.

S7 claims handlers run with `--route-mode claims=native` (proxy by default).
This serves `GET /claims` and every `POST /claims` action natively, with
generation fencing, retired holders, lease expiry and `claim.expired` events
through the same writer as registration. Three cases stay on the Python
upstream. `GET /owners` joins policy, sessions and Grok Bot state. A
`GET /claims` while Grok Bot leases are active needs Python's display
projection. A `POST /claims` whose target is not ASCII needs Python's
`repr()` of the target in its refusal text.

S8 messages handlers run with `--route-mode messages=native` (proxy by
default). This serves the `POST /messages` actions `send`, `reply`, `ack`,
`list`, `sent`, `sent_lookup`, `count`, `identity`, `keys_get`,
`manifests_get` and the three `question_*` actions through the same writer as
registration and claims, with sealed envelope checks, sender and fleet storage
quotas, `message.created` events and the `BRIGADE_FLEET_MESSAGES_*` storage
policy plus `--require-sealed`. Writes run Python's retention housekeeping in
the same transaction, its 32-row sealed purge included, but delete at most
1,024 expired messages per command where Python deletes all of them at once.
Reads already hide expired rows, so a backlog, for example after a shorter
retention window, drains over later commands with the same visible results
and never holds a command past the writer deadline. Reads never write:
they hide expired rows, show a `reply_to` to an expired parent as null, and
show every sealed row acknowledged at least 7 days ago as its tombstone, so a
native read never returns ciphertext past its lifetime, even with a purge
backlog. A stored sealed row too corrupt to make a tombstone reads as
`content_expired` with no envelope once its lifetime ends. The maintenance
thread runs the same retention as a writer command at most once a minute,
with a 256-row sealed purge and at most 1,024 expired messages deleted per
command, and pauses under the read-only audit.

A `list` with `wait_ms` waits natively: the recipient's wake epoch is marked
before every read, native commits move it after they commit, and woken
waiters re-read in groups of up to 256 per reader job. While anyone waits,
or a poll that may wait is still in its first read, a watch thread on its own
read-only connection samples `PRAGMA data_version` every 250 ms and wakes the
recipients of messages another process committed. Native commits also move
that connection's `data_version`, so they get a second, harmless notify. A
waiting `list` holds no request body and no body-byte reservation. Waiter
caps: 2 per principal, 64 per source except the front, and 3,072 in total at
the default 4,096 connections, which keeps 1,024 for requests that do not
wait. Only waiters count: idle keep-alive connections never refuse a wait.
A principal revoked, or given a new token, while it waits gets the
`message_sender_inactive` error on its next re-read and no rows. The token
check is stricter than the Python hub's `list`, which checks only that the
principal is active.

These stay on the Python upstream:

- `who` and `claims`, which build paging snapshots over sessions and claims.
- `keys_publish`, whose stored record keeps the client's key order, which
  serde_json does not.
- Every `events_*` action.
- Sealed sends while a hub identity is installed, which need Python's checks
  of the signed identity metadata.
- An `enqueued_at` outside the common
  `YYYY-MM-DDTHH:MM:SS[.fff|.ffffff][Z|+HH:MM]` forms.
- Bodies serde_json reads differently from `json.loads` (an integer `-0`, or
  `NaN`, `Infinity`, `1e400` and lone surrogate escapes). For `list`, `sent`
  and `sent_lookup` this has two costs. A `list` with `wait_ms` waits on
  Python's own long poll, which hears only Python's commits, so a native send
  to that recipient reaches the poll at its deadline instead of at once. And
  Python answers with its 32-row sealed purge, so with a purge backlog above
  32 rows those reads can return ciphertext past its 7-day lifetime.

Under the read-only audit the native families read `HUB_AUDIT_CLOCK` when `--test-clock-file` is absent,
and the audit traces every write statement a native connection runs.

Hub memory (S13a) runs with `--route-mode memory=native`. This serves
`POST /memory` from `fleet-hub-memory.db` beside the hub database, which the
Rust hub creates on first start and owns (Python never opens it). Without a
`memory` route mode, `/memory` is an unknown path and the Python upstream
answers `404 {"error": "not found"}`, which clients read as "this hub has no
memory". `memory=proxy`, or a memory file that cannot be opened or migrated,
answers `503 {"error": "hub busy", "reason": "memory-unavailable"}` while the
rest of the hub keeps serving. Every memory write commits while the memory
writer holds the hub file's write lock, after re-reading the caller's
credential (D23). The content scan hook reports unavailable until S18, so no
write raises a card's trust, and `import` answers `503 memory_scan_unavailable`.
After restoring the memory file from a copy, start a new `log_epoch` before
serving it (`brigade_hub::memory::schema::rotate_epoch`, which S31's restore
tool calls). `/metrics` adds `brigade_hub_memory_*` lines for batches,
hub-lock residence and wait, authority timeouts and the residence bucket.

`search` (S13b) answers from an in-memory lexical index that the hub builds
from the memory file at start and the memory writer updates after every
commit. It scores with BM25 (k1=1.2, b=0.75), using the local scorer's
tokenizer and card fields, over the live cards in the scopes the caller can
read, and breaks ties by `cards/<card_id>.md`, then scope. A write answers
with its `seq`. A `search` with `min_seq` waits up to 500 ms for the index to
reach it and otherwise answers with `retrieval.stale: true`. If the index
cannot follow a commit, searches keep the last good snapshot, marked stale,
until a background rebuild from the store replaces it. A failed rebuild
retries on its own, starting at 100 ms and doubling to 30 s, so a card
retired during an index failure leaves search without waiting for another
write, and a new index failure during that backoff starts the next try at
once. Each commit adds one delta segment. Whenever the newest 8 deltas share
a merge level, they merge into one delta a level up, from their postings
alone, so small commits never rebuild the main segment. Past 64 deltas,
every delta merges into one. A rebuild starts once delta documents pass 10%
of the main segment or once a quarter of the indexed documents are dead
(each needs at least 256 documents). When the hub stops, it stops and joins
the rebuild thread before the memory writer closes, so no thread keeps the
memory file open after it. `/metrics` adds the committed and indexed `seq`, the lag
between them, the stale flag, segments, documents, publications, failures,
rebuilds and the last build's time.

The query is refused with `memory_query_invalid` when it is over 4,096 UTF-8
bytes as sent (a guard on normalization's work, with its own message) or
over 256 characters after normalization. A query with no word characters is
matched literally, as the local scorer does, and every match scores 0.0. The
hub reads the visible cards in answer order (`cards/<card_id>.md`, then
scope), up to 1,000 cards or 4 MiB of card text. When more cards are visible
than that budget, it also stops at `limit` matches. Its `matches` are the same
as a full scan's first `limit`. When every visible card fits the budget,
`match_count` is exact, as in the local scorer. A scan that stops before the
last visible card answers `retrieval.truncated: true`, and then `match_count`
counts only the matches it read, so it is a lower bound.

The Unicode tables in `src/memory/pytables.rs` and the goldens in
`tests/lexical-golden.json` come from Python 3.12, the version the Hub
workflow pins, and R0 parity holds against Python 3.12. Brigade runs on
Python 3.11 and later, and a local search on another minor version can
tokenize characters that version's Unicode data classifies differently. After
a change to the local scorer or a new pinned Python minor version, regenerate
both with `PYTHONPATH=src python engines/hub/tests/lexical_golden.py --write`
from the repository root. `tests/memory.py` checks that they are current and
runs gate R0 (same ids, order and scores as `bm25`) on
`evals/memory-retrieval`.

Front-matter lists go through a copy of the subset of `ast.literal_eval` that
cards use, including base-prefixed integers, lone surrogate escapes (shown as
U+FFFD), trailing `#` comments and CPython's limit of 200 nested brackets.
Where it differs, the hub keeps the raw text: a `\N{name}` escape, because
the hub ships no Unicode name table, and a base-prefixed integer over 4,300
decimal digits, where Python raises `ValueError`. A card with such a line can
score differently from the local search. A surrogate pair written as two
escapes, as `json.dumps` writes 🎉, is two lone surrogates to Python and two
U+FFFD to the hub, so the hub's `tags` show `��` where a local search shows
🎉. Tokens and scores are the same. Joining the pair for display alone would
need a second copy of each tag, because the literal match must still see two
characters as Python does.

Acceptance drives can use `--test-clock-file PATH` to share a deterministic
clock with the managed Python upstream. This flag is for throwaway fixtures.

### Native heartbeat (S6)

`--route-mode heartbeat=native` serves `POST /events`, `GET /status`,
`POST /sessions` and `GET /sessions`. The default stays `proxy`. Shadow mode
is refused until a heartbeat shadow audit exists. The native commands share
one hub-file writer with registration, claims and messages. Event and session
admission is non-durable, with the durable queue reserve left for claims and
credentials. Every batch takes queued durable commands first, so a claim waits
for the batch in flight rather than every heartbeat ahead of it. Each class
stays in arrival order. A heartbeat that has waited 5 s, half its budget, at
the head of the ordinary queue (a sweep: 125 ms) runs first in the next batch,
one per batch, and one whose deadline passes is answered `503 writer-timeout`
at the next pick. So sustained claim load delays heartbeats but cannot leave
them unanswered. Every commit still uses `FULL`.

An event object or an array of up to 1,000 events validates before insertion.
Dedupe keys, receipt timestamps, optional fields and `run.failed` outbox rows
follow Python. A refusal rolls its command back. The writer rechecks the schema
version and caller under its write lock. Local sessions retain their start time
while active, end a prior checkout identity on a Claude or Codex move, and keep
T3 provider links when an update omits the link field. Reads project active and
historical sessions, cloud presence, last known seats and stale T3 observations.

Compatibility fallbacks remain on the matching Python upstream: event bodies
with execution context, non-ASCII or control-bearing node identities, negative
integer zero and integers beyond SQLite's range, cloud session writes, session
requests with keys needing Python string representation, JSON spellings that
serde rejects, token query parameters, and status rows with exotic datetime
spellings. Cloud session reads are native. These fallbacks preserve the existing
validators and error bytes and are not a claim that every heartbeat input has
been ported. Port the extended validators before requiring heartbeat to survive
upstream failure for those inputs.

The S6 load procedure and deployment observation are in
[bench/README.md](bench/README.md). `writer_commit_duration_seconds` adds
cumulative histogram buckets for every COMMIT attempt, failed commits included.
Subtract the buckets at the run boundaries to bound commit p99. The existing
last-batch gauges keep their meaning and cannot measure every commit.
