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

S15 is split for review. S15a supplies this boundary and lexical mode.
S15b supplies vector storage, jobs, pull workers, dense retrieval and mode
switching. Until S15b, a configured hybrid mode validates the embedder table
and token file at startup, reports switching and serves BM25. Search sends
no query to the endpoint, because S15a would discard the vector. S15b
restores the query call with dense retrieval. The transport alone does not
claim hybrid retrieval coverage.

A query that succeeds clears an earlier fault, even when the probe failed,
and an expired breaker window counts as closed. Status leaves `degraded`
once the embedder answers again.
