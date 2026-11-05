# brigade-hub (S3c)

A Rust HTTP front for the Python Fleet Hub. Run where `python3 -m brigade`
resolves this matching Python package, or set `BRIGADE_HUB_PYTHON` to its
interpreter.

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

Live deployment remains outside this slice. S3c owns upstream mode and client
address forwarding. S10 owns packaging and deployment recipes.

The front exits nonzero if its managed child dies. `/health` retains the v1
liveness bytes and server header. `GET /ready` reads a background health probe that runs every
second on its own connection with a one-second deadline and returns only `{"ready":true}` or `{"ready":false}`,
with status 200 or 503. A service manager must restart the pair after failure.
Shutdown stops accepting, closes idle keep-alive connections, lets in-flight
requests finish for at most 25 seconds, then stops the child. A container stop
grace period must exceed 25 seconds.

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

Deferred: S4 storage, S8 native waiters, S9 metrics and route modes, and S10
packaging and deployment recipes.

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
`registration` and `claims` accept native or proxy. `memory` accepts native or proxy (see below).
`heartbeat`, `messages`, `cold` and `browser` currently accept proxy only. Selecting an unavailable native or shadow handler refuses
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
scope) and stops at `limit` matches, 1,000 cards or 4 MiB of card text,
whichever comes first. Its `matches` are the same as a full scan's first
`limit`. A scan that stops before the last visible card answers
`retrieval.truncated: true`, and then `match_count` counts only the matches
it read, so it is a lower bound.

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
