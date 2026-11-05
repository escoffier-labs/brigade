# Hub memory embedder

The native hub reads `[memory]` from `hub.toml` beside its database. Use
`--memory-config PATH` to select another file. The matching Python runtime
reads TOML at startup. Token values stay in the hub process.

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

The token file must be a regular file with mode 0600 on Unix. A relative
path is relative to the configuration file. The allowlist names exact URLs.
Redirects are refused. Plain HTTP resolves once per request and connects
only to the checked private or loopback address. Public endpoints require
HTTPS with certificate verification through rustls. `local` uses the same
HTTP contract for a loopback server or an allowlisted internal service.
The operator owns the endpoint bind, private-network ACL and server-side
token enforcement.

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

The S18 scanner checks every outgoing payload. A pending version is never
eligible for documents. Blocking query findings send no text. Scanner
unavailability prevents all documents and every non-loopback query.

S15 is split into two stacked PRs. S15a supplies the HTTP boundary and
lexical mode. S15b supplies the following storage and retrieval behavior.

## Jobs and switching

The hub starts one scheduler only in hybrid mode. It queues missing content
hashes in bounded writer turns. Matching vectors are reused across revisions
and scopes. Query results remain lexical until the configured fingerprint
covers every live head. Status reports switching with covered and total card
counts during the backfill. A failed embedding leaves the lexical index
available. Changing back to lexical and restarting leaves vectors on disk,
and loads none of them.

Derived tables migrate the memory file from schema 1 to schema 2. The hub
keeps vectors under a fingerprint of model name, reported digest, dimensions,
normalizer version and query prefix version. Bump the prefix version when
changing the prefix. Vectors use little-endian float32. Immutable append
segments share older rows with retained snapshots. Compaction copies rows
into a new main segment after deltas exceed 10% of the first segment. Work
runs on reader-pool threads outside the writer locks. A later refresh picks
up writes committed during that read snapshot. A query whose live head has
no matching vector continues using BM25.

Normalizing document text uses title, summary and body. Unrelated frontmatter
and card aliases do not enter the payload. The stored scan receipt must be
clean or flagged, and S18 scans the exact outgoing payload again. Pending
revisions wait. Retiring or replacing a head removes its old lineage rows.
A shared vector stays until no live head in any scope refers to its content.
Finished jobs expire after seven days through the bounded writer sweep.

Use `brigade memory reindex --target WORKSPACE --admin-token-file FILE` to
queue missing vectors. The command drains bounded pages, and repeating it
queues nothing after those pages exist. Its queued count describes the first
page. The explicit admin credential is separate from the node credential.
Use `--drop EMBEDDER_ID` to remove only that embedder's vectors, lineage and
jobs. A configured active embedder can backfill itself again on its next
scheduler pass. Old matrices stay available until explicitly dropped.

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
expiry, dimensions and digest again. `--once` processes at most one lease.
A failed worker leaves the lease available for recovery after expiry.

For reachable query embedding, configure `query_endpoint` or `endpoint` on
the hub under the same allowlist and secret-file policy. Pull mode can omit
both endpoints and the model secret. It then answers lexical fallback with
embedder-unavailable, even when document vectors are complete. Only a local
loopback model is admitted by the Python pull worker. The Rust hub's remote
backend supports allowlisted private HTTP and HTTPS endpoints.

## Measurements

At 20,000 synthetic rows, a matrix rebuild with its old snapshot retained
recorded 171,508 KiB peak RSS at 1,024 dimensions and 651,560 KiB at 4,096
dimensions. These are isolated matrix-process measurements, including test
runtime overhead, without a lexical index, SQLite corpus or embedding model.
The larger rebuild exceeds the reference 512 MiB allotment. Production
memory planning must include those other components and any second model.
