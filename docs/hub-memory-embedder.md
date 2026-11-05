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

S15 is split for review. S15a supplies this boundary and lexical mode.
S15b supplies vector storage, jobs, pull workers, dense retrieval and mode
switching. Until S15b, a configured hybrid mode reports switching and serves
BM25, or degraded fallback when the query endpoint fails. The transport
alone does not claim hybrid retrieval coverage.
