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
back the setting. Standalone Python keeps its existing checkpoint policy.

Rust replaces incoming internal key and client-address headers with its key
and the TCP peer address. Python keeps that address for audit records while
browser trust checks continue using the actual socket peer.

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

Limits: 4,096 connections (`--max-connections` overrides), 256 connections per
TCP source, backlog 1,024, 64 requests per hashed Authorization value, and 8
requests per source without Authorization. The 24-request global proxy cap
applies to both. Request headers have 10 seconds and request bodies 30 seconds
and 8 MiB. The front buffers each body before request admission, so an upload
that is still arriving holds no proxied slot. Buffered bodies share a 64 MiB
budget. A body that cannot reserve its declared length within 1 second gets
503 `body-budget-full`. Body overflow returns 413, an unfinished body 408, and
a truncated body 400. HTTP framing ambiguity is refused with 400. Connection
admission closes the socket. Request admission returns 503 with
`Retry-After: 1`. Slots remain held through response completion or
disconnect. Startup raises the soft descriptor limit to the hard limit and
refuses a hard limit below max_connections + 24 upstream sockets + 64 margin.

Python remains the route and authorization authority. Authorization and Cookie
pass unchanged, duplicate Set-Cookie headers survive, and hop-by-hop headers
are removed. Identity and scheme headers are stripped unless explicitly trusted
on browser routes. Incoming internal client-address headers are always dropped.
Request bytes are forwarded unchanged after buffering, and response bytes
stream, both without JSON re-encoding.

Deferred: S4 storage, S8 native waiters, S9 metrics and route modes, and S10
packaging and deployment recipes.

Verification: execute `engines/hub/verify` through `brigade work verify run`.
The frozen contract drive launches the managed front with:

```sh
registry/skills/verify-brigade/control-brigade.py hub-contract --hub-cmd \
  'engines/hub/target/release/brigade-hub --host {host} --port {port} --db {db} --token-file {token_file}'
```
