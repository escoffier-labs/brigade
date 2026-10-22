# Agent mesh implementation plan

Design: [phase-agent-mesh.md](phase-agent-mesh.md). Issues: #2005 (sealed
mail), #2006 (teammate invites), #2007 (no-hub mode), #2008 (Hub Lite). Parent:
#1121.

Each slice below is one pull request that a worker lane can pick up on its own
once the slices it depends on have merged. Slices list the files they touch,
the tests they add and what must be true before they merge. Slice numbers
from revision 1 are kept so review comments still resolve. New slices carry a
letter suffix. Every slice runs the focused gate through Brigade with its own
test selectors:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","<selectors>"]' --capture brigade-work
```

The full gate runs once per pull request, as AGENTS.md requires. Planned
commands are written without the `brigade` prefix because they do not exist
yet.

## Integration baseline

Slices build on `main` plus #1939 (opaque paging cursors and snapshots), #2009
(`count_only`), #2010 (`client_id` and tool annotations) and #1951 (operator
questions). A slice that touches a surface one of them changes waits for that
merge and says so in its header. Rebase onto the merged form, not the open
branch.

## Gates

- **G1, dependency: approved** on #2005 (2026-10-22). The optional extra is
  `mesh = ["cryptography>=48,<52"]`. Design D1 explains the floor of 48.
- **G2, native engine: approved** on #2007 (2026-10-22), staged: slice 15 is a
  bounded spike, slice 15b writes the wire protocol, and slices 18a to 18e
  start only after slice 15 records a go and slice 15b is merged.
- **G3, open questions: decided.** Design "Decisions on the open questions"
  records all seven.
- **G4, `wrangler` for CI (blocks the workerd runs in slices 12a to 12c):**
  running the contract suite against workerd needs `wrangler` as a pinned
  development dependency of `workers/hub-lite/`, used only in CI and locally.
  AGENTS.md asks for a conversation before a new dependency, so slice 12a
  records the operator's agreement on #2008 before it merges.

## Constraints for every slice

- `src/brigade/fleet_hub.py` is at 1965 lines on `main` and the module size
  ratchet caps modules at 2000 lines. New hub logic goes in new modules. Only
  small hook calls may be added to `fleet_hub.py`. `fleet_hub_messages.py`
  (1135 lines) and `fleet_client_messages.py` (1183 lines) also take new logic
  in new modules.
- The hub never imports `cryptography`. A test in slice 3 enforces this.
- Every slice that changes the hub schema (3, 4, 7b, 8 and 9) bumps
  `SCHEMA_VERSION` past #1939's 41 or registers additive columns, so the
  startup gate accepts an upgraded database.
- Every module that needs the extra imports it lazily and raises one
  `MeshExtraMissing` error, the pattern `src/brigade/dot/access_auth.py` uses.
- No fixture, test or doc contains real hostnames, addresses, tokens or
  personal names. Use `example.invalid` hosts and obviously fake keys.
- Existing plain-text messaging tests pass unchanged with and without the
  extra installed.
- `src/brigade/message_envelope.py` already exists for run messages (#1010).
  New modules use the `fleet_mesh_` prefix.
- Docs refer to planned commands without the literal `brigade` prefix until
  the slice that adds the command merges, so the documented-command inventory
  check stays green.

## Dependency order

```text
1 -> 2
3 -> 4
1 + 3 -> 2b
1 + 2 + 2b + 3 + 4 -> 5 -> 6 -> 7
6 + #1951 -> 7b
8;  3 + 8 -> 9
2 + 2b + 5 + 6 + 9 -> 10
4 + 9 -> 11 -> 12a -> 12b -> 12c -> 13 -> 14 (14 also needs 10)
15 -> 15b
16
1 + 2 + 5 + 6 + 15b + 16 -> 17
15 + 17 -> 18a -> 18b -> 18c -> 18d, 18e
18b -> 19
```

## How the review findings map to slices

| Finding | Design | Slices |
|---|---|---|
| ask_user bypasses the boundary | D7 | 7b, docs note in 7 |
| Hub identity can reset pins | D4 | 3, 2b |
| Reply check trusts hub columns | D5, D6 | 4, 5 |
| Pin acceptance boundary | D3 | 2 |
| Invite fragment readable by the hub's page | D9 | 9, 10 |
| Pending invitation trust | D3, D9 | 2, 10 |
| Retry recovery API | D5 | 1, 4, 6 |
| Rotation and retention semantics | D3, D8 | 2, 4, 5 |
| Idempotent redemption | D9 | 9, 10 |
| Dependency graph and baseline | this plan | all |
| Hub Lite storage math | D8, D12 | 4, 12c, 14 |
| Reason Network rationale | D10 | none (docs) |

## #2005: sealed message bodies

### Slice 1: crypto core and the `mesh` extra

Pure functions, no I/O.

Files:
- `pyproject.toml`: add the `mesh` extra. Add `cryptography>=48,<52` to the
  `dev` extra.
- `src/brigade/fleet_mesh_crypto.py` (new): `MeshExtraMissing`, the ML-KEM
  backend probe (`mesh_backend_unsupported`), Ed25519 and hybrid KEM key
  generation, key record build and verify, `kid` and fingerprint derivation,
  canonical encoding, binary payload fields, padding, the routing header,
  signed header and envelope of design D2, the retry commitment, the label
  constants, and `seal()` and `open()` with `Suite(KEM.MLKEM768_X25519,
  KDF.HKDF_SHA256, AEAD.AES_256_GCM)`.
- `.github/workflows/ci.yml`: one leg runs the mesh tests with the extra, one
  runs the plain-text suite without it, and one installs the floor (48).

Tests (`tests/test_fleet_mesh_crypto.py`):
- Seal and open round trip for the recipient and sender wraps.
- Changing any single routing header field, `ct_sha256`, `wraps_sha256`,
  `rc`, the ciphertext, a wrap or the signature makes `open()` fail with a
  fixed code.
- An envelope re-signed by a different sender with `from` changed fails to
  unwrap and fails the AEAD check.
- An envelope forwarded unchanged to a third party fails the `to` check, and
  one with `to` or `rcpt` rewritten fails the signature.
- The signed header verifies alone after `ct` and `recipients` are removed.
- Unknown `v` or `suite` gives `envelope_unsupported`.
- Padding lands on 1 KiB buckets and strips back exactly.
- Two seals of the same input draw different content keys and produce the
  same `rc`. Changing any semantic field changes `rc`, and changing
  `created_at` alone does not.
- Every label is distinct, and a signature made under one label fails under
  each of the others.
- The largest legal message seals under 96 KiB, and the client reports
  `message_too_large` against a 32 KiB `max_envelope_bytes`.
- Key records: changing any covered field breaks `self_sig`.
- A checked-in known-answer fixture (fixed test keys and envelope) opens.
- With `cryptography` blocked in `sys.modules`, every entry point raises
  `MeshExtraMissing`. With ML-KEM reported unavailable, sealing raises
  `mesh_backend_unsupported` and never falls back to `KEM.X25519`.

Acceptance: the focused gate passes on the floor and newest `cryptography`.

### Slice 2: local key store, trust levels and the keys subcommands (needs 1)

Files:
- `src/brigade/fleet_mesh_keys.py` (new): key files, the retry key and retired
  keys under `<brigade_home>/fleet/mesh-keys/`, a pin store per accepted hub
  identity with the statuses of design D3, continuity chains of up to 8 links,
  key history with validity intervals, `compromised_from` handling, the
  manifest key pin, manifest and fingerprint-list verification, and pruning.
  File handling follows `read_message_token_file` and
  `src/brigade/authority_key.py`.
- `src/brigade/cli/fleet_keys.py` (new), registered from `cli/fleet.py`:
  `fleet keys show`, `rotate`, `accept <principal> --fingerprint <fp>`,
  `accept --manifest <file>`, `accept --fingerprints <file>`,
  `trust-manifest-key --fingerprint <fp>`, `manifest sign` (admin machine)
  and `prune`. No `--all-changed`. `rotate` writes locally until slice 6.

Tests (`tests/test_fleet_mesh_keys.py`):
- Key files are 0600 under a 0700 directory, and a symlinked path is refused.
- A first key is recorded `observed`, never `verified`.
- `accept` without `--fingerprint` is refused. With a fingerprint that does not
  match the fetched record it changes nothing, and with a matching one the
  entry becomes `verified`.
- A manifest signed by an unpinned key, or with any entry altered, is refused
  whole. A fingerprint list with one mismatch accepts nothing.
- A chain of 8 valid links advances the pin and keeps its trust level. A chain
  of 9 yields `changed`. A chain with a revoked link, a gap, or a
  non-increasing `created_at` yields `changed`.
- A key revoked with `compromised_from` rejects mail signed at or after that
  time, and rejects earlier-dated mail that this client did not record before
  learning of the revocation.
- Archived mail verifies against a retired key inside its interval and fails
  outside it.
- No MCP tool reaches pin, key, hub identity or manifest writers:
  `fleet_mcp.tool_specs()` names are unchanged and none maps to these modules.

Acceptance: the focused gate passes, and the keys help output lists the
subcommands.

### Slice 2b: hub identity on the client (needs 1 and 3)

Files:
- `src/brigade/fleet_mesh_hub_identity.py` (new): identity statement signing
  and verification, the hub id derivation, the endpoint-to-identity store (a
  separate file from the pin store), migration statements, and the
  `hub_identity_changed` refusal.
- `src/brigade/cli/fleet_hub_identity.py` (new): `fleet hub-identity init`,
  `show`, `publish` (admin token), `migrate` and `accept --hub-id <id>`.

Tests (`tests/test_fleet_mesh_hub_identity.py`):
- A known endpoint serving a different hub id or key stops every message
  operation with `hub_identity_changed` and creates no new pin namespace.
- A migration signed by the old key moves the pins to the new id. One signed by
  any other key is refused.
- `accept --hub-id` with the right id carries the pins, and with a wrong id
  changes nothing.
- A statement whose `hub_id` is not the hash of its key is refused.
- A new endpoint served by the same key with a fresh signed statement is
  accepted.

Acceptance: the hub identity can change only through a signed migration or
operator verification.

### Slice 3: hub key directory and hub identity storage (no crypto, needs #1939)

Files:
- `src/brigade/fleet_hub_message_keys.py` (new): `fleet_message_keys` with one
  active key per principal, `keys_publish` and `keys_get`, format checks, the
  `prev_kid` rule, `principal_key_exists`, refusal of revoked kids, and
  `compromised_from`.
- `src/brigade/fleet_hub_identity.py` (new): `hub_meta` storage of the signed
  statement and migration, the `hashlib` check that `hub_id` matches the key,
  and the admin upload action.
- `src/brigade/fleet_hub_messages.py` and `fleet_hub_paging.py`: route the new
  actions. `who` reads key entries live on every page instead of from the
  #1939 snapshot.
- `src/brigade/fleet_hub.py`: revoke keys in the same transaction as
  `revoke_node`, the schema bump, and `GET /mesh/identity` in
  `fleet_hub_http.py`.
- `revoke_principal`: the same key revocation call.

Tests (`tests/test_fleet_hub_message_keys.py`):
- A caller can publish only its own record. A second record without
  `prev_kid` gets `principal_key_exists`.
- Revoking a node or principal revokes its keys with `compromised_from`, and a
  revoked kid cannot be published again.
- Paging: between two pages of one `who` snapshot, a key published for a
  principal on the second page shows on that page, and a key revoked for a
  principal on the second page is not served there.
- `who` shows keys only for principals the caller's grants allow.
- `/mesh/identity` serves the uploaded statement unchanged, and refuses an
  upload whose `hub_id` does not match its key or that lacks the admin token.
- Importing the hub modules with `cryptography` blocked succeeds.

Acceptance: after revocation `who` serves no key for that node, including on
continuation pages.

### Slice 4: sealed storage, retry lookup, retention and admission (needs 3)

Files:
- `src/brigade/fleet_hub_sealed.py` (new): envelope parse and header checks by
  string comparison, the `max_envelope_bytes` cap (96 KiB default),
  `payload_hash` over the envelope bytes, `recipient_key_stale` and
  `sender_key_stale`, the symmetric `reply_to` and `in_reply_to` presence
  rule, `sent_lookup` by `client_id`, the existing id in the 409 body, the
  7 day post-ack purge to a `content_expired` tombstone, the unacked TTL, and
  per-sender and fleet stored-bytes admission.
- `src/brigade/fleet_hub_messages.py`: `envelope`, `envelope_bytes` and
  `content_state` columns, the sealed path in `send()` and `reply()`, sealed
  rows in `_row()`, and `count` (#2009) treating sealed rows like any row.
- `brigade fleet serve --require-sealed` and its environment variable.

Tests (`tests/test_fleet_hub_sealed.py`), using syntactically valid fake
envelopes because the hub never verifies cryptography:
- A sealed send stores empty `subject` and `body` and NULL `reference`,
  `thread` and `sender_label`. A sealed send that also carries plain fields
  is refused.
- A wrong `from`, a stale `from_kid` or a stale recipient kid is refused, and
  a 409 stores nothing.
- The duplicate check runs before the key id checks.
- Identical bytes with the same `client_id` return the stored row. Different
  bytes get 409 `message_client_id_conflict` with the existing id.
- `sent_lookup` returns only the caller's own row: another sender's
  `client_id` gives `not_found`, and a purged row returns its tombstone with
  the signed header.
- A `reply_to` without `in_reply_to`, or the reverse, is refused.
- An acked envelope becomes a tombstone after 7 days and keeps its header and
  signature. Unacked mail past the TTL is removed.
- A send over the per-sender stored-bytes limit gets `sender_storage_quota`,
  and one over the fleet limit gets `hub_storage_full`.
- `--require-sealed` refuses plain text with `message_sealing_required`.
  Without it, `tests/test_fleet_messages.py` passes unchanged.

Acceptance: the hub stores sealed rows without plaintext columns, enforces its
policy, and supports retry recovery after purge.

### Slice 5: opening, pending requests and the fence (needs 1, 2, 2b, 3 and 4)

Opening lands before sealing because a sealed reply has to open the original.

Files:
- `src/brigade/fleet_mesh_open.py` (new): `open_rows()` in the order of design
  D6, the seen-id cache with its start time and `unchecked` label, freshness
  and `MAX_FUTURE_SKEW`, tombstones, legacy labels, structural pattern checks
  and the failed-row shape.
- `src/brigade/fleet_mesh_pending.py` (new): the local pending request store
  and reply linking (design D5).
- `src/brigade/fleet_client_messages.py`: `list_messages()` (with
  `open_limit`), `sent()`, `ack()`, `sent_lookup` and the rows returned by
  `send()` and `reply()` pass through `open_rows()`.
- `src/brigade/fleet_mcp.py`: the new structural fields and error codes, and
  `key: {"fingerprint", "status"}` in `fleet_who`.
- `src/brigade/fleet_inbox_delivery.py`: open only the shown rows, with cached
  keys and the read-only pending store, and no extra hub call.
- `src/brigade/dot/hub_inbox.py`: replace the `row.get("reply_to") == msg_id`
  match at line 424 with a match of the verified `in_reply_to` against the
  pack's own record of the `client_id` it sent for `msg_id`.
- `src/brigade/doctor_*`: a clock offset warning.

Tests:
- `tests/test_fleet_mesh_open.py`:
  - A tampered ciphertext or wrong signer is rejected before `_frame()`, and
    the result contains no plaintext.
  - Another hub id, an envelope older than the window and a future-dated one
    each get their own code.
  - A replay under a new hub id opens as `replayed`, and the seen entry is
    persisted before the row is returned.
  - On a fresh station, an envelope created before the cache start opens as
    `unchecked`.
  - A hub `reply_to` pointing at a different request does not link the reply.
    A reply whose signed `in_reply_to` matches a local record links even when
    the hub's `reply_to` is altered. Presence mismatches open as
    `reply_mismatch`.
  - A reply signed after the request's deadline does not link.
  - Plain-text rows are labeled `unauthenticated-legacy`. A plain-text row
    from a pinned sender with no local pre-pin record is labeled
    `unauthenticated-after-pin`, whatever its hub timestamp.
  - A tombstone opens as `content_expired` with a verified header.
  - A failed row contains no sender-chosen string outside the fence.
  - `sender_label` is dropped for `agent:` senders and for `observed` keys.
- `tests/test_fleet_inbox_delivery.py`: a linked reply shows `reply:
  "linked"`, an unknown one shows `unlinked`, delivery completes no request,
  and an unopenable row renders the fixed body without raising.
- `tests/test_dot_inbox_tools.py`: a forged `reply_to` does not match, and a
  failed row does not raise.

Acceptance: the "tampered ciphertext or wrong signer is rejected before the
agent sees the text" bullet of #2005 holds. Run the focused gate with the
extra and with it blocked.

### Slice 6: sealing on send, reply, retry and the spool (needs 5, and #2010 for the MCP replay test)

Files:
- `src/brigade/fleet_mesh_send.py` (new): the seal decision table, key lookup
  through pins and `who`, the downgrade rule, a fresh content key on every
  seal, `rc`, local reply construction, pending request records, and retry
  recovery (409 with id, `sent_lookup`, header verification, `rc` comparison,
  the wrap fallback and `content_unverifiable`).
- `src/brigade/fleet_client_messages.py`: `send()`, `reply()` and the spool
  call into `fleet_mesh_send`. The spool stores the sealed envelope before the
  first transmission. `seal: "pending"` records for offline sends and replies.
  `_drain_locked` keeps and reseals on `recipient_key_stale` and
  `sender_key_stale`.
- `src/brigade/fleet_action_adapters.py`: `_inbox_main` sends through `send()`.
- `src/brigade/fleet_mesh_keys.py`: publish on first use and on rotate, and
  `local_key_superseded`.

Tests (`tests/test_fleet_mesh_send.py`):
- Every row of the decision table, including `observed` with and without
  require-verified, `pending_binding` and `changed`.
- The spool holds the sealed envelope before the first request, and a forced
  retry sends identical bytes.
- A stale-key 409 keeps the spool record and reseals it.
- Crash and resend with a deterministic `client_id` (patrol and Dot shapes)
  returns the stored id. The same `client_id` with a different body is a
  permanent 409.
- An MCP `fleet_send` retried with the same `client_id` and arguments returns
  the original id, and with a changed body gets #2010's permanent 409.
- Retry recovery after the stored row is purged to a tombstone still returns
  the stored id through `rc`.
- A hub that answers `sent_lookup` with a row not signed by the caller does
  not count as delivered.
- A pinned recipient with no key gets `recipient_key_missing`, never plain
  text.
- Round trips through MCP and through the message CLI, and a dump of every hub
  table contains neither the subject nor the body marker.

Acceptance: all four #2005 acceptance bullets hold, including the database
dump with real sealing.

### Slice 7: user docs for sealed mail (needs 6)

Files: `docs/fleet-messages-sealed.md` (new), links from the fleet messaging
docs and `docs/fleet-mcp-events.md`.

Content: install (the Intel Mac `--only-binary` route to 48.0.x, Windows
arm64, and `mesh_backend_unsupported` for builds without ML-KEM), hub and
local policy, key commands, how to compare fingerprints out of band, the local
boundary statement from design D3, what stays plain text, the threat model,
and, until slice 7b merges, the statement that ask_user answers are
hub-trusted.

Acceptance: `brigade guard scan` with
`src/brigade/guard/policies/public-repo.json` is clean, Vale reports no
errors, and the documented-command inventory test passes.

### Slice 7b: sealed operator questions (needs 6 and #1951 merged)

Files:
- `src/brigade/fleet_ask_user.py`: the question goes out as a sealed `kind:
  question` envelope with a signed deadline through `fleet_mesh_send`, not
  `messages._online()`. `_terminal()` is replaced by one verification function
  used for hub-inbox and desktop answers (design D7).
- `src/brigade/fleet_hub_questions.py`: `question_create` takes the envelope.
  The hub no longer builds the message. `fleet_questions` stores envelopes
  and verification metadata instead of `question` and `answer`. The trigger
  copies `NEW.envelope` and answer metadata.
- `docs/fleet-messages-sealed.md`: remove the hub-trusted note.

Tests (`tests/test_fleet_ask_user_sealed.py`):
- A hub that writes `status='answered'`, `source='operator_reply'` and
  `answered_by=<operator>` with its own answer text produces
  `operator_authenticated: false`.
- An answer signed by the operator's `verified` key, linked to this ask and
  inside the deadline, is authenticated. The same answer linked to a different
  pending question, signed after the deadline, sent to another principal, or
  signed by an `observed` operator key is not.
- A replayed operator answer from an earlier question is refused.
- A desktop answer without an operator signature is fenced and not
  authenticated. A desktop answer signed with the operator's own key on the
  same machine is authenticated.
- A dump of the hub database contains neither the question nor the answer
  marker.

Acceptance: no unsigned hub field can confer operator authority.

## #2006: teammate invites

### Slice 8: teammate role, symmetric grants and the audit log (no crypto)

Files:
- `src/brigade/fleet_hub_audit.py` (new): `fleet_audit_log`, an append helper
  that refuses secret-shaped values, admin paging, and aggregation for refused
  redemptions.
- `src/brigade/fleet_hub_messages.py`: `role` and `person` columns, audit rows
  for grant changes and revocation.
- `src/brigade/fleet_hub_message_grants.py`: one caller lookup helper that
  treats only enrolled node ids as unrestricted, replacing the prefix tests in
  `permits()`, `require_granted()`, `require_recipient_cap()`,
  `_station_read()` and `fleet_hub_events.permits()`. Symmetric grants for any
  pair that includes a teammate, both checked in the insert's
  `BEGIN IMMEDIATE` transaction.
- `_station_read`: teammates get `principal_role_forbidden` on `claims` and a
  reduced `who`.
- `src/brigade/fleet_hub_events.py`: teammates subscribe only to their own
  `message.created`.
- `src/brigade/fleet_hub_http.py`: `GET /audit` (admin).

Tests (`tests/test_fleet_hub_teammates.py`, extending
`tests/test_fleet_message_grants.py`):
- A teammate cannot read claims, subscribe to `claim.expired` or `run.failed`,
  or message principals outside its grant.
- Reciprocity: agent A to teammate T succeeds only when A's grant names T and
  T's grant names A, and the same holds from T to A. A node principal cannot
  message a teammate that has not granted it.
- Revocation: removing one side of a reciprocal grant blocks the next send in
  both directions. A grant revoked on another connection while a send waits on
  the write lock blocks that send.
- A principal id with an unknown prefix gets agent restrictions.
- Audit rows exist for grant changes and revocation and contain no token.

Acceptance: hub-side half of #2006's "a teammate principal cannot read claims,
submit work or message principals outside its grant".

### Slice 9: hub invites and idempotent redemption (needs 3 and 8)

Files:
- `src/brigade/fleet_hub_invites.py` (new): `fleet_invites` and
  `fleet_invite_uses`, admin `create`, `list` and `revoke`, redemption under
  `BEGIN IMMEDIATE` with the `join_attempt_id` rule of design D9 (following
  enrollment in `fleet_hub_messages.py:445-505`), constant-time digest
  comparison, one generic refusal, rate limits, principal creation with role,
  grants and client-supplied token digest, key and binding storage, audit
  rows.
- `src/brigade/fleet_hub_http.py`: `POST /invites` (admin) and
  `POST /invites/redeem`. No join page.

Tests (`tests/test_fleet_hub_invites.py`):
- A second redemption under a different attempt id fails. Expired and revoked
  invites fail. A wrong secret fails like an unknown id.
- The same attempt id with the same token, key record and name returns the
  same success without counting a use. The same attempt id with any value
  changed is refused.
- Two concurrent redemptions of a one-use invite produce exactly one
  principal.
- `max_uses` above one admits that many attempts and no more.
- The hub stores only `sha256(redeem_key)`, the token digest and the binding.
- `GET /join` returns 404.
- Removing a member makes its token return 401.

Acceptance: #2006's first two acceptance bullets hold against the Python hub.

### Slice 10: invite and join client (needs 2, 2b, 5, 6 and 9)

Files:
- `src/brigade/fleet_mesh_invites.py` (new): `bfi1` token build and parse,
  HKDF derivation, the persisted join attempt, the inviter's invite record
  holding `bind_key` until the binding is processed, `pending_binding`
  handling, binding verification, `binding_mismatch` and `binding_missing`.
- `src/brigade/cli/fleet_invites.py` (new): `fleet invite create | list |
  verify | revoke`, `fleet join --hub <url> --invite-file <path>` and
  `--invite-stdin`, `fleet members list | remove`, `fleet audit list`.
- `src/brigade/fleet_client_message_credentials.py`: a
  `write_message_token_file()` that mirrors the reader.

Tests (`tests/test_fleet_mesh_invites.py`):
- End to end: create, join, the joiner pins the inviter from the token, the
  inviter verifies the binding, and a sealed round trip works.
- Before the inviter verifies, sends to the new principal and mail from it are
  refused with `invite_binding_pending`.
- A hub that swaps the joiner's key is caught by the binding, and one that
  swaps the inviter's key is caught by the token fingerprint.
- A hub that serves no binding leaves the principal blocked as
  `binding_missing`, never `observed`.
- `bind_key` survives the invite's expiry until the binding is processed.
- A token whose hub id does not match the verified identity statement is
  refused before redemption.
- Response loss: the hub stores the redemption and the client sees a timeout.
  Rerunning the command installs the credential. Crash after redemption and
  before token install: restarting from the persisted attempt installs the
  same credential.
- `fleet join` refuses a token passed as a positional argument, and the token
  never appears in logs or error text.
- `invite create --as` a principal whose key is not local is refused.
- A `--bootstrap` token joins a fresh hub, and the next invite comes from the
  joined machine.
- A pre-mesh principal stays `observed` until `keys accept` with its
  fingerprint.

Acceptance: #2006's third bullet ("a teammate's sealed mail cannot be read by
the hub") holds, using the slice 6 database dump with a teammate sender.

## #2008: Hub Lite

### Slice 11: hub contract suite (needs 4 and 9)

Files:
- `tests/contract/fleet_hub_contract.py` (new): a backend-neutral HTTP suite
  that takes a base URL, an admin token and a storage dump callback. It covers
  identity, invites and idempotent redemption, `who`, sealed `send`, `list`
  with `wait_ms`, `count`, `ack`, `reply`, `sent`, `sent_lookup`, key publish
  and revoke, role restrictions, the envelope cap, admission errors, and the
  no-plaintext storage check, with two nodes.
- `tests/test_fleet_hub_contract_python.py` (new): runs it against the Python
  hub.
- `scripts/hub-contract` (new): runs it against a URL.

Acceptance: the suite passes on the Python hub.

### Slice 12a: Hub Lite skeleton under workerd (needs 11 and G4)

Files under `workers/hub-lite/`:
- `wrangler.toml` with one SQLite-backed Durable Object class,
  `workers_dev = true`, a `HUB_ADMIN_TOKEN` secret and `.dev.vars.example`.
- `package.json` pinning `wrangler` as a development dependency.
- `src/index.js` (routing, request limits, 404 for every route outside design
  D12) and `src/fleet.js` (the Durable Object: schema, token hashing,
  principals, identity statement storage, `GET /mesh/identity`, audit).
- `test/*.test.js` on `node:test` with a `node:sqlite` fake for fast checks.
- `.github/workflows/ci.yml`: a `workers` job for `workers/**` that runs
  `node --test`, then starts `wrangler dev` and runs the contract suite's
  identity and auth sections against it. It also runs
  `workers/github-webhook-relay` tests.

Acceptance: the identity and auth contract sections pass against
`wrangler dev` in CI.

### Slice 12b: Hub Lite keys and invites (needs 12a)

Files: `workers/hub-lite/src/fleet.js` and tests. `keys_publish`, `keys_get`,
key state in `who`, invites, `invite_uses` and idempotent redemption.

Acceptance: the key and invite contract sections, including the redemption
retry cases, pass against `wrangler dev`.

### Slice 12c: Hub Lite messages, admission and retention (needs 12b)

Files: `workers/hub-lite/src/fleet.js` and tests. Sealed-only `send` and
`reply`, the 32 KiB cap, `list` with waiters capped at 20 s, `count`, `ack`,
`sent`, `sent_lookup`, tombstones, the alarm that purges ciphertext at the
earlier of ack plus 7 days and creation plus 14 days, and the admission
ceilings with `hub_storage_full`, `sender_storage_quota` and
`hub_daily_quota`.

Tests: the messaging contract sections, a storage dump with no plaintext
marker, a send without an envelope refused, a 32 KiB plus one byte envelope
refused, each ceiling refusing before the next write, and the purge alarm
leaving a verifiable header.

Acceptance: the messaging contract sections pass against `wrangler dev`.

### Slice 13: Hub Lite presence and client throttles (needs 12c)

Files: `workers/hub-lite/src/fleet.js`, `src/brigade/fleet_session_presence.py`
and `src/brigade/fleet_inbox_delivery.py`, with tests.
- `POST /sessions` and `GET /sessions`, heartbeats in memory, rows written only
  on state, repository or branch change or once per third of the TTL.
- Client throttles applied when the identity statement reports a Hub Lite: one
  heartbeat per session per 60 seconds unless state changes, and a 60 second
  delivery poll.

Tests: a heartbeat storm writes a bounded number of rows, and the client
throttles hold under a burst of tool events.

Acceptance: the presence contract section passes against `wrangler dev`.

### Slice 14: deploy template, limits docs and live measurements (needs 10 and 13)

Files:
- `workers/hub-lite/README.md`: the deploy button, the `npx wrangler deploy`
  fallback, identity upload and bootstrap invite, the free plan limits and
  admission ceilings with what happens at each, one Hub Lite per account,
  optional Cloudflare Access for admin routes, and the claims deferral.

Operator step, recorded in the pull request: deploy to a fresh Free plan
account with the button, confirm the Durable Object migration applied, run
`scripts/hub-contract` with two nodes, and record rows written per message,
requests per waiting station, and the per-object storage limit. The design's
D12 figures, including the 1 GB assumption and the 800 MB ceiling, are
replaced by the measured numbers.

Acceptance: #2008's three acceptance bullets for messaging, invites and
presence. Claims are out of scope (decision Q7) and get their own issue after
this slice merges.

## #2007: no-hub mode

### Slice 15: spike (G2)

Files: `engines/mesh/` (new Rust crate, iroh 1.x) with a minimal binary that
opens an endpoint on ALPN `brigade-mesh/1` and exchanges one length-prefixed
frame. No Brigade integration.

Thresholds, fixed before measuring: stripped release binary at most 20 MB per
platform, endpoint ready within 2 seconds of a cold start, and 10 of 10
relayed connections between two machines on different networks through a
self-hosted `iroh-relay`. The direct-path rate and behavior with
`--relay none` on a LAN and a tailnet are recorded without a threshold.

Acceptance: a go or no-go recorded in design D10. On no-go, D10 is rewritten
to the fallback (LAN and tailnet only, Hub Lite for the rest) before any later
no-hub slice starts.

### Slice 15b: no-hub wire protocol (needs 15 with a go)

Files: `docs/phase-agent-mesh-wire.md` (new). Frame format and size limits,
transport receipts and signed application acks, outbox retry and backoff with
idempotency on `(from, client_id)`, per-contact admission (frame size, rate,
stored bytes), inbox caps and retention matching design D8, contact
revocation, and the `bmi1` invite exchange.

Acceptance: reviewed and merged before slice 17 or 18a starts.

### Slice 16: backend interface (any time)

Files:
- `src/brigade/fleet_backend.py` (new): a `MessageBackend` protocol and
  `HubBackend` wrapping the existing client functions.
- `src/brigade/fleet_mcp.py`, `src/brigade/fleet_inbox_delivery.py` and
  `src/brigade/cli/fleet_messages.py`: call through the backend.

Tests: the existing MCP and delivery tests pass unchanged, and `tool_specs()`
is identical for every backend.

Acceptance: pure refactor.

### Slice 17: local mesh store and backend (needs 1, 2, 5, 6, 15b and 16)

Files:
- `src/brigade/fleet_mesh_store.py` (new): SQLite inbox, outbox, contacts,
  receipts, the seen-id cache and pending requests in WAL mode, with the
  admission and storage limits of slice 15b.
- `src/brigade/fleet_mesh_backend.py` (new): `MeshBackend` per design D11,
  reusing `open_rows()` and `fleet_mesh_send`.
- `src/brigade/cli/fleet_mesh.py` (new): `mesh init --relay
  <url|n0-public|none>` with no default, `mesh status`, contacts, and `bmi1`
  tokens through `fleet join`.
- `fleet.mode` configuration and selection in `brigade fleet mcp`.

Tests (`tests/test_fleet_mesh_backend.py`), with a fake engine between two
temporary databases:
- Two nodes exchange sealed messages with no hub, through the same MCP calls
  the hub tests use, and the D6 replay and reply-linking tests pass against
  the mesh backend.
- An offline peer receives queued mail on reconnect, and the outbox drops
  entries after 7 days.
- `mesh init` without `--relay` refuses.
- `fleet_claims` returns the fixed code.

Acceptance: #2007's "the same MCP tool schemas work in hub and no-hub mode"
holds against the fake engine.

### Slice 18a: engine protocol crate (needs 15 and 17)

Files: `engines/mesh/src/proto/` and `.github/workflows/ci.yml` (cargo check,
clippy and test for `engines/mesh/**`). Framing, size limits, receipt types
and admission limits from slice 15b, with no networking loop.

Tests: Rust unit tests for framing, oversize frames, malformed receipts and
admission refusals.

### Slice 18b: engine daemon (needs 18a)

Files: `engines/mesh/src/daemon/`. Reads the outbox, sends frames, writes
received envelopes to the inbox, returns receipts, honors the relay setting
and exposes a status file. It never decrypts.

Tests: an integration test in CI starts `iroh-relay --dev`, runs two daemons
that can reach each other only through it, and exchanges a sealed message. A
capture at the relay contains no plaintext marker. Killing a daemon mid-send
and restarting it delivers exactly once.

### Slice 18c: Linux service, setup and doctor (needs 18b)

Files: a systemd user unit template, `brigade setup` installing the engine
like the code-graph engine, `brigade doctor` `WARN` or `MANUAL` checks for a
missing engine, a stopped daemon and an unset relay choice, and
`docs/fleet-no-hub.md` (setup, relay choices and what each exposes, and the
plain statement that claims, runs, patrol, dispatch and MCP Events need a
hub).

Acceptance: #2007's "two nodes on different networks exchange sealed messages
with no hub configured, and a relay cannot read them", shown by the slice 18b
relay test and one manual run across two real networks recorded in the pull
request.

### Slice 18d: macOS service (needs 18c)

Files: a launchd agent template, setup and doctor support. Tests: template
rendering and doctor checks.

### Slice 18e: Windows service (needs 18c)

Files: a scheduled task template, setup and doctor support. Tests: template
rendering and doctor checks.

### Slice 19: anchor mode (optional, needs 18b)

Files: `engines/mesh/` and `src/brigade/fleet_mesh_store.py`. An always-on
node started with `--anchor` holds sealed envelopes for contacts that opted in
and forwards them when the recipient connects, storing ciphertext and routing
ids only, with the same 7 day limit and the slice 15b admission limits.

Tests: delivery succeeds when sender and recipient are never online at the
same time, and the anchor's database contains no plaintext marker.

## Deferred

- Hub Lite claims (decision Q7): after slice 14, as a new issue.
- OS separation of keys and pins from agents (design D3).
- The `bm2` suite with ML-DSA signatures (design D2).
