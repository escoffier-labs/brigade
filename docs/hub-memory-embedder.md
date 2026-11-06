# Hub memory embedder

The native hub reads `[memory]` from `hub.toml` beside its database. Use
`--memory-config PATH` to select another file. The matching Python runtime
reads TOML at startup, in isolated mode (`python3 -I`), and only when the
memory route is native. A missing default file means lexical mode. If the
default file cannot be read (no `python3`, Python older than 3.11, or bad
TOML), memory answers as unavailable and the rest of the hub starts. The
same holds for hybrid settings in the default file that do not validate,
such as a missing token file or an unknown `retrieval` value, and the hub
log names the reason. A file named with `--memory-config` that cannot be
read or does not validate stops startup with the cause. The lookup has a
10 s deadline covering the existence check and the Python child, so a FIFO
or a stalled mount cannot hold startup. On timeout the child is killed. A
`retrieval` key that is present must be a string. Only an absent key means
lexical. Token values stay in the hub process.

```toml
[memory]
retrieval = "lexical"
```

Lexical mode ignores a leftover embedder table, including an unreadable token
file, and prints one INFO message about the unused table. It starts no
embedding scheduler, makes no embedding request and loads no dense matrix.

The transport configuration for hybrid retrieval is:

```toml
[memory]
retrieval = "hybrid"

[memory.embedder]
backend = "remote"
endpoint = "https://embed.example.com/v1/embeddings"
allowlist = ["https://embed.example.com/v1/embeddings"]
token_file = "embedder.token"
shape = "v1/embeddings"
name = "example-model"
model_digest = "operator-pinned-model-digest"
dimensions = 4096
input_chars = 6000
query_prefix = ""
query_prefix_version = 1
```

The token file must be a regular file owned by the hub's effective user with
mode 0600 or 0400 on Unix. The hub opens it nonblocking, so a FIFO fails
that check instead of holding startup. A relative path is relative to the configuration
file. The allowlist names exact URLs.
Redirects are refused. Plain HTTP resolves once per request and connects
only to the checked private or loopback address. Public endpoints require
HTTPS with certificate verification through rustls. `local` uses the same
HTTP contract for a loopback server or an allowlisted internal service, and
the hub holds it to those destinations. Plain HTTP passes the pinned check
for a private or loopback address. HTTPS needs a loopback or private IP
literal, because its resolver connects on its own. A name over HTTPS is
refused for `local`, an allowlisted public URL included. Use `remote` for
that endpoint.
The operator owns the endpoint bind, private-network ACL and server-side
token enforcement. On `http://`, which the hub accepts only for private
ranges, the bearer token crosses the LAN in cleartext. The allowlist lives
in the same hub.toml as the endpoint, so it catches typos, not tampering.
The hub keeps up to two idle connections per endpoint for 30 s, so most
queries skip the TCP and TLS handshake. Plain HTTP pools by the pinned
address, so a reused connection always goes to the address that passed the
check.

Choose `api/embed` or `v1/embeddings` for `shape`. Both send a JSON `model`
and an `input` array. Every successful response must include the pinned
`model_digest` JSON field or `x-model-digest` header. When both appear,
both must match. A model name alone is insufficient. The embedding server
or its authenticating front must provide the digest. Vectors must have the
configured dimensions, finite values and a nonzero norm. The hub normalizes
them before use.

Queries have a 250 ms deadline including connection and response parsing.
Documents have a 30 s deadline. A query failure opens the breaker for 30 s.
One background probe uses a 10 s deadline and closes it early on success.
The fifth concurrent query falls back immediately. Document batches contain
at most four texts and 32 KiB. A document request cannot start while a query
is in flight or during the following second. Document contention can cause
a query fallback without opening the breaker. Document retries start at
one second, double to 60 seconds, and honor a bounded numeric Retry-After.

The S18 scanner checks every outgoing payload, and only a clean result
leaves the host. A version stored as `pending` or `flagged` is never
eligible for documents, and a query the scanner blocks or flags sends no
text. Scanner unavailability prevents all documents and every query whose
connect address is not loopback. Plain HTTP checks the pinned address after
resolution, so `localhost` that resolves to a LAN host gets no exemption.
HTTPS gets one only for a loopback IP literal.

