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
message-principal rows on one dedicated query-only connection. Admin digest
comparison uses constant-time equality. Revocation takes effect on the next
lookup. Lookup and connection wait together have a two-second deadline.
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
