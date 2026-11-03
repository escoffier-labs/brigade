# brigade-hub (S3)

A Rust HTTP front for the Python Fleet Hub. Run from an environment where
`python3 -m brigade` resolves the matching Python package, or set
`BRIGADE_HUB_PYTHON` to that environment's interpreter.

```sh
brigade-hub --host localhost --token-file ./admin-token --db ./hub.db
```

The default launcher starts Python on a numeric loopback ephemeral port and
passes all existing serve flags and environment through. Python validates the
token, deck configuration, sealing policy, callback settings and database before
Rust binds. Rust enforces the numeric loopback restriction on trust flags
against the public bind address. No Rust storage or schema writes exist yet.

`--upstream http://<numeric-loopback>:<port>` instead uses an existing Python
hub. Its operator must configure that upstream with the same token, database,
deck, sealing and trust settings. In this mode Python owns startup validation
and lifecycle. Rust refuses non-loopback origins and never retries requests.
A failed upstream connection or response header timeout returns
`503 {"error":"hub busy","reason":"upstream-unavailable"}`. Native health
continues to answer the v1 liveness shape while the upstream is down.

Limits: 4,096 connections (`--max-connections` overrides), 256 connections per
TCP source, backlog 1,024, 64 in-flight requests per Authorization value,
10 seconds for headers, 30 seconds for the request body, and 8 MiB per body.
Connection admission closes the socket. Credential admission returns 503 with
reason `credential-inflight-full`. Admission slots live through response
streaming and are released on disconnect. Idle source and credential entries
are removed. The client pools at most 32 idle upstream sockets.

Python still applies its current request JSON bounds, auth envelopes, unknown
route behavior and waiter limits. It sees a loopback peer, so its 8-per-source
cap also remains effective. S10's authenticated upstream mode is required before
production cutover can preserve real source addresses and remove that second
cap. Requests and responses stream without body collection or JSON re-encoding.
Hop-by-hop headers are removed, Authorization is unchanged, identity and scheme
headers are stripped unless explicitly trusted on browser routes.

Deferred: S4 storage, writer queues and reader pools. S8 native waiter admission.
S9 route modes, shadow comparisons, metrics and structured request logs. S10
release artifacts, manifests, setup installation, Python engine selection,
trusted upstream mode and deployment recipes. No new Python dependencies.

Verification: `engines/hub/verify` runs format, clippy, tests, build and Rust
1.85 checks. Execute it through `brigade work verify run`. The frozen contract
can launch the front with its default supervised Python upstream:

```sh
registry/skills/verify-brigade/control-brigade.py hub-contract --hub-cmd \
  'engines/hub/target/debug/brigade-hub --host {host} --port {port} --db {db} --token-file {token_file}'
```
