# Agent mesh implementation plan

Design: [phase-agent-mesh.md](phase-agent-mesh.md). Issues: #2005 (sealed
mail), #2006 (teammate invites), #2007 (no-hub mode), #2008 (Hub Lite). Parent:
#1121.

Each slice below is one pull request that a worker lane can pick up on its own
once the slices it depends on have merged. Slices list the files they touch,
the tests they add and what must be true before they merge. Every slice runs
the focused gate through Brigade with its own test selectors:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","<selectors>"]' --capture brigade-work
```

The full gate runs once per pull request, as AGENTS.md requires.

## Gates that need the operator

- **G1, dependency (blocks slice 1 and everything that imports it):** agreement
  on #2005 to add the optional extra `mesh = ["cryptography>=47,<52"]`, with
  the Intel Mac `--only-binary cryptography` instruction and no Windows arm64
  wheel. AGENTS.md requires a conversation before a new dependency. Until G1 is
  recorded on #2005, slice 1 must not merge. Slices 3, 4 and 8 never import
  `cryptography` and can proceed while G1 is open.
- **G2, native engine (blocks slices 15, 18 and 19):** agreement on #2007 to add
  `engines/mesh/` (Rust, iroh 1.x) and its CI job.
- **G3, design open questions 2, 3, 4, 6 and 7** in the design doc. Each slice
  that depends on one names it. A slice may land with the proposed default
  behind a constant if the operator has not answered yet, and the pull request
  says so.

## Constraints for every slice

- `src/brigade/fleet_hub.py` is at 1963 lines and the module size ratchet caps
  modules at 2000 lines. New hub logic goes in new modules. Only small hook
  calls may be added to `fleet_hub.py`. `fleet_hub_messages.py` (1135 lines)
  and `fleet_client_messages.py` (1183 lines) also take new logic in new
  modules where it is more than a few lines.
- The hub never imports `cryptography`. A test in slice 3 enforces this.
- Every slice that changes the hub schema (3, 4, 8 and 9) bumps
  `SCHEMA_VERSION` or registers its columns in the additive column lists in
  `fleet_hub.py`, so the startup gate accepts an upgraded database.
- Every module that needs the extra imports it lazily inside functions and
  raises one `MeshExtraMissing` error, the same pattern
  `src/brigade/dot/access_auth.py` uses with `OptionalDependencyError`.
- No fixture, test or doc contains real hostnames, addresses, tokens or
  personal names. Use `example.invalid` hosts and obviously fake keys.
- Existing plain-text messaging tests pass unchanged with and without the
  extra installed.
- `src/brigade/message_envelope.py` already exists for run messages (#1010).
  New modules use the `fleet_mesh_` prefix to avoid confusion.

## Dependency order

```
G1 -> 1 -> 2 -------> 5 -> 6 -> 7
3 (no G1) ----------> 5
4 (no G1) ----------> 5
8 (no G1) + 3 -> 9 -> 10 (also needs 2)
4 + 9 -> 11 -> 12 -> 13 -> 14 (also needs 10)
G2 -> 15
16 (any time)
1 + 2 + 16 -> 17
15 + 17 -> 18 -> 19
```

## #2005: sealed message bodies

### Slice 1: crypto core and the `mesh` extra (needs G1)

Pure functions, no I/O, no wiring into messaging.

Files:
- `pyproject.toml`: add the `mesh` extra. Add `cryptography>=47,<52` to the
  `dev` extra so mypy and tests see it.
- `src/brigade/fleet_mesh_crypto.py` (new): `MeshExtraMissing`, key pair
  generation, key record build and verify (`self_sig`, `prev_sig`), `kid` and
  fingerprint derivation, canonical encoding, the binary payload fields,
  padding, `seal()` and `open()`
  for the version 1 envelope in design D2, using
  `cryptography.hazmat.primitives.hpke` with X25519, HKDF-SHA256 and
  AES-256-GCM.
- `.github/workflows/ci.yml`: one job leg runs the mesh tests with the extra,
  one confirms the plain-text suite without it, and one installs the floor
  version (47) to catch API drift.

Tests (`tests/test_fleet_mesh_crypto.py`):
- Seal and open round trip for recipient and sender wraps.
- Changing any single header field, the ciphertext, a wrap or the signature
  makes `open()` fail with a fixed error code.
- An envelope re-signed by a different sender with `from` changed fails to
  unwrap (the identity-misbinding case in D2).
- Unknown `v` or `suite` gives `envelope_unsupported`.
- Padding: sizes land on 1 KiB buckets and strip back exactly.
- Two seals of the same input draw different content keys.
- The largest legal message (64 KiB body of quotes, backslashes and CJK text,
  maximum subject, ref, thread and label) seals under the 96 KiB envelope cap.
- Key records: changing any covered field (including `suites` and `prev_kid`)
  breaks `self_sig`.
- A checked-in known-answer fixture (fixed test keys, fixed envelope) opens, so
  a refactor cannot change the format silently.
- With `cryptography` blocked in `sys.modules`, every entry point raises
  `MeshExtraMissing` and nothing else.

Acceptance: focused gate passes on the floor and newest `cryptography`. No
other module imports `fleet_mesh_crypto` yet.

### Slice 2: local key store, pins and `brigade fleet keys` (needs 1)

Files:
- `src/brigade/fleet_mesh_keys.py` (new): key files under
  `<brigade_home>/fleet/mesh-keys/`, pin files per hub id, TOFU, continuity
  acceptance, `changed` status, retired keys and pruning. File handling follows
  `read_message_token_file` and `src/brigade/authority_key.py`: owner-only
  mode, parent directory 0700, no symlinks, atomic replace.
- `src/brigade/cli/fleet_keys.py` (new), registered from `cli/fleet.py`:
  `keys init`, `keys show`, `keys fingerprint`, `keys rotate`, `keys accept
  <principal> --fingerprint <fp>`, `keys accept --all-changed --hub <url>`,
  `keys prune`. `rotate` only writes locally in this slice. Publishing arrives
  with slice 6.

Tests (`tests/test_fleet_mesh_keys.py`):
- Key files are created 0600 under a 0700 directory, and a symlinked key path
  is refused.
- First sight pins with source `tofu`. A record with a valid `prev_sig` from the
  pinned, retired key advances the pin. Any other change yields `changed`.
- A change signed by a revoked key yields `changed` and is never accepted automatically.
- `keys accept` refuses a fingerprint that does not match.
- No MCP tool can reach pin mutation: `fleet_mcp.tool_specs()` names are
  unchanged and none maps to `fleet_mesh_keys` writers.

Acceptance: focused gate passes. `brigade fleet keys --help` lists the
subcommands.

### Slice 3: hub key directory and hub identity (no G1)

Files:
- `src/brigade/fleet_hub_message_keys.py` (new): `fleet_message_keys` table
  with one active key per principal (partial unique index), `hub_meta` table
  with `hub_id`, `keys_publish` and `keys_get` actions, format checks, the
  `prev_kid` rule and `principal_key_exists`, refusal of revoked kids.
- `src/brigade/fleet_hub_messages.py`: route `keys_publish` and `keys_get` in
  `handle()`. `_station_read` adds each visible principal's active record under
  `keys`.
- `src/brigade/fleet_hub.py`: one call from `revoke_node` to revoke keys in the
  same transaction, the schema version bump, and `GET /mesh/identity` routing
  in `fleet_hub_http.py`.
- `revoke_principal` in `fleet_hub_messages.py`: the same key revocation call.
- `src/brigade/fleet_mcp.py`: `_frame_directory` treats the hub's validated key
  fields as structural. Raw keys still do not reach agents (slice 5 replaces
  them with a fingerprint and status).

Tests (`tests/test_fleet_hub_message_keys.py`):
- A caller can publish only its own record. A second record without
  `prev_kid` gets `principal_key_exists`.
- Revoking a node or principal revokes its keys, and a revoked kid cannot be
  published again.
- `who` shows only active keys, and only for principals the caller's grants
  allow.
- `/mesh/identity` returns a stable hub id across restarts.
- Importing the hub modules with `cryptography` blocked in `sys.modules`
  succeeds, proving the hub has no crypto dependency.

Acceptance: issue #2005 "revoking a node stops new mail from being sealed to
its key" holds at the hub level: after revocation `who` serves no key for that
node.

### Slice 4: sealed storage and the hub policy (no G1, design question 2)

Files:
- `src/brigade/fleet_hub_sealed.py` (new): envelope parse and header checks
  with string comparisons only, size cap (96 KiB), `payload_hash` over the
  envelope bytes, `recipient_key_stale`, the `in_reply_to` check for replies,
  and the post-ack envelope purge behind a constant (default from question 2).
- `src/brigade/fleet_hub_messages.py`: `envelope` and `envelope_bytes` columns,
  `send()` and `reply()` branch to the sealed path, `_row()` returns
  `envelope` and `sealed: true` for sealed rows. Sealed rows store empty
  `subject` and `body`.
- Hub serve flag `--require-sealed` and `BRIGADE_FLEET_MESSAGES_REQUIRE_SEALED`.
  `/mesh/identity` reports the policy.

Tests (`tests/test_fleet_hub_sealed.py`), using syntactically valid fake
envelopes because the hub never verifies cryptography:
- A sealed send stores empty `subject` and `body` columns and NULL `reference`,
  `thread` and `sender_label`. A sealed send that also carries plain `subject`
  or `body` fields is refused rather than stored. The database grep with real
  sealing is in slice 6, because this slice cannot produce real ciphertext.
- `from` not equal to the caller, a stale `from_kid` (`sender_key_stale`) or a
  stale recipient kid (`recipient_key_stale`) is refused, and a 409 stores
  nothing.
- The `(sender, client_id)` duplicate check runs before the key id checks: a
  retry of a stored envelope after the sender rotates returns the stored row.
- A retry with identical envelope bytes and the same `client_id` returns the
  stored row. Different bytes with the same `client_id` is a conflict.
- With `--require-sealed`, a plain-text send gets `message_sealing_required`.
  Without it, plain-text tests in `tests/test_fleet_messages.py` pass unchanged.
- A sealed reply whose hub `reply_to` row disagrees with `in_reply_to` is
  refused.

Acceptance: the hub stores sealed rows without plaintext columns and enforces
its policy.

### Slice 5: opening on read, the fence and live delivery (needs 1, 2, 3 and 4)

Opening lands before sealing, because a sealed reply has to open the original.
Tests build envelopes with slice 1's `seal()` and post them through the hub.

Files:
- `src/brigade/fleet_mesh_open.py` (new): `open_rows()` with the order in
  design D5 (header and hub id check, signature, unwrap and decrypt, binary
  payload limits, freshness and replay cache, header-sourced `kind`,
  `priority` and `sent_at`, the `reply_to` check), the structural field
  pattern checks for every row, the `unsealed_from_pinned` rule, and the
  failed-row shape with Brigade's fixed body.
- `src/brigade/fleet_client_messages.py`: `list_messages()` (with
  `open_limit`), `sent()`, `ack()` and the rows returned by `send()` and
  `reply()` pass through `open_rows()` after `_validate_page`.
- `src/brigade/fleet_mcp.py`: add `sealed`, `signature`, `open_error`, `key`
  and `sent_at` to the structural field sets, add the new codes to
  `_SAFE_ERROR_CODES`, and replace raw keys in `fleet_who` output with
  `{"fingerprint", "status"}`.
- `src/brigade/fleet_inbox_delivery.py`: open only the shown rows, lazily, with
  cached keys only and no extra hub call.
- `src/brigade/cli/fleet_messages.py`: show `sealed` and `open_error` in human
  and JSON output.
- `src/brigade/dot/hub_inbox.py`: reply matching uses the opened row, so it
  relies on the verified `reply_to`.

Tests:
- `tests/test_fleet_mesh_open.py`:
  - A tampered ciphertext and a wrong signer are rejected before `_frame()`
    runs, and the agent-facing result contains no plaintext.
  - An envelope from another hub id, an envelope older than the window, and a
    future-dated envelope are each refused with their own code.
  - A replayed envelope under a new hub message id opens as `replayed`.
  - A plain-text row whose sender is pinned opens as `unsealed_from_pinned`.
  - Hub-supplied `subject` and `body` beside an envelope are ignored.
  - A `reply_to` that disagrees with `in_reply_to` opens as `reply_mismatch`.
  - A failed row contains no sender-chosen string outside the fence, including
    `client_id`.
  - An `agent:` sender's sealed `sender_label` is dropped.
  - A row with a malformed structural field is dropped.
- `tests/test_fleet_inbox_delivery.py`:
  - A sealed message is shown fenced.
  - An unopenable one renders the fixed body without raising, and the cursor
    advances exactly as it does for plain text.
- `tests/test_dot_inbox_tools.py`: a failed row does not raise in the Dot pack.

Acceptance: the "tampered ciphertext or wrong signer is rejected before the
agent sees the text" bullet of #2005 holds. Run the focused gate with the
extra and with it blocked.

### Slice 6: sealing on send, reply and the spool (needs 5)

Files:
- `src/brigade/fleet_mesh_send.py` (new):
  - the seal decision table in design D4
  - key lookup through pins and `who`, and the downgrade rule for pinned
    principals
  - a fresh content key on every seal
  - reply subject and thread construction for sealed originals
  - the conflict check for deterministic client ids, which fetches the stored
    row with `sent` by id and compares plaintext through the sender wrap
- `src/brigade/fleet_client_messages.py`:
  - `send()`, `reply()` and the spool call into `fleet_mesh_send`.
  - The spool stores the sealed envelope before the first transmission.
  - A send made offline without a key is stored as `seal: "pending"`, and so
    is a reply whose original has not been opened. The reply record keeps the
    original's hub message id, and drain fetches and opens the original before
    sealing.
  - `_drain_locked` keeps and reseals on `recipient_key_stale` and
    `sender_key_stale` instead of dropping them as unknown 4xx responses.
- `src/brigade/fleet_action_adapters.py`: `_inbox_main` sends through
  `send()` rather than raw `_online`, so it gets the seal decision.
- `src/brigade/fleet_mesh_keys.py`: publish on first use and on `keys rotate`,
  and report `local_key_superseded` when the hub refuses this machine's kid.
- `src/brigade/fleet_client_message_credentials.py`: the new error codes.

Tests (`tests/test_fleet_mesh_send.py`), against the in-process hub fixtures in
`tests/_fleet_message_fixtures.py`:
- Every row of the D4 decision table.
- The spool file holds the sealed envelope before the first request leaves,
  and a forced retry sends identical bytes.
- Two seals of the same input use different content keys.
- A pending send and a pending reply are sealed at drain time and persisted
  before sending.
- A 409 for a stale key keeps the spool record and reseals it. Nothing is
  dropped.
- A crash-and-resend with a deterministic `client_id` (the patrol and Dot
  shapes) returns the stored row instead of a conflict.
- A pinned recipient with no key served gets `recipient_key_missing` and never
  plain text.
- `fleet_action_adapters` inbox sends are sealed.
- Without the extra, plain-text sends behave exactly as before, and sends that
  need sealing fail with `mesh_extra_missing`.
- Round trips:
  - MCP: two principals exchange sealed mail through `fleet_send`,
    `fleet_inbox`, `fleet_reply`, `fleet_ack` and `fleet_sent`.
  - CLI: the same exchange through `brigade fleet msg send`, `list`, `reply`
    and `ack`.
  - A dump of every table in the hub SQLite file contains neither the subject
    nor the body marker string.

Acceptance: all four #2005 acceptance bullets hold, including the database
grep with real sealing.

### Slice 7: user docs for sealed mail (needs 6)

Files: `docs/fleet-messages-sealed.md` (new), links from the fleet messaging
docs and `docs/fleet-mcp-events.md` (one sentence: payloads already exclude
sealed fields).

Content: install (including the Intel Mac and Windows arm64 notes), hub
policy, local policy, key commands, fingerprint checks, what stays plain text,
and the threat model table condensed from the design.

Acceptance: `brigade guard scan` with `src/brigade/guard/policies/public-repo.json`
is clean, and Vale reports no errors.

## #2006: teammate invites

### Slice 8: teammate role, grant hardening and the audit log (no G1, design question 6)

Files:
- `src/brigade/fleet_hub_audit.py` (new): `fleet_audit_log` table, append
  helper that refuses secret-shaped values, admin read with paging, and
  aggregation for refused redemptions.
- `src/brigade/fleet_hub_messages.py`: `role` and `person` columns on
  `fleet_message_principals` (default `agent`). Audit rows for grant changes
  and principal revocation.
- `src/brigade/fleet_hub_message_grants.py`: one helper that looks the caller
  up and treats only enrolled node ids as unrestricted. It replaces the
  `agent:` prefix tests in `permits()`, `require_granted()` and
  `require_recipient_cap()`, in `_station_read()` and in
  `fleet_hub_events.permits()`. Teammate grants become symmetric (a sender
  needs to be in the teammate's grant list to reach it).
- `_station_read`: teammates get `principal_role_forbidden` on `claims`, and
  `who` drops session repo, branch and harness for them.
- `src/brigade/fleet_hub_events.py`: teammates may subscribe only to their own
  `message.created`.
- `src/brigade/fleet_hub_http.py`: `GET /audit` (admin).

Tests (`tests/test_fleet_hub_teammates.py`, extending
`tests/test_fleet_message_grants.py`):
- A teammate cannot read claims, cannot subscribe to `claim.expired` or
  `run.failed`, and cannot message principals outside its grant.
- A node principal cannot message a teammate that has not granted it.
- A principal id with an unknown prefix gets the restrictions of an `agent:` sender.
- Audit rows exist for grant changes and revocation and contain no token.

Acceptance: hub-side half of #2006's "a teammate principal cannot read claims,
submit work or message principals outside its grant".

### Slice 9: hub invites and redemption (needs 3 and 8)

Files:
- `src/brigade/fleet_hub_invites.py` (new): `fleet_invites` table, admin
  `create`, `list` and `revoke`, redemption under `BEGIN IMMEDIATE` following
  `fleet_hub_enrollment.redeem`, constant-time digest comparison, one generic
  refusal, per-source and global rate limits, principal creation with role and
  grants, key and binding storage, audit rows.
- `src/brigade/fleet_hub_http.py`: `POST /invites` (admin),
  `POST /invites/redeem` (no token), `GET /join` (static page that does not
  read the fragment).

Tests (`tests/test_fleet_hub_invites.py`):
- An invite redeemed twice fails the second time. Expired and revoked invites
  fail. A wrong secret fails with the same response as an unknown id.
- Two concurrent redemptions of a one-use invite produce exactly one principal.
- `max_uses` above one admits that many and no more.
- The hub stores only `sha256(redeem_key)` and the binding, never the secret.
- Removing a member makes its token return 401 on the next call.

Acceptance: #2006's first two acceptance bullets hold against the Python hub.

### Slice 10: invite and join client (needs 2 and 9)

Files:
- `src/brigade/fleet_mesh_invites.py` (new): link build and parse for
  `bfi1` (hub id, invite id, secret, inviter fingerprint), HKDF derivation of `redeem_key` and `bind_key`, the local invite
  record holding `bind_key`, binding verification and `invite`-source pins.
- `src/brigade/cli/fleet_invites.py` (new): `brigade fleet invite create |
  list | revoke`, `brigade fleet join '<link>'`, `brigade fleet members list |
  remove`, `brigade fleet audit list`.
- `src/brigade/fleet_client_message_credentials.py`: a new
  `write_message_token_file()` that mirrors `read_message_token_file()`
  (owner-only, no symlinks, atomic replace). Only a reader exists today.
- `invite create` requires the `--as` principal's private key on the local
  machine and supports `--bootstrap` for a hub with no keyed principal yet.

Tests (`tests/test_fleet_mesh_invites.py`):
- End to end against the in-process hub: create, join, the joiner pins the
  inviter from the link fingerprint, the inviter verifies the binding and pins
  the joiner, and a sealed round trip works between them.
- A hub that swaps the joiner's key is caught by the binding check, and a hub
  that swaps the inviter's key is caught by the link fingerprint.
- The link never appears in logs or error text.
- A link whose hub id differs from `/mesh/identity` is refused before
  redemption.
- A binding mismatch pins nothing, reports `binding_mismatch` and exits
  nonzero.
- `invite create --as` a principal whose key is not local is refused.
- A `--bootstrap` link joins a fresh hub, and the next invite is created from
  the joined machine.

Acceptance: #2006's third bullet ("a teammate's sealed mail cannot be read by
the hub") holds, using slice 4's database grep with a teammate sender.

## #2008: Hub Lite

### Slice 11: hub contract suite (needs 4 and 9)

Files:
- `tests/contract/fleet_hub_contract.py` (new): a backend-neutral suite that
  takes a base URL, an admin token and a callback that dumps stored rows. It
  covers identity, invites, join, `who`, sealed `send`, `list` with `wait_ms`,
  `ack`, `reply`, `sent`, key publish and revoke, role restrictions, and the
  "no plaintext in storage" check, with two nodes.
- `tests/test_fleet_hub_contract_python.py` (new): runs the suite against the
  Python hub in-process.

Acceptance: the suite passes on the Python hub and is the bar Hub Lite must
meet.

### Slice 12: Hub Lite core (needs 11)

Files under `workers/hub-lite/` (self-contained, so the deploy button can treat
it as a repository root):
- `wrangler.toml` with one SQLite-backed Durable Object class, `workers_dev =
  true`, a `HUB_ADMIN_TOKEN` secret, and a `.dev.vars.example`.
- `src/index.js` (routing and request limits) and `src/fleet.js` (the Durable
  Object: schema from design D10, token hashing with WebCrypto, identity,
  invites, keys, messages with sealed-only storage, quotas, long-poll waiters
  capped at 20 s, audit).
- `test/*.test.js` on `node:test`, with a fake `ctx.storage.sql` backed by
  Node's built-in `node:sqlite` so the real SQL runs in tests without npm
  dependencies. The slice confirms the minimum Node version that ships
  `node:sqlite` unflagged and pins it in CI.
- `.github/workflows/ci.yml`: a `workers` job that runs `node --test` for
  `workers/**` when those paths change. It also covers
  `workers/github-webhook-relay`, which has no CI job today.

Tests: the same cases as the contract suite, ported to `node:test`, plus a
storage dump assertion that no plaintext marker is stored and a test that a
send without an envelope is refused.

Acceptance: `node --test` passes in CI. No route outside the D10 table answers
anything but 404.

### Slice 13: Hub Lite presence and claims (needs 12, design question 7)

Files: `workers/hub-lite/src/fleet.js` and tests.
- Presence: `POST /sessions` and `GET /sessions`, heartbeats held in memory,
  rows written only on state, repository or branch change or once per third of
  the TTL.
- Claims (if question 7 keeps them): `acquire`, `renew` and `release` with
  holder fencing tokens and TTL, and the `claims` read for `fleet_claims`.
  The `scope: "node"` supersede extension stays out.

- Client throttles in `src/brigade/fleet_session_presence.py` and
  `src/brigade/fleet_inbox_delivery.py`, applied when `/mesh/identity` reports
  a Hub Lite: one heartbeat per session per 60 seconds unless state changes,
  and a 60 second delivery poll interval.

Tests: a heartbeat storm writes a bounded number of rows, and the Python
client throttles hold under a burst of tool events. Claim exclusivity,
fencing token checks and expiry match the Python hub's claim tests that apply.

Acceptance: the contract suite's presence and claims sections pass in
`node:test`.

### Slice 14: deploy template, limits docs and live contract run (needs 10 and 13)

Files:
- `workers/hub-lite/README.md`: the Deploy to Cloudflare button, the
  `npx wrangler deploy` fallback, the free plan limits table from design D10
  with what happens at each limit, the one-hub-per-account advice, optional
  Cloudflare Access for admin routes, and bootstrap with `brigade fleet invite
  create --role node`.
- `scripts/hub-lite-contract` (new): runs `tests/contract/fleet_hub_contract.py`
  against a URL, for `wrangler dev` locally or a deployed instance.

Operator step, recorded in the pull request: deploy to a fresh Cloudflare
account on the Free plan with the button, confirm the Durable Object migration
applied (UNVERIFIED in the design), run the contract script with two nodes,
and record the measured rows written per message and requests per waiting
station.

Acceptance: #2008's three acceptance bullets, with the measured numbers
replacing the derived ones in the design doc.

## #2007: no-hub mode

### Slice 15: spike prototype (needs G2)

Files: `engines/mesh/` (new Rust crate, iroh 1.x) with a minimal binary that
opens an endpoint on ALPN `brigade-mesh/1` and exchanges one length-prefixed
frame. No Brigade integration.

Measure and record in the design doc's D8 section: release binary size per
platform, cold start time, a connection between two machines on different
networks through a self-hosted `iroh-relay`, whether a direct path forms, and
behavior with `--relay none` on a LAN and on a tailnet.

Acceptance: a go or no-go recorded in the design doc. On no-go, D8 is
rewritten to the fallback (LAN and tailnet only, Hub Lite for the rest) before
any later no-hub slice starts.

### Slice 16: backend interface (any time)

Files:
- `src/brigade/fleet_backend.py` (new): a `MessageBackend` protocol with `who`,
  `send`, `reply`, `list`, `ack`, `sent` and `claims`, and `HubBackend`
  wrapping the existing client functions.
- `src/brigade/fleet_mcp.py`, `src/brigade/fleet_inbox_delivery.py` and
  `src/brigade/cli/fleet_messages.py`: call through the backend, so no-hub mode
  gets live delivery and the CLI as well as MCP. No behavior change.

Tests: the existing `tests/test_fleet_mcp.py` and
`tests/test_fleet_inbox_delivery.py` pass unchanged. A new test asserts
`tool_specs()` is identical for every backend.

Acceptance: pure refactor, no output change.

### Slice 17: local mesh store and backend (needs 1, 2 and 16)

Files:
- `src/brigade/fleet_mesh_store.py` (new): SQLite inbox, outbox, contacts,
  receipts and replay cache in WAL mode, the shared database the engine will
  use.
- `src/brigade/fleet_mesh_backend.py` (new): `MeshBackend` implementing the
  seven tools per design D9. `claims` returns `hub_feature_unavailable`.
- `src/brigade/cli/fleet_mesh.py` (new): `brigade mesh init --relay
  <url|n0-public|none>` (design question 4), `brigade mesh status`, contacts,
  and `bmi1` invites through `brigade fleet join`.
- `fleet.mode` configuration and selection in `brigade fleet mcp`.

Tests (`tests/test_fleet_mesh_backend.py`), with a fake engine that moves rows
between two temporary databases:
- Two nodes exchange sealed messages with no hub configured, through the same
  MCP tool calls the hub tests use.
- An offline peer receives queued mail when the fake engine reconnects, and the
  outbox drops entries after 7 days.
- `fleet_claims` returns the fixed code. `fleet_who` returns contacts and an
  empty `sessions` list.

Acceptance: #2007's "the same MCP tool schemas work in hub and no-hub mode"
holds against the fake engine.

### Slice 18: engine daemon (needs 15 and 17)

Files:
- `engines/mesh/`: the daemon reads the outbox, sends frames, writes received
  envelopes to the inbox, returns `stored` receipts, honors the relay setting,
  and exposes a status file. It never decrypts.
- `brigade setup` installs the engine the way it installs the code-graph
  engine. `brigade doctor` adds `WARN` or `MANUAL` checks for a configured but
  missing engine, a stopped daemon and an unset relay choice.
- Service unit templates for Linux (systemd user unit), macOS (launchd) and
  Windows (scheduled task).
- `.github/workflows/ci.yml`: cargo check, clippy and test for
  `engines/mesh/**`, path-filtered like `engines/code-graph`.
- `docs/fleet-no-hub.md` (new): setup, relay choices and what each exposes,
  and the plain statement that claims, runs, patrol, dispatch and MCP Events
  need a hub.

Tests: Rust unit tests for framing and size limits. An integration test in CI
starts `iroh-relay --dev`, runs two daemons that can only reach each other
through it, and exchanges a sealed message. A capture at the relay contains no
plaintext marker.

Acceptance: #2007's "two nodes on different networks exchange sealed messages
with no hub configured, and a relay cannot read them", shown by the relay test
in CI and by one manual run across two real networks recorded in the pull
request.

### Slice 19: anchor mode (optional, needs 18)

Files: `engines/mesh/` and `src/brigade/fleet_mesh_store.py`. An always-on node
started with `--anchor` holds sealed envelopes for contacts that opted in and
forwards them when the recipient connects. It stores ciphertext and routing ids
only, with the same 7 day limit.

Tests: delivery succeeds when sender and recipient are never online at the
same time, and the anchor's database contains no plaintext marker.

Acceptance: offline delivery between machines that never overlap works
without a hub.