Text that scans private (S18) goes only to a loopback endpoint, by the same
rule as the unscanned exemption: a loopback literal, or plain HTTP
`localhost` whose pinned address is loopback. That rule applies in push mode,
where the hub embeds in its own process. For that endpoint a `private`
receipt is queued, leased and embedded like a clean one. For every other
endpoint, including a private-network address, a private card is not queued by
`put` or `memory reindex` and is not leased. A job whose lease-time scan finds
private text there is parked as `content-private`. That reason is not
transient. A worker that reports `content-private` or `content-blocked` for a
lease parks the job at once, without the retries that re-lease the text.
`embed-worker` follows the same rule, and reports a private lease as
`content-private` unless its own endpoint is a numeric loopback address. Pull
mode has its own rule, under Pull worker below.

A query that succeeds clears an earlier fault, even when the probe failed,
and an expired breaker window counts as closed. Status leaves `degraded`
once the embedder answers again.

S15 is split into two stacked PRs. S15a supplies the HTTP boundary and
lexical mode. S15b supplies the following storage and retrieval behavior.

## Jobs and switching

The hub starts one scheduler only in hybrid mode. At start-up it queues
missing content hashes in bounded writer turns. After that, `put` and
`retire` queue and remove jobs inside their own transactions, so the
scheduler never scans the corpus. It leases only while work is queued. An
empty lease doubles its wait up to 30 seconds, and a committed `put` wakes
it. It takes no lease while the endpoint is backing off. Matching vectors are
reused across revisions and scopes. Query results remain lexical until the
configured fingerprint covers every embeddable live head. Status reports
switching during the backfill, with `coverage` fields `cards`, `eligible`,
`unembeddable` and `total`. A card is unembeddable when its scan receipt is
not clean (a `private` receipt counts as clean for a loopback endpoint in push
mode, and in pull mode only when `pull_private` is on), or when its job is
parked. Unembeddable cards are answered by BM25
alone and do not hold the switch to hybrid, with one exception. When no card
has a vector and every card that could be embedded has parked on an embedding
failure, which is what an endpoint that always answers the wrong row count
produces, status reports `degraded` with reason `all-embeddings-failed` and
searches answer from BM25 alone. A card parked by a scan verdict does not count
as a failure. Run `memory reindex` once the endpoint is fixed to requeue the
parked documents. While the loaded matrix has no live row, because the hub has
no card to embed, every embeddable card is unembeddable, or every vector was
collected, status reports `effective: lexical` with state `ok`. Search answers
from BM25 alone and does not send the query text to the endpoint. Covered cards
with an empty matrix report `switching` until the matrix catches up. A vector
committed while the first matrix load scans joins the matrix as soon as that
load finishes, without waiting for another write. In hybrid answers, `match_count`
and `truncated` keep their lexical meaning. A failed embedding leaves the lexical index
available. Changing back to lexical and restarting leaves vectors on disk,
and loads none of them.

Status reports `dense-not-loaded` only before `start_retrieval` returns. After
that, a matrix is installed or the load failed, so production status shows
`dense-load-failed` when the load fails. A retry loads the matrix without
holding the lock the writer needs, so a slow retry does not delay writes. An
empty matrix answers searches from BM25 alone and never sends query text to the
endpoint.

Derived tables migrate the memory file from schema 1 to schema 2. Triggers
on `memory_vectors` append to `memory_vector_log`, and the dense index
replays only the log rows after its watermark. A commit that touches no
vector reads no vector row. The sweep keeps the newest 4,096 log rows, and a
matrix behind the kept rows reloads its embedder through the
`(embedder_id, normalizer_version, content_hash)` index. The hub
keeps vectors under a fingerprint of model name, reported digest, dimensions,
normalizer version and query prefix version. Bump the prefix version when
changing the prefix. Vectors use little-endian float32. Immutable append
segments share older rows with retained snapshots. Compaction captures an immutable snapshot and its watermark after deltas
exceed 10% of the first segment. A bounded background compactor builds the new
main matrix outside the writer locks. It returns the result to the memory
writer, which replays committed vector additions and removals before atomic
publication. Metadata records which rows still have vectors, so deletion
never copies the whole matrix on the writer. A mode switch or drop
invalidates a pending build. A query whose live head has no matching vector
continues using BM25.

Normalizing document text uses title, summary and body. Unrelated frontmatter
and card aliases do not enter the payload. Flagged text never leaves the
host. The stored scan receipt must be clean, and S18 scans the exact
outgoing payload again at lease time. Text that this scan flags or blocks is
parked as unembeddable. Pending revisions are not queued.

A document that fails is retried alone, so it cannot hold back the documents
leased with it. Each failure of a document leased alone counts an attempt
and backs off for 10 seconds, doubled per attempt up to one hour. After five
attempts the job is parked with `state = 'failed'` and `last_error`. Endpoint
faults (unavailable, timeout, refused credential, digest mismatch, busy)
release the job without counting an attempt. An endpoint answer of 400, 413
or 422 is a document refusal. Embedding commands write no idempotency row,
because a lease reply carries card text. Retiring or replacing a head removes its old lineage rows.
A shared vector stays until no live head in any scope refers to its content.
Finished jobs expire after seven days through the bounded writer sweep.

