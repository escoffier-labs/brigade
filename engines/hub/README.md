# brigade-hub (S3b)

A Rust HTTP front for the Python Fleet Hub. Run where `python3 -m brigade`
resolves this matching Python package, or set `BRIGADE_HUB_PYTHON` to its
interpreter.

```sh
brigade-hub --host localhost --token-file ./admin-token --db ./hub.db
```

**Do not deploy this front in front of a live hub until S10's upstream mode
lands.** This slice does not preserve real client addresses in Python.

Managed mode launches Python on a numeric loopback ephemeral port. Python
reports its bound port, avoiding a port reservation race. Only that child gets
`BRIGADE_HUB_MANAGED_LOOPBACK=1`, a default-off escape hatch that lifts Python's
per-source cap from 8 to 32 while retaining the 32-thread global cap. Python
refuses the escape hatch on non-loopback binds. Do not set it on a standalone
hub. The front admits at most 24 proxied requests, leaving room for cleanup
and health probes. It disables idle upstream pooling so idle keepalive sockets
cannot occupy Python handler threads.

The front exits nonzero if its managed child dies. `/health` retains the v1
liveness bytes and server header. `GET /ready` probes upstream health with a
one-second deadline and returns only `{"ready":true}` or `{"ready":false}`,
with status 200 or 503. A service manager must restart the pair after failure.
Shutdown stops accepting, drains connections for at most 125 seconds, then
stops the child. Idle connections count toward that deadline.

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
After response headers have reached a client, a body failure terminates the
stream. HTTP cannot replace that status with a 504. Clients must treat an
incomplete response as a transport failure.

Limits: 4,096 connections (`--max-connections` overrides), 256 connections per
TCP source, backlog 1,024, 64 requests per hashed Authorization value, and 8
requests per source without Authorization. The 24-request global proxy cap
applies to both. Request headers have 10 seconds and request bodies 30 seconds
and 8 MiB. Body overflow returns 413. HTTP framing ambiguity is refused with
400. Connection admission closes the socket. Request admission returns 503
with `Retry-After: 1`. Slots remain held through response completion or
disconnect. Startup raises the soft descriptor limit to the hard limit and
refuses a hard limit below max_connections + 24 upstream sockets + 64 margin.

Python remains the route and authorization authority. Authorization and Cookie
pass unchanged, duplicate Set-Cookie headers survive, and hop-by-hop headers
are removed. Identity and scheme headers are stripped unless explicitly trusted
on browser routes. Incoming internal client-address headers are always dropped.
Request and response bytes stream without JSON re-encoding.

Deferred: S4 storage, S8 native waiters, S9 metrics and route modes, and S10
packaging, upstream identity, real source forwarding and deployment recipes.

Verification: execute `engines/hub/verify` through `brigade work verify run`.
The frozen contract drive launches the managed front with:

```sh
registry/skills/verify-brigade/control-brigade.py hub-contract --hub-cmd \
  'engines/hub/target/release/brigade-hub --host {host} --port {port} --db {db} --token-file {token_file}'
```