Use `brigade memory reindex --target WORKSPACE --admin-token-file FILE` to
queue missing vectors. The command drains bounded pages, and repeating it
queues nothing after those pages exist. Its queued count describes the first
page. The command also requeues parked jobs. The explicit admin credential
is separate from the node credential. Use `--drop EMBEDDER_ID` to remove
only that embedder's vectors, lineage and jobs. The id must be 64 lowercase
hex characters. The drop runs in writer turns of at most 512 rows per table
and reports the deleted `vectors`, `jobs` and `lineage` counts. The hub
refuses to drop the active embedder while hybrid is configured. Set
`memory.retrieval = "lexical"` and restart first. Old matrices stay
available until explicitly dropped.

## Pull worker

Set `pull = true` in `[memory.embedder]` to have the hub queue jobs without
making document requests. A GPU host can then run:

```text
brigade memory embed-worker --target WORKSPACE --config worker.toml
```

The worker configuration has the same model identity, dimensions, prefix
version, shape and token file as the hub's model configuration. Its endpoint
must be an allowlisted numeric loopback URL on the worker host. The worker
holds a hub node or principal credential with only the embed grant on the
scopes it serves. It needs no read grant and never opens the hub database.
Each lease names at most four scanned texts and expires after 60 seconds.
The worker verifies the endpoint digest and vectors, then submits base64
little-endian float32 rows with the lease id. The hub checks ownership,
expiry, dimensions and digest again. When a batch is refused, the worker
embeds each document alone, submits the vectors it got, and reports the rest
with `embed_failed` (`lease_id` and up to four `content_hash` and `reason`
pairs, plus `alone: true` on a document it already tried by itself, which
gets the solo-failure backoff). A lease reply carries `parked`, the number
of texts the lease-time scan parked. It keeps leasing after per-document
failures. While the endpoint is down it sleeps 1 s, doubling to 60 s, and a
good step resets that. It also sleeps 1 s when a lease is empty. `--once` processes at most one lease. A
worker that stops leaves the lease available for recovery after expiry.

### Private cards in pull mode

In pull mode the hub's `endpoint` or `query_endpoint` is only the query
embedder. Documents go to whichever worker leases them, so that endpoint says
nothing about where private text lands, and the hub does not use it for the
private gate. The default is closed: private cards are never queued or leased,
and `coverage` counts them as unembeddable, whatever the query endpoint is.

```toml
[memory.embedder]
pull = true
pull_private = true
```

`pull_private` is a bool that defaults to false. A non-bool is a
configuration error, and so is `pull_private = true` without `pull = true`.
Turning it on asserts that every worker holding the embed grant runs on the
operator's own machines. The hub then queues and leases private text to any
credential with that grant, and trusts the operator to have kept the grant
inside that boundary. The worker's own check still applies: `embed-worker`
sends private text only to a numeric loopback endpoint and reports
`content-private` for anything else. `memory status` shows `pull_private on`
or `off` in the retrieval line for a pull hub.

For reachable query embedding, configure `query_endpoint` or `endpoint` on
the hub under the same allowlist and secret-file policy. Pull mode can omit
both endpoints and the model secret. It then answers lexical fallback with
embedder-unavailable, even when document vectors are complete. Only a local
loopback model is admitted by the Python pull worker. The Rust hub's remote
backend supports allowlisted private HTTP and HTTPS endpoints.

## Measurements

At 20,000 synthetic rows, a matrix rebuild with its old snapshot retained
recorded 175,848 KiB peak RSS at 1,024 dimensions and 655,908 KiB at 4,096
dimensions. These are isolated matrix-process measurements, including test
runtime overhead, without a lexical index, SQLite corpus or embedding model.
The larger rebuild exceeds the reference 512 MiB allotment. Production
memory planning must include those other components and any second model.

A fresh private lexical run used the retained 1,891-card fixture and three
complete label sets. S18 admitted 1,344 cards, blocked 546, and one card id
was invalid. Native prompt hit@5 was 0.483871, 0.612903 and 0.531250 over
62, 62 and 64 labelled cases. Python BM25 on the imported corpus agreed
with every native top-five list. The full-corpus Python reference was
0.661290, 0.741935 and 0.703125. The import run therefore does not establish
full-corpus end-to-end quality parity. Receipt:
`20261105-235600-work-verify-0119a4`. No model server was contacted.
