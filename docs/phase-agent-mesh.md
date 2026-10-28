# Agent mesh: sealed Fleet Hub mail, teammate invites, no-hub mode and Hub Lite

Design for #2005, #2006, #2007 and #2008 (parent #1121). The implementation
plan is [phase-agent-mesh-plan.md](phase-agent-mesh-plan.md).

Operator direction: get end-to-end encryption working, let a user invite a
teammate's agents, and offer a no-hub option for people who do not want to run
or trust a hub. This document records the decisions and the reasons. It does
not change code.

Revision 2 (2026-10-22) answers an independent review that found the
cryptographic construction sound and the trust boundaries around it
incomplete. It also records the operator's decisions on the open questions.

Revision 3 (2026-10-22) answers the second review. Own-fleet `observed` keys
now need an operator-signed membership manifest (D3), revoked keys keep old
mail readable only with local receipt evidence and never with authority (D3),
signed statements carry rollback high-water marks (D3, D4), the hub identity
keeps a lineage (D4), `open_rows()` takes an explicit inbound or outbound
context (D6), question storage holds references instead of copies (D7),
invites admit every permitted use and keep each binding (D9), node principals
get grants (D9), the tool contract covers all eight tools (D11), and the `bm1`
wire profile is frozen (D2).

Revision 4 (2026-10-22) answers the third review, which found no blockers.
The current membership manifest now gates every use of an `observed` key, and
a removed member keeps its pin but is blocked as `observed_not_member` (D3).
Confidentiality is a per-envelope property taken from the key the envelope was
sealed to, recorded at seal time for sent mail (D5, D6). The receipt clock
bound is strict and counts the relative error of two clocks (D3). A question
retry reuses its persisted deadline and `client_id` (D5, D7). The hub identity
statement carries a signed `backend` and capability list (D4, D11). D3 states
what the rollback rule cannot detect on a fresh station and offers a trusted
minimum manifest as a mitigation.

Revision 5 (2026-10-27), proposed contract closure, answers the Daybreak,
Opus, Astra and Fable foundation reviews. D3 and D4 specify signed statement
bytes, bootstrap, trust precedence and atomic migration. D5 and D6 specify
attempt recovery and receipt transitions. D8 proposes separate retry receipts
and staged policy activation for parent, Astra and Daybreak review. These
proposals do not authorize implementation, migration or deployment. The frozen
`bm1` construction, invite, no-hub and Hub Lite scope remain unchanged.

Research was retrieved on 2026-10-22. Sources are listed at the end with their
URLs. Claims the research could not confirm from a primary source are marked
UNVERIFIED.

Planned commands appear without the `brigade` prefix, for example "the planned
`fleet keys accept` subcommand", because none of them exist yet. Commands
written with the prefix exist on `main` today.

## Decisions at a glance

| Topic | Decision |
|---|---|
| Crypto library | Optional extra `brigade-cli[mesh]` on pyca `cryptography>=50.0.2,<52`. G1 approved the extra. The current source and CI floor is 50.0.2, pending platform review (D1). |
| Suite | `bm1`, frozen: HPKE base mode `0x00`, KEM `0x647A` (X-Wing), KDF `0x0001` (HKDF-SHA256), AEAD `0x0002` (AES-256-GCM), plus Ed25519 signatures. The KEM and its HPKE binding are Internet-Drafts, pinned by revision. Version 1 ships no classical-only suite. |
| Envelope | A random single-use content key encrypts the payload with AES-256-GCM and a zero nonce. HPKE wraps that key for the recipient and for the sender. The routing header is bound through HPKE `info` and the payload AAD. An Ed25519 signature covers the whole envelope through digests of the ciphertext and the wraps. |
| Sealed fields | Subject, body, ref, thread and sender_label. |
| Routing fields | Sender, recipient, kind, priority, client_id, reply target, deadline, timestamps and padded size stay readable by the hub. |
| Keys | Separate Ed25519 signing and hybrid KEM keys per principal, generated where its token lives. Keys are `verified` (out-of-band fingerprint, invite binding or signed key manifest). A first key is `observed` only for a principal listed in the operator-signed membership manifest, and that path gives opportunistic confidentiality only. The current manifest gates every later use of an `observed` key, so a removed member is blocked as `observed_not_member` and keeps its pin. Every other unconfirmed key is `unverified` and blocked. Any change outside a signed continuity chain fails closed. |
| Confidentiality label | A property of each envelope, taken from the trust status of the key it was sealed to, separate from signer authentication. Sent mail records the recipient kid and its status at seal time. |
| Rotation | Signed successor chains of up to 8 links are accepted. Longer chains, and every change after a revocation, need operator verification. Clients keep historical keys with validity intervals. |
| Revocation | Mail signed by a revoked key stays readable as history only when this station's append-only receipt log shows it received that exact envelope strictly before `compromised_from` minus the relative clock error of two clocks and the timestamp precision. Such history never confers authority. |
| Signed statements | Hub identity statements, membership manifests, key manifests and revocation statements carry sequence numbers. Clients keep a high-water mark and refuse rollback and same-sequence conflicts. |
| Hub identity | An admin-held hub identity key. Its signed statement names the backend (`full` or `hub-lite`) and its capabilities. Clients bind it to the configured endpoint, separately from the pin store, and keep an authenticated lineage so old mail stays readable after a migration. A change fails closed unless the old identity signs a migration or the operator verifies the new hub id out of band. |
| Replies | Matched against a local record of each pending request, keyed by the signed `(hub, from, client_id)`, never by the hub's numeric `reply_to`. |
| Operator questions | Questions and answers from #1951 travel as sealed, signed messages. Operator authority comes only from a signature by the verified operator key. The hub's question table references message rows and holds no question text, answer text or hash of either. Until that slice lands, answers are documented as hub-trusted. Hub Lite and no-hub mode return `hub_feature_unavailable` for questions in version 1. |
| Invites | A bare token, `bfi1.<hub-id>.<invite-id>.<secret>.<inviter-fingerprint>`, redeemed only by the CLI from a file or stdin. The hub URL is a separate field. Each permitted use is a separate join attempt, retries of an attempt are idempotent, and each use keeps its binding and exact key record. Invited principals stay blocked until their binding verifies. |
| Retention | Ciphertext is cleared 7 days after ack and the signed header stays as a `content_expired` tombstone. A malicious hub cannot be forced to delete copies. |
| No-hub path | Brigade's own store-and-forward protocol over an iroh transport in an optional native engine. A bounded spike comes first, and the production daemon only after measured results and a written receipt, retry, admission and storage protocol. Reason Network interop is not pursued. |
| Tool contract | The eight fleet MCP tools keep the input schemas of the integration baseline in every mode. Outputs gain structural fields, and trust labels reach every rendered surface. Missing features return fixed error codes. Mesh mode never falls back to a configured hub. |
| Hub Lite | One Worker and one SQLite-backed Durable Object per fleet, sealed mail only, a 32 KiB envelope cap and storage admission limits. Claims and operator questions are deferred, which narrows #2008's scope. |
| Plain text by design | Claims, presence, run events, grants, keys, the audit log and message routing metadata. |

## Integration baseline

The design targets `main` plus four open pull requests, each of which changes a
surface this work touches. Slices that touch those surfaces wait for the
corresponding merge.

| PR | Head read for this revision | What it changes here |
|---|---|---|
| #1939 | a374638 | `fleet_who` and `fleet_claims` return pages from credential-scoped snapshots with opaque cursors (hub schema 41). Key entries served by `who` must not be frozen in those snapshots (D3). |
| #2009 | e410444 | A body-free `count` action and the `fleet_inbox` `count_only` argument. Sealed rows count like any other row. |
| #2010 | f52a68a | `fleet_send` and `fleet_reply` accept a sender-bound `client_id` (`^[A-Za-z0-9_-]+$`, at most 64 characters). A replay with identical arguments returns the original message id, and a different body gets a permanent 409. Tool annotations. |
| #1951 | fa041d4 | Operator questions: the eighth MCP tool, `fleet_ask_user`, the `fleet_questions` table and a trigger that records answers. D7 replaces its trust model and its storage. |

## Current state

This section records the original integration baseline. At foundation commit
`c32ad821d482a4714573bead711e5f44f01d6467`, crypto, key-directory and sealed-storage
foundations exist. Client opening, sealing and authority verification below
remain planned. Source inspection confirms that sealed uniqueness/lookup still
omit `hub`, retention is configurable from 1 through 3650 days, identity upload
can retain an unrelated migration, and plaintext replies to sealed parents are
accepted when plaintext is optional. D4, D5 and D8 require closure before client
integration. Foundation storage does not establish a deployed sealed-mail flow.

Current foundation status: the common Hub storage guard refuses plaintext
replies to retained sealed or `content_expired` parents even with plaintext
optional, including retries of legacy plaintext rows. Pre-upgrade plaintext
replies already stored remain readable under existing retention and are not
retroactively encrypted or quarantined. After retention deletes a parent,
its former content state cannot be classified. An existing detached
plaintext reply can be retried only with its exact non-null stored payload
hash. New replies to missing parents and ambiguous null-hash retries are
refused. This does not establish deployed enforcement or an end-to-end
sealed-mail flow. The remaining D4, D5 and D8 prerequisites below stay open.

The following baseline facts explain the original integration work.

- Messages live in the hub's `fleet_messages` table
  (`src/brigade/fleet_hub_messages.py`). `subject` and `body` are `TEXT NOT
  NULL` plaintext columns, next to `reference`, `thread`, `sender_label`,
  `kind`, `priority`, `client_id`, `reply_to` and the timestamps. Rows expire
  after `RETENTION_DAYS = 90`.
- `payload_hash` is SHA-256 over the plaintext recipient, subject, body,
  reference, priority, reply target and metadata. With the unique `(sender,
  client_id)` index it makes sends idempotent: the same `client_id` and hash
  returns the stored row, and a different hash is a conflict.
- `reply()` builds the reply subject on the hub as `Re: <original subject>` and
  copies the original `ref` and `thread`.
- Principals are node ids (`nodes` table, `src/brigade/fleet_hub.py`) and
  `agent:<name>` message principals (`fleet_message_principals`). Both store a
  SHA-256 digest of a bearer token. Node token rotation is "revoke, then add
  again".
- Principal enrollment (`fleet_hub_messages.py:445-505`) is already
  retry-safe: the client may supply its own `request_id` and token, the hub
  stores the token digest under `BEGIN IMMEDIATE`, and a retry with the same
  `request_id`, token and label succeeds again while a different one is a
  conflict. Invite redemption reuses this pattern (D9).
- Send grants (`src/brigade/fleet_hub_message_grants.py`) live in
  `fleet_message_send_grants(principal_id, pattern)` and restrict only
  `agent:` senders. `permits()` returns true for any sender id that does not
  start with `agent:`, so a node has no grant rows and no way to get one.
- The client (`src/brigade/fleet_client_messages.py`) posts every message
  action to `POST /messages`, and spools sends and replies to a local JSONL
  file when the hub is unreachable. Retries reuse the same `client_id`.
- The MCP station (`src/brigade/fleet_mcp.py`) exposes `fleet_who`,
  `fleet_send`, `fleet_inbox`, `fleet_ack`, `fleet_reply`, `fleet_sent` and
  `fleet_claims` on `main`, and #1951 adds `fleet_ask_user` as the eighth. `_frame()` fences every sender-chosen message field with the
  body. `_frame_directory()` fences non-structural strings in `who` and
  `claims`.
- Live delivery (`src/brigade/fleet_inbox_delivery.py`) runs from a prompt or
  tool hook, makes one hub call with a 2 second deadline, shows at most three
  messages, and reuses `_frame()`. It must never raise.
- The Dot pack matches replies by the hub's numeric `reply_to`
  (`src/brigade/dot/hub_inbox.py:424`: `row.get("reply_to") == msg_id`).
- In #1951, `AskUser.ask` sends questions through `messages._online()`
  directly (`fleet_ask_user.py:223`), so a question bypasses `send()`. The hub
  builds the question message itself with `messages.send()`
  (`fleet_hub_questions.py:165`) and stores the question text in plaintext in
  `fleet_questions`. A trigger on `fleet_messages` copies `NEW.body` into
  `fleet_questions.answer` when the configured operator replies.
  `_terminal()` grants operator authority when the hub-returned `source` is
  `operator_reply` and `answered_by` equals `operator`
  (`fleet_ask_user.py:185`). Every field in that decision comes from the hub.
  `fleet_questions.payload_hash` is SHA-256 over the JSON list `[question,
  timeout]` (`fleet_hub_questions.py:142`). The timeout is an integer from 1 to
  120, so for a short question the hash is a dictionary oracle: anyone holding
  the database can confirm a guessed question.
- Three surfaces copy a fixed set of message fields and drop the rest. The
  live delivery header (`fleet_inbox_delivery.render()`, line 326) prints the
  id, sender, kind, `reply_to` and the injection flag. The Dot pack's persisted
  projection (`DotState.hub_upsert()`, `src/brigade/dot/state.py:195`) stores
  a fixed tuple of twelve fields. The legacy projection
  (`src/brigade/hub_messages.py:107`, `legacy_message()`) builds a dict with
  ten fixed keys. A trust label added only to opened rows would not reach any
  of them.
- MCP Events (`docs/fleet-mcp-events.md`) keep `message.created` payloads
  thin: ids, sender, recipient, priority, kind and timestamps.
- Dashboard enrollment codes (`src/brigade/fleet_hub_enrollment.py`) are
  random, stored as digests, expire after 300 seconds, and are consumed under
  `BEGIN IMMEDIATE`.
- The Dot connector's Cloudflare Access check (`src/brigade/dot/access_auth.py`)
  imports `cryptography` lazily and raises `OptionalDependencyError` when it is
  missing. The `grokbot` extra already pulls in `cryptography` through
  `pyjwt[crypto]`.
- `workers/github-webhook-relay/` is the only Worker in the repo. It uses D1,
  plain JavaScript and `node:test` with hand-written fakes, and no CI job runs
  its tests.

## D1. Crypto library, the optional extra and the suite

Python's standard library has no X25519, Ed25519, ML-KEM, AEAD or HKDF. The
3.14 "Cryptographic Services" chapter lists only `hashlib`, `hmac` and
`secrets` [PY]. Brigade keeps zero runtime dependencies, so sealing ships as
an extra:

```toml
[project.optional-dependencies]
mesh = ["cryptography>=50.0.2,<52"]
```

Comparison as of 2026-10-22 [CRY-PYPI] [CRY-CHG] [NACL-PYPI] [NACL-CHG]:

| | pyca `cryptography` 50.0.2 | PyNaCl 1.6.2 |
|---|---|---|
| Released | 2026-09-30, 11 releases in 12 months | 2026-01-01, 2 releases in 12 months |
| X25519, Ed25519, HKDF | yes, yes, yes | yes, yes, no HKDF |
| AEAD | AES-GCM, ChaCha20-Poly1305 | XChaCha20-Poly1305, AES-GCM bindings |
| HPKE (RFC 9180) | yes, `cryptography.hazmat.primitives.hpke`, since 47.0.0 | no |
| Post-quantum KEM in HPKE | `KEM.MLKEM768_X25519` (X-Wing), `KEM.MLKEM768`, `KEM.MLKEM1024`, `KEM.MLKEM1024_P384`, on backends with ML-KEM. The official wheels have ML-KEM since 48.0.0. | no |
| macOS wheels | arm64 only since 49.0.0. 48.0.x still shipped `universal2` | `universal2` |
| Windows wheels | amd64 only | amd64, arm64, win32 |
| Already in Brigade's dependency tree | yes, through `grokbot` and the Dot Access check | no |

`cryptography` is the only candidate with HPKE and a hybrid post-quantum KEM,
so Brigade assembles no KEM, KDF or AEAD by hand.

Post-quantum check (finding from the operator's brief). The stable HPKE
documentation for 50.0.2 lists `KEM.MLKEM768_X25519`, described as "a hybrid
KEM combining ML-KEM-768 with X25519 (also known as X-Wing)", available only
on backends that support ML-KEM, with key classes `MLKEM768X25519PrivateKey`
and `MLKEM768X25519PublicKey` added in 47.0.0 [CRY-HPKE]. The 48.0.0
changelog adds ML-KEM when built against OpenSSL 3.5.0 or later and says
"this means post-quantum algorithms are now available to users of our wheels"
[CRY-CHG]. On 2026-10-22 a 48.0.0 wheel on Linux x86_64 produced a 1168 byte
HPKE ciphertext for a 32 byte key under `MLKEM768_X25519`, against 80 bytes
under `X25519`, and decrypted it [CRY-MEAS]. The hybrid KEM is therefore the
default and only suite in slice 1. That historical measurement does not
establish support on other platforms or versions.

The original G1 rationale chose floor 48 for wheel ML-KEM availability.
Current `pyproject.toml` and mesh CI use `>=50.0.2,<52` after the dependency
update. Keep that floor pending a source-backed platform review. This revision
changes no dependency or CI configuration. The earlier Intel macOS guidance
that `--only-binary cryptography` would resolve to 48.0.x is stale with this
floor and must not be offered as an installation route. Intel macOS and Windows
arm64 support need fresh platform evidence before install guidance is published.
The comparison above and [CRY-MEAS] are dated research, not a current platform
support guarantee.

A source build or distribution package may lack the required backend. The
client probes on first use and fails with `mesh_backend_unsupported` rather
than selecting a classical suite. The floor/newest cross-version acceptance
now means 50.0.2 and the newest version within `<52`. Review supported wheels,
backend availability and the pinned-profile fixtures before changing that floor.

### The `bm1` wire profile, frozen before implementation

The normative encoding subsection in D2 is part of this frozen profile.
Together with the pinned primitives below, it fully pins the meaning of `bm1`.

| Parameter | Value |
|---|---|
| HPKE mode | base, `0x00` |
| KEM | `0x647A`, X-Wing (MLKEM768-X25519), as specified by draft-connolly-cfrg-xwing-kem revision 11 (2026-09-23) [XWING] |
| HPKE binding of the KEM | draft-ietf-hpke-pq revision 05 (2026-07-06) [HPKE-PQ] |
| KDF | `0x0001`, HKDF-SHA256 |
| AEAD | `0x0002`, AES-256-GCM |
| Public key (`enc_pub`) | 1216 bytes: the ML-KEM-768 encapsulation key (1184 bytes), then the X25519 public key (32 bytes) |
| Private key | the 32 byte X-Wing seed |
| `enc` | 1120 bytes: the ML-KEM-768 ciphertext (1088 bytes), then the X25519 ephemeral public key (32 bytes) |
| Wrap of a 32 byte content key | 1168 bytes: `enc`, then 32 bytes of AES-256-GCM ciphertext and its 16 byte tag |

Two hash functions appear and do different jobs. X-Wing combines the two
shared secrets inside the KEM with SHA3-256 over `ss_M`, `ss_X`, `ct_X`,
`pk_X` and the label `5c2e2f2f5e5c` [XWING]. HKDF-SHA256 is only the HPKE key
schedule that turns the KEM's shared secret into the AEAD key and nonce. HPKE
never sees the component secrets.

Both documents are Internet-Drafts, and neither is an RFC. draft-ietf-hpke-pq is a working
group draft of the IETF HPKE working group. The X-Wing draft is an
individual submission on the Independent Submission stream, which the
datatracker describes as "not endorsed by the IETF" [XWING]. Either can change
before publication. The pinned revisions define `bm1`, and its meaning never
changes: if a later revision or the published RFC computes anything
differently (key expansion, the combiner, sizes, the key schedule or the code
points), Brigade keeps `bm1` as specified here and defines the new behavior
under a new suite id. Rotation (D3) moves keys to the new suite.

Private keys are stored as the 32 byte X-Wing seed. pyca builds the hybrid
private key from its two parts [CRY-HPKE], so the client expands the seed the
way the X-Wing draft does, with `hashlib.shake_256(seed).digest(96)`: bytes 0
to 63 are the ML-KEM-768 seed for `MLKEM768PrivateKey.from_seed_bytes()`
[CRY-MLKEM] and bytes 64 to 95 are the X25519 private key. Storing the seed
keeps keys portable to any other X-Wing implementation and lets slice 1 test
key generation against the draft's vectors.

Known-answer vectors, and a gap in them. Neither draft ships a vector for this
exact suite. hpke-pq revision 05 has X-Wing vectors only with
ChaCha20-Poly1305 (Appendix A.5: KEM `0x647A`, KDF `0x0001`, AEAD `0x0003`),
and its JSON vector file has no `0x647A` entry with AEAD `0x0002`
[HPKE-PQ-TV]. pyca's one-shot API takes no associated data and only sequence
number 0 [CRY-HPKE], while the published vectors encrypt with associated data,
so pyca cannot consume them directly. Slice 1 therefore checks three layers:

1. The KEM against the X-Wing draft's vectors: seed to public key, and
   decapsulation to the shared secret, using pyca's ML-KEM and X25519
   primitives and `hashlib.sha3_256` for the combiner.
2. The HPKE key schedule for `0x647A` with `0x0001` against Appendix A.5: a
   test-only RFC 9180 key schedule (`hmac` and `hashlib`, about 40 lines)
   must reproduce the vector's `key`, `base_nonce` and first ciphertext.
3. pyca against that reference: the reference schedule seals a message under
   AEAD `0x0002` with empty associated data, and pyca's
   `Suite(KEM.MLKEM768_X25519, KDF.HKDF_SHA256, AEAD.AES_256_GCM).decrypt`
   must open it, and the reverse.

A checked-in `bm1` envelope fixture, generated once and verified by both pyca
and the reference schedule, guards against drift. Cross-version check: CI
seals a fixture with the floor (50.0.2) and the newest release, and each version
must open the other's envelopes and the checked-in fixture byte for byte.

Size budget. The round 2 review measured a 20 KiB padded payload at a 31,639
byte envelope [ASTRA-MEAS], which fits Hub Lite's 32 KiB (32,768 byte) cap
with about 1.1 KiB to spare, so the 20 KiB payload budget for Hub Lite stays.

Without the extra, nothing changes for plain-text mail. Sealed mail shows up as
a row with `sealed: true` and `open_error: "mesh_extra_missing"`. Sending to a
hub that requires sealing fails with `message_sealing_required`.

## D2. Envelope format

A sealed message is one JSON object, stored by the hub as text and handed to
recipients unchanged. Binary values are unpadded base64url. Every header value
is an integer, an ASCII string checked by a fixed pattern, or an object of
those, so the canonical form (`json.dumps(..., sort_keys=True,
separators=(",", ":"), ensure_ascii=True)`) is the same in Python and
JavaScript.

```json
{
  "v": 1,
  "suite": "bm1",
  "hub": "h_3q9GZ2pX8vN0aT1kLm4w",
  "from": "node-a",
  "from_kid": "k1_<sender kid>",
  "to": "agent:dana.laptop",
  "kind": "question",
  "priority": "normal",
  "client_id": "c0ffee00c0ffee00c0ffee00c0ffee00",
  "created_at": "2026-10-22T12:00:00+00:00",
  "deadline": "2026-10-22T12:05:00+00:00",
  "in_reply_to": {"hub": "h_3q9GZ2pX8vN0aT1kLm4w", "from": "agent:dana.laptop", "client_id": "..."},
  "pad": 1024,
  "rcpt": ["k1_<recipient>", "k1_<sender>"],
  "ct_sha256": "<SHA-256 of ct>",
  "wraps_sha256": "<SHA-256 of canonical(recipients)>",
  "rc": "<retry commitment, D5>",
  "recipients": [
    {"kid": "k1_<recipient>", "wrap": "<HPKE ciphertext of the content key>"},
    {"kid": "k1_<sender>", "wrap": "<the same, sealed to the sender's own key>"}
  ],
  "ct": "<AES-256-GCM ciphertext of the padded payload>",
  "sig": "<Ed25519 signature>"
}
```

`hub` is the hub id (D4), or the literal `mesh` in no-hub mode. `deadline` is
present only on `kind: question`. `in_reply_to` is absent on a new message.
It names the original's full `(hub, from, client_id)` triple, so a reply sent
after a hub identity migration still links to a request made under the
previous hub id (D4).
`client_id` follows #2010's pattern, `^[A-Za-z0-9_-]{1,64}$`. A caller that
does not supply one gets 32 random hex characters.

Three nested parts:

- The routing header `rh`: `v`, `suite`, `hub`, `from`, `from_kid`, `to`,
  `kind`, `priority`, `client_id`, `created_at`, `deadline`, `in_reply_to`,
  `pad` and `rcpt`.
- The signed header: `rh` plus `ct_sha256`, `wraps_sha256` and `rc`.
- The envelope: the signed header plus `recipients`, `ct` and `sig`.

What is sealed. The payload holds `subject`, `body`, `ref`, `thread` and
`sender_label` as binary fields (a one byte tag, a four byte big-endian
length, the UTF-8 bytes). It is padded with one `0x80` byte and zeros up to a
multiple of 1 KiB (minimum 1 KiB), so the stored size reveals the length only
to the nearest kilobyte. `pad` records the bucket.

### Normative bm1 encoding

The following rules are normative for `bm1`. A change to these rules needs a
new suite id. With this text, bm1's meaning is fully pinned.

Payload fields use these one-byte tags, followed by an unsigned four-byte
big-endian UTF-8 byte length and exactly that many UTF-8 bytes:

| Tag | Field | Presence |
|---|---|---|
| 1 | `subject` | required |
| 2 | `body` | required |
| 3 | `ref` | optional |
| 4 | `thread` | optional |
| 5 | `sender_label` | optional |

Tags MUST be unique and strictly ascending. Readers MUST reject unknown tags,
duplicates, reordered tags, missing required fields and invalid UTF-8. Optional
fields whose value is `None` MUST be omitted, and omission decodes to `None`.
An explicitly empty `ref` MUST be encoded as tag 3 with length zero and MUST
decode to `""`, distinct from `None`. Required text MUST contain at least one
non-whitespace character, using the existing messaging validator's
`value.strip()` test. The existing byte limits and thread and label patterns
apply. A thread or label cannot be empty. After the last field, exactly one
`0x80` byte and the minimum number of zero bytes bring the length to a multiple
of 1024 bytes, with a minimum of 1024. Readers MUST reject nonzero padding or
an extra padding bucket.

`rcpt` MUST contain unique kids sorted in ASCII lexicographic order.
`recipients` MUST contain exactly one `{kid, wrap}` object for each kid, in
that same order. Self-send deduplicates the sender and recipient kid into one
entry and one wrap. A sealer MUST reject a self-send whose recipient record is
not the sender's own record (for example, a stale pre-rotation record), and
MUST reject distinct principals that share a kid. Later slices MUST address
wraps by kid, never by position.

Timestamps MUST match this exact ASCII grammar and name a valid calendar time:

```text
YYYY-MM-DDTHH:MM:SS[.fff[fff]](Z|+HH:MM|-HH:MM)
```

The separators and uppercase `T` and `Z` are literal. All digit positions use
ASCII digits. Fractions have zero, three or six digits only. `Z` denotes UTC.
An explicit offset has hours 00 through 23 and minutes 00 through 59, including
`+00:00` for UTC. Seconds range from 00 through 59, with no leap-second form.
Readers MUST reject every other spelling. Continuity compares instants after
applying the offset, not the timestamp strings.

All envelope authentication entry points MUST accept raw bytes through
`parse_envelope(raw: bytes)` before any normalization. The raw envelope MUST
be ASCII. Non-ASCII bytes are rejected before parsing. Readers MUST also reject
NUL bytes before the structural scan, decode as ASCII and pass text to the JSON
parser so encoding auto-detection cannot run. Before allocating a JSON tree, it
MUST reject more than 99,328 bytes (the 96 KiB envelope cap plus a fixed
1024-byte parsing margin) and container nesting deeper than four levels,
counting the root object as level one. JSON objects MUST reject duplicate keys
recursively, including the root, `in_reply_to` and every recipient object.
The parser MUST validate the envelope schema, including binary lengths,
recipient order and the complete-content or tombstone shape, then re-encode
with `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=True,
allow_nan=False).encode("ascii")`. It MUST reject a re-encoded envelope larger
than 98,304 bytes and any input whose bytes differ from this encoding.
Whitespace, alternate key order, byte-order marks, alternate escapes and
trailing newlines are therefore rejected. Each accepted message has exactly
one raw byte representation.

Every whole-envelope digest, including the receipt log, seen-id cache, outbox
and spool `payload_hash`, MUST be SHA-256 of those canonical raw bytes.
Structured hash inputs such as `recipients` and signed statements use the same
canonical encoder. `ct_sha256` continues to hash the decoded ciphertext bytes,
and primitive binary derivations keep their specified binary inputs.

How it is built:

1. The sender fixes `rh`. `rhb` is `canonical(rh)`.
2. A fresh random 32 byte content key `cek` encrypts the padded payload with
   AES-256-GCM, a zero nonce and associated data
   `b"brigade-mesh/v1/aad\0" + rhb`. A zero nonce is safe only because each
   content key encrypts exactly one message. Every seal, including a reseal
   after `recipient_key_stale` and a retry that reseals the same content,
   draws a new `cek`. No code path reuses a content key recovered from the
   sender's own wrap, because the same key and nonce under different
   associated data would expose the GCM authentication key.
3. For each kid in `rcpt`, `Suite(KEM.MLKEM768_X25519, KDF.HKDF_SHA256,
   AEAD.AES_256_GCM).encrypt(cek, recipient_public_key, info)` with
   `info = b"brigade-mesh/v1/cek\0" + sha256(rhb)`. The pyca one-shot API
   takes `info` and no associated data, so `rh` is bound through `info`
   [CRY-HPKE].
4. `ct_sha256 = sha256(ct)`, `wraps_sha256 = sha256(canonical(recipients))`,
   and `rc` per D5.
5. The sender signs `b"brigade-mesh/v1/sig\0" + canonical(signed header)` with
   its Ed25519 key.

What the signature covers. Through the two digests, the signature covers the
whole envelope: version and suite, both identities and the sender's kid, the
hub id, the sealed key copies, the routing header and the ciphertext. Any
change to any of them fails verification. Because the signed header is
verifiable on its own, it survives ciphertext purge as a `content_expired`
tombstone (D8) and routing stays verifiable.

Why this shape:

- HPKE base mode is anonymous, so the Ed25519 signature authenticates the
  sender. An attacker who strips the signature and re-signs the ciphertext
  under its own name must change `from`, which changes `rhb`, so both the
  recipient's HPKE open and the payload's AEAD check fail. A sender cannot
  claim a message it could not read.
- Re-addressing and surreptitious forwarding fail. A recipient that forwards a
  message to a third party unchanged leaves `to` naming itself, and the third
  party refuses it (D6 step 2). Changing `to` or `rcpt` breaks the signature,
  and only the original sender can sign again.
- The hub checks `from`, `to`, `from_kid` and the recipient kid against its
  own records with string comparisons, so the Python hub needs no crypto
  library.
- AES-GCM instead of ChaCha20-Poly1305 because Workers WebCrypto implements
  AES-GCM and not ChaCha20 [CF-WC]. WebCrypto has no ML-KEM, so a future
  browser client (the desktop panel in #1887 slice 6) would need a WebAssembly
  ML-KEM to open mail. Hub Lite never decrypts.
- Wrapping a content key keeps a large body from being duplicated between
  the sender and recipient copies. `bm1` admits those two parties only.
  AES-GCM does not commit to one key: a malicious sender can construct different
  sender/recipient views using different wrapped keys. A sender-wrap recovery
  proves only the sender's own view. Multi-recipient or stronger
  audit claims require separately reviewed `bm2` work, not a `bm1` change.

Replay identity. `(hub, from, client_id)` is the unique per-sender message id.
Sealed storage must enforce `(hub, sender, client_id)` before client integration
(D5, D8). The current `(sender, client_id)` index does not do that. Each
client's seen-id cache enforces it locally whatever the hub does (D6 step 5).
The signed `created_at` is the timestamp the freshness check uses.

Key separation. No key material serves two purposes, and every signature or
derivation context carries its own label, so a value produced in one context
cannot be replayed in another.

| Key | Used only for |
|---|---|
| Ed25519 identity signing key (per principal) | envelope signatures, key records, signed ack receipts in no-hub mode |
| Hybrid KEM key pair, ML-KEM-768 plus X25519 (per principal) | HPKE wraps under `bm1`. A later suite gets its own key pair. |
| Content key `cek` (per message) | that message's payload |
| Retry key (per principal, 32 random bytes) | retry commitments and local ask idempotency records (D5), under separate labels |
| iroh endpoint key (per machine, no-hub mode) | QUIC transport |
| Hub identity key (admin-held, per hub) | hub identity statements and hub migrations (D4) |
| Operator manifest key (admin-held) | membership manifests, key manifests and revocation statements (D3) |
| Invite secret (per invite) | HKDF into `redeem_key` and `bind_key` (D9) |

Labels: `brigade-mesh/v1/aad`, `/cek`, `/sig`, `/key`, `/kid`, `/retry`,
`/ask`, `/hub-id`, `/hub-identity`, `/hub-migration`, `/manifest`,
`/membership`, `/revocation` and `/ack`, and `brigade-invite/v1/redeem` and
`/bind`.

Crypto agility. `v` and `suite` sit in the signed header and in `rh`, so they
are bound into the KEM and the AEAD as well. Key records list the suites they
accept, under the record's signature. A sender seals only with a suite the
recipient's record lists, and a reader refuses an unknown `v` or `suite` with
`envelope_unsupported`. Version 1 defines `bm1` alone, so there is no
negotiation to downgrade. A later suite, for example `bm2` adding an ML-DSA
signature next to Ed25519 (pyca ships ML-DSA in the same wheels [CRY-CHG]),
arrives as a new key pair published through rotation (D3). Dropping a suite is
also a rotation.

Harvest now, decrypt later. Sealed bodies rest on a hub, so anyone who copies
them today could try to decrypt them with a future quantum computer. The
hybrid KEM keeps them confidential while either ML-KEM-768 or X25519 holds.
Signatures are not post-quantum: a future quantum adversary could forge
Ed25519 signatures on archived mail, which affects the authenticity of old
records but not their confidentiality. The `bm2` path above addresses it.

Limits. The plaintext limits stay as they are: 64 KiB for the body
(`BODY_LIMIT`), 200 bytes for the subject, 256 for `ref`, plus the thread and
label patterns. The largest payload pads to 65 KiB, about 88 KiB in base64url.
Each hybrid wrap is 1168 bytes, about 1.5 KiB encoded, so two wraps and the
header add about 4 KiB, for about 92 KiB in all. The Python hub caps an
envelope at 96 KiB. Every hub advertises `max_envelope_bytes` in its identity
statement (D4). Hub Lite advertises 32 KiB, which leaves room for a payload
bucket of about 20 KiB. The client checks the encoded size before sending and
fails with `message_too_large`. The list page byte cap
(`MAX_LIST_RESPONSE_BYTES`, 7 MiB) already splits pages by size.

## D3. Keys: publication, trust, acceptance, rotation and revocation

Each principal (a node id or an `agent:` principal) has one active key record
per hub identity:

```json
{
  "v": 1,
  "principal": "agent:dana.laptop",
  "hub": "h_3q9GZ2pX8vN0aT1kLm4w",
  "kid": "k1_...",
  "sig_pub": "<Ed25519 public key, 32 bytes>",
  "enc_pub": "<ML-KEM-768 public key, 1184 bytes, then X25519 public key, 32 bytes>",
  "suites": ["bm1"],
  "transport": {"iroh": "<endpoint id>"},
  "created_at": "2026-10-22T12:00:00+00:00",
  "prev_kid": "k1_...",
  "self_sig": "<signature by sig_pub>",
  "prev_sig": "<signature by the previous key>"
}
```

pyca exposes the hybrid public key only as its two components, so the record
stores them concatenated in X-Wing order and the client rebuilds
`MLKEM768X25519PublicKey` from them.

`kid` is `k1_` plus the first 16 bytes of `sha256(b"brigade-mesh/v1/kid\0" +
sig_pub + enc_pub)`. The fingerprint is the full SHA-256 of `sig_pub`, shown as
52 base32 characters in groups of four. Invite tokens carry its first 16
bytes. `self_sig` and `prev_sig` sign `b"brigade-mesh/v1/key\0" +
canonical(record without self_sig and prev_sig)`, so a record cannot be
replayed onto another principal or hub, and its suites cannot be downgraded.
Every check that accepts a key by fingerprint also verifies `self_sig`, which
authenticates `enc_pub`.

Generation and storage. Keys are generated where the principal's token lives,
on first need. Private keys, the retry key and retired keys are stored under
`<brigade_home>/fleet/mesh-keys/` with the owner-only, no-symlink rules that
`read_message_token_file` applies to token files. Private keys never leave the
machine. The planned `fleet keys show` subcommand prints this machine's
principal, kid and fingerprint, which is what a peer compares out of band.

The current local custody foundation requires `<brigade_home>`, its `fleet`
directory and its private `mesh-keys` directory to be owned by the current
user with exactly 0700 permissions. Private files require exactly 0600.
No component anywhere in the custody path, including ancestors above
`<brigade_home>`, may be a symlink. If a home or temporary-directory ancestor
is symlinked, the operator must choose an already physical path. Custody does
not resolve that path automatically and refuses symlinks with
`mesh_keys_unsafe`. Native Windows custody is unsupported and fails with
`mesh_keys_platform_unsupported` before filesystem access.

Custody retains retired keys without automatic pruning. If the serialized
history would exceed 8 MiB, rotation fails closed with `mesh_keys_invalid`
and leaves the saved generation unchanged. A dedicated capacity diagnostic
is deferred. This bound does not authorize deleting retained private keys.

Publication. A new `keys_publish` action on `POST /messages` takes a key
record. The hub requires `principal` to equal the authenticated caller and
checks formats. If the caller already has an active key, the new record must
carry `prev_kid` equal to it and a `prev_sig`, or the hub answers
`principal_key_exists`. The hub cannot verify signatures, so clients do.
Anyone holding the token can still publish a record with a junk `prev_sig`.
Peers then see `changed` and fail closed, and the original machine's next send
fails the hub's `from_kid` check, which the client reports as
`local_key_superseded`.

Serving. `who` adds a `keys` entry for each visible principal, and `keys_get`
returns any record by kid. With #1939, `who` pages come from snapshots that
live up to 10 minutes. Key entries are not part of the snapshot: each page
reads the current key state of the principals on it at continuation time, the
same way #1939 rechecks grants, so a key published or revoked mid-traversal is
reflected on the next page. `fleet_who` shows agents only `key:
{"fingerprint", "status"}`, both from fixed formats.

Trust levels. The client keeps a pin store per accepted hub identity (D4), with
one entry per principal:

| Status | Meaning | Effect |
|---|---|---|
| `verified` | The fingerprint matched a value the operator supplied, an invite binding, or a signed key manifest. | Full use. Required for anything that confers authority. Rows carry `confidentiality: "end_to_end"`. |
| `observed` | The first key the hub served for a principal that the accepted membership manifest lists, with nothing confirming the key itself. | Sealed mail works and later changes fail closed, but the key never confers authority: no operator answers (D7) and no `sender_label`. Rows and `fleet_who` entries carry `confidentiality: "opportunistic"`. |
| `observed_not_member` | An `observed` pin whose principal the current accepted membership manifest no longer lists. Computed at each use from the pin and the manifest, never stored. | The pin and its key history stay. Sends fail with `recipient_key_not_member`, mail opens as `sender_key_not_member`, and the key confers nothing. |
| `unverified` | A key served for a principal that no accepted membership manifest lists, and that nothing has verified. | Sends fail with `recipient_key_unverified` and mail opens as `sender_key_unverified`. |
| `pending_binding` | An invited principal whose binding has not been verified yet on the inviting station (D9). | Sends and opens are refused with `invite_binding_pending`. |
| `changed` | A record that does not follow from the pinned key. | Sends fail with `recipient_key_changed` and mail opens as `sender_key_changed`. |
| `revoked` | The hub or a signed statement revoked the key. | See revocation below. |

Membership decides who may be `observed`. A malicious hub controls every
field it serves, including a principal's `role`, so a hub-provided role never
selects the weaker path. The hub could otherwise present a teammate, or a
principal it minted, as an ordinary own-fleet agent to a station that did not
create the invite, and the first-key rule would let it substitute that key.
The only input that allows `observed` is an operator-signed membership
manifest:

```json
{
  "v": 1,
  "hub": "h_3q9GZ2pX8vN0aT1kLm4w",
  "seq": 7,
  "created_at": "2026-10-22T12:00:00+00:00",
  "members": ["node-a", "node-b", "agent:build.node-a"],
  "sig": "<Ed25519 signature by the operator manifest key>"
}
```

The example is schematic: producers sort `members` in ASCII order. It is signed under `brigade-mesh/v1/membership` with the operator manifest
key, the same key whose fingerprint each machine pins once out of band with a
planned `fleet keys trust-manifest-key --public-key-file <file>
--fingerprint <fp>`. The file is exactly the unpadded base64url encoding of a
32-byte Ed25519 public key, with no whitespace or newline. The fingerprint is
supplied independently over a channel outside the hub and must exactly match
D3's full grouped base32 SHA-256 fingerprint. No hub response installs or
replaces this trust anchor. A mismatch changes nothing. Replacement is an
explicit operator action and retains the old anchor and its marks as history. It lists principal ids only, never keys or
roles. The admin builds it with a planned `fleet keys membership sign`, which
proposes the nodes and agent principals the admin enrolled, shows each one for
confirmation, and leaves out every principal created by a teammate invite.
The proposal comes from the hub, so the operator's confirmation is the check,
and the signature is what stations trust. Stations import it from a file with
`fleet keys membership import <file>`, or fetch the latest copy the hub stores
as an opaque blob (D8). Either way the client verifies the signature and the
high-water rule below before using it. A station with no accepted membership
manifest treats every unconfirmed key as `unverified`.

The decision for a principal with no pin:

1. A signed key manifest entry, an operator-supplied fingerprint or, on the
   inviting station, a verified invite binding makes it `verified`.
2. Otherwise, a principal listed in the accepted membership manifest becomes
   `observed`, unless `BRIGADE_FLEET_MESSAGES_REQUIRE_VERIFIED=1` is set, in
   which case it stays `unverified`.
3. Everything else is `unverified`: teammates on any station other than the
   inviting one, principals the hub reports as nodes or agents but the
   manifest does not list, and any principal the hub merely claims.

A teammate becomes `verified` on other stations after the inviting station
verifies its binding (D9) and the operator adds its fingerprint to a signed key
manifest, which those stations accept with `fleet keys accept --manifest`.

Membership gates every use of an `observed` key, at first contact and after
it. The manifest that allowed the pin is the one accepted at that moment, and
membership can shrink. Take manifest 7 listing P, P pinned `observed`, and
manifest 8 removing P. From the moment manifest 8 is accepted:

- Every use of P's `observed` key checks the current accepted manifest:
  sealing to it, opening mail from it, linking a reply from it (D5), and any
  authority, which `observed` never had. P reports as `observed_not_member`.
  Sends fail with `recipient_key_not_member` and inbound mail from P opens as
  `sender_key_not_member`, including mail that arrived while P was listed.
- The pin and its key history are kept. A later first contact therefore
  cannot substitute a key for P: a record that does not follow from the pin
  is `changed`, as before. Continuity checks still run while P is removed, so
  a valid successor chain advances the pin and a break marks it `changed`.
- The status is computed at each use from the persisted pin and the persisted
  manifest, so a restart cannot revive P, and the high-water rule below
  refuses a replay of manifest 7.
- P returns to `observed` only when a later manifest lists it again and its
  pin is not `changed`, and to `verified` through any route in step 1 above.
  `verified` keys are not gated by membership, because their trust comes from
  a fingerprint the operator confirmed.
- A station's own outbound rows to P still open, because their signer is the
  station itself. Their confidentiality label stays the one recorded at seal
  time (D6).

Opportunistic confidentiality. For an `observed` peer, a hub that substitutes
the key on first contact can read what is sealed to the substituted key. Mail
to and from `observed` peers is therefore protected only against a hub that is
honest at first contact and passive afterwards. Every surface labels it
`confidentiality: "opportunistic"` (D6), and the user docs recommend
`BRIGADE_FLEET_MESSAGES_REQUIRE_VERIFIED=1` for anyone who has verified
fingerprints. With it set, `observed` is never used and sends to an
unconfirmed key fail with `recipient_key_unverified`.

Pre-mesh principals migrate explicitly. When a peer first publishes a key, it
becomes `observed` if the membership manifest lists it and `unverified`
otherwise. The peer runs `fleet keys show`, the operator reads the fingerprint
over a channel the hub does not control, and the planned `fleet keys accept
<principal> --fingerprint <fp>` moves the entry to `verified`. A signed key
manifest does the same in bulk.

Acceptance. The planned `fleet keys accept` never accepts what it just
fetched. It requires the expected fingerprint as input, fetches the current
record, verifies `self_sig`, and accepts only on an exact match. Bulk
acceptance takes one of two inputs:

- `--manifest <file>`: the exact key-manifest object below, with a `keys`
  list of `{principal, hub, kid, fingerprint}`, signed under
  `brigade-mesh/v1/manifest` by the operator manifest key.
- `--fingerprints <file>`: principal and fingerprint pairs the operator
  verified out of band. Every entry must match.

There is no `--all-changed`. This refines the operator's decision that a
person accepts rotated keys with one bulk command: the manifest is that
command, and its trust comes from a key the operator controls.

Signed statements and rollback. Four kinds of statement carry a sequence
number `seq` under their signature: hub identity statements (D4), membership
manifests, key manifests and revocation statements. For each kind, and per
signing key, the client persists the highest accepted `seq` and the SHA-256
of that complete signed statement's canonical form. It refuses a statement with a lower `seq`
(`signed_statement_rollback`) and a statement with the same `seq` and a
different digest (`signed_statement_conflict`), and records the conflict as
evidence that the signing key is misused. A replay of the accepted statement
itself is a no-op. Revocations are also cumulative: a later revocation
statement can add revoked kids or move a `compromised_from` earlier, and
cannot remove a revocation or move it later. Each revocation statement lists
every revocation so far, so the latest one is complete, and a statement that
drops an earlier entry or changes its principal is refused. These marks live in the key
directory with the pin store and follow the same file rules.

### Revision 5 signed statement contract

This is the proposed interoperable statement profile for slices 2 and 2b.
It uses the existing Ed25519 signing helper, SHA-256 and D2 canonical encoder.
It changes neither envelope bytes nor any existing domain label. A statement's
kind is selected by its import/upload route and expected verifier, never by a
hub-chosen verification key. Exact objects (all fields required, no others):

| Kind and label suffix | Complete signed object |
|---|---|
| membership, `/membership` | `{v, hub, seq, created_at, members, sig}` |
| key manifest, `/manifest` | `{v, hub, seq, created_at, keys, sig}` where each key is exactly `{principal, hub, kid, fingerprint}` |
| cumulative revocation, `/revocation` | `{v, hub, seq, created_at, revocations, sig}` where each revocation is exactly `{principal, kid, compromised_from}` |
| identity, `/hub-identity` | `{v, hub_id, hub_sig_pub, endpoint, require_sealed, max_envelope_bytes, envelope_versions, suites, backend, capabilities, created_at, seq, sig}` |
| migration, `/hub-migration` | `{v, old_hub_id, new_hub_id, new_hub_sig_pub, new_identity_seq, new_identity_sha256, endpoint, created_at, seq, sig}` |

For every kind, remove only the top-level `sig` and sign
`LABELS[label_name] + canonical(object_without_sig)`, where `label_name` is
exactly `membership`, `manifest`, `revocation`, `hub-identity` or `hub-migration`
from the table. Each label is
`b"brigade-mesh/v1/" + label_name.encode("ascii") + b"\0"`. The NUL is
one byte `0x00`, as in `LABELS`. `sig` decodes to
exactly 64 bytes. The rollback/floor digest is the full 32-byte SHA-256 of
`canonical(complete_object_including_sig)`, never the unsigned object, raw
HTTP text, wrapper or a truncated hash. Display `--sha256` as 64 lowercase hex
characters. Invite floor digests use the same 32 bytes in unpadded base64url.
Manifest kinds verify with the locally pinned manifest public key. Identity
verifies with `hub_sig_pub` after deriving its id. Migration verifies with the
persisted old identity key and must derive `new_hub_id` from its new key.

HTTP/file authentication entry points must parse raw bytes with the bounded,
duplicate-rejecting parser, not accept an already decoded untrusted dict.
`GET /mesh/identity` may return a bare complete identity object or exactly
`{"identity":<complete identity>,"migration":<complete migration>}`. No partial
object, null migration or extra wrapper field is accepted. The wrapper carries
no authority or digest of its own. Bound that response to 34,816 raw bytes,
then validate each inner object at its own bound. The manifests response is
exactly `{"statements":{<kind>:<complete statement>,...}}` with any subset of
`membership`, `manifest`, `revocation`, no duplicate/unknown kind, and at most
8 MiB raw. Response-wrapper depth adds at most two containers to the inner
four-level bound. Verify and digest each statement independently.

Before signature verification or state changes:

- Bound raw input before JSON allocation: membership, key manifest and
  revocation at 2,098,176 bytes each (2 MiB plus 1024), identity and migration
  at 17,408 bytes each (16 KiB plus 1024). Bound canonical complete objects to
  2 MiB or 16 KiB respectively. The three-manifest response, including its
  wrapper, remains within the existing 8 MiB response cap. Refuse nesting
  deeper than four containers, counting the root as one.
- Input is ASCII JSON with no BOM or literal NUL. Reject duplicate keys at
  every level, unknown fields, nulls, floats, NaN, infinities, booleans where
  integers are expected, malformed strings and escaped lone surrogates.
  All decoded strings in these schemas are ASCII. JSON whitespace and key
  order may vary for statements and never affect their canonical digest.
  Unlike statements, frozen `bm1` envelopes still require canonical raw bytes.
- `v` is integer 1. `seq` is an integer from 0 through 9,007,199,254,740,991
  (the JavaScript exact-integer bound). This is intentionally narrower than
  the current hub's SQLite bound of `2**63-1`. Reject, never round, larger
  sequences. Every time uses D2's exact timestamp grammar and valid instants.
  Comparison uses UTC instants, never strings. No timestamp proves freshness.
- Binary fields use D2 unpadded base64url with exact decode/re-encode equality.
  `hub_sig_pub` and `new_hub_sig_pub` are 32 bytes. A hub id is `h_` followed
  by the canonical encoding of its 16-byte derivation. Hub statements never
  use literal `mesh`. A kid is `k1_` plus 16 encoded bytes. Principal ids use
  the crypto core's node `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` or agent
  `agent:[a-z][a-z0-9._-]{0,62}` grammar with ASCII full matching.
- `members` has 0 through 4096 distinct principal ids in ASCII order. `keys`
  has 0 through 4096 entries ordered by `(hub, principal, kid)`, with each
  entry's `hub` equal to the statement's `hub`, one entry per principal and
  no shared kid. Each fingerprint is the exact 13 groups of four uppercase
  base32 characters from the full 32-byte hash, joined with hyphens, checked
  by decode/re-encode equality and against the fetched self-signed key.
- `revocations` has 0 through 8192 entries ordered by `(principal, kid)`,
  with unique kids and no duplicate pair. `compromised_from` is a timestamp,
  not a command alias such as `now`. Every later statement must include each
  previously accepted revocation for this hub/signer with an equal or earlier
  cutoff. An imported cutoff and an unsigned hub cutoff combine by earlier
  instant. Reaching a count/size bound fails closed and requires a reviewed
  profile extension, never truncation or forgetting a revoked kid.
- Identity `require_sealed` is a boolean. `max_envelope_bytes` is an integer
  1 through 98,304. `envelope_versions` is exactly `[1]`, `suites` exactly
  `["bm1"]`, `backend` exactly `full` or `hub-lite`. `capabilities` has 0
  through 32 unique names in ASCII order, each `[a-z][a-z0-9_-]{0,63}`.
  Known names are the seven in D4. Clients ignore unknown names and never
  enable a feature from them. Current hub upload accepts only known names,
  so an extension also needs a reviewed server change. Hub Lite must require
  sealing and cap envelopes at no more than 32,768 bytes.
- Identity and migration `endpoint` must equal the normalized origin of D4.
  Migration requires distinct old/new ids, the stored old signer and the new
  served identity's exact id, key and endpoint. Its `created_at` is the
  identity supersession instant. It has its own `(kind, signer)` sequence
  stream and never resets identity or manifest sequences. `new_identity_seq`
  has the same integer bounds as `seq`. `new_identity_sha256` is the unpadded
  base64url encoding of the complete 32-byte SHA-256 digest of the signed new
  identity object, including `sig`. The served new identity must match both
  fields exactly before migration commits. Install that exact object as the
  new signer's identity floor in the same transaction. Only then may later
  statements advance it. If policy advanced before this client migrated, the
  operator supplies a fresh old-key migration binding the served statement,
  or the client fails closed. A higher new sequence alone is not a substitute
  for the referenced digest.

Acceptance is all-or-nothing, including key-manifest entries. Persist accepted
objects, their complete digests and `(kind, signer)` marks in one transaction.
Here signer is the full SHA-256 of the signing public key.
Across hubs preserve that signer's marks and continue its sequence stream for
that kind. A different signer starts a distinct stream only after explicit
anchor acceptance. Neither changing hub nor changing kind discards old marks.
Hub publication also has one administrative counter per manifest kind,
matching its existing opaque storage. Anchor replacement continues above that
counter, even when the new signer's independent client stream has no mark.
Persist the publication counter across anchor rotation and restart. Never
derive authority from it. Identity server guards compare sequences within the
same identity signer, while a new identity is admitted only by its authenticated
migration or explicit operator acceptance and the bound identity floor.
Every floor binds kind, signer, hub, minimum sequence and complete digest.
A statement at that sequence must have that digest. A higher statement may
satisfy the minimum only under the same authenticated scope and signer.
Existing minimum/high-water marks can increase, never decrease.

Before hub-facing code, check in deterministic fake-key fixtures for all five
kinds: complete object, unsigned canonical bytes, exact labeled signing input,
public key, signature and full canonical digest. Python and the future Worker
must verify the same bytes, including a boundary sequence and empty lists.
One malformed corpus covers duplicate/nested keys, Unicode, limits, order,
range, wrong label, unsigned digest and same-sequence conflicts. These are
known answers for encoding/signatures, separate from the frozen HPKE vectors.

What the high-water rule cannot detect. It only compares a statement with
what this station accepted before. A fresh station, or one whose marks were
lost, has no mark, so a hub can serve it an old but validly signed first copy,
for example membership manifest 7 after the operator signed manifest 8 to
remove P, and the station accepts 7. A hub can also withhold a newer
statement from any station, which leaves that station on the last version it
accepted (D8). Neither case lets the hub exceed what the operator once signed,
but both can keep a removed member `observed`. The optional mitigation is a
trusted floor: the operator gives a station a minimum `seq` and the SHA-256 of
that complete signed statement's canonical form, either out of band with a planned
`fleet keys membership require --seq <n> --sha256 <digest>` or inside an
invite (D9). The station refuses any membership manifest below the floor,
refuses one at the floor whose digest differs, and until it accepts a manifest
at or above the floor it treats every unconfirmed key as `unverified`, as if
it had no manifest. A floor shortens what a withheld update can hide to the
time since the floor was issued. Nothing short of a fresh out-of-band check
detects a withheld update after that.

Trust provenance and use refusal are separate state. Keep the accepted record,
its original `observed`/`verified` provenance and acceptance evidence even when
use is blocked. Check irreversible kid revocation first, then unresolved
`changed` or `pending_binding`, then current-hub membership for observed keys,
then require-verified. Relisting a member clears only `observed_not_member`.
Membership cannot erase `changed`. Explicit operator acceptance of an exact,
independently checked fingerprint may replace a nonrevoked changed head after
validating the replacement's self-signature. Preserve the old records,
revocations and acceptance evidence. Fingerprint acceptance cannot revive a
revoked kid or complete `pending_binding`. A replacement for a revoked kid
needs a fresh nonrevoked key and explicit verification. Require-verified is checked at
every seal, open, reply link and spool transmission, including existing pins.
It blocks observed use without erasing the pin or its history.

The local boundary, stated exactly. Fleet MCP exposes no operator acceptance,
anchor replacement or manual identity-acceptance tool. Normal MCP messaging
may persist validated automatic first observation, continuity, statement marks
or authenticated identity migration through the same verifier as the CLI.
These automatic writes cannot substitute for operator confirmation. An agent with shell access running as the same OS
user is not stopped. It can edit the pin store, copy the private keys, or run
the CLI with a fingerprint it read from the hub's own `who` output. Requiring
an out-of-band fingerprint stops accidental and naive acceptance, not a
hostile agent with the operator's shell. A real boundary needs OS separation,
such as a dedicated OS user that owns the key and pin files behind a narrow
broker, or an OS keychain that asks for user presence. This plan leaves OS
separation as a future option.

Rotation and continuity. The planned `fleet keys rotate` publishes a new
record signed by the old key and keeps the old private key, marked retired, so
mail sealed to it still opens. A client accepts a successor without the
operator when every link from the pinned key to the new record has a valid
`prev_sig` by its predecessor, `created_at` strictly increases along the
chain, no link is revoked, and the chain has at most eight successors.
`keys_get` fetches the intermediate records. The local peer API takes
successors only. Nine authenticated successors yield `changed`. Malformed
input, a tuple longer than nine, or a fresh chain starting with the pinned
head is refused without recording changed evidence. A gap or revoked link
yields `changed`. Every successor MUST have a different kid from its
predecessor. Slice 1's `verify_key_record` checks one adjacent link, including
strict timestamp increase and predecessor identity. Slice 2's pin-store chain
walker MUST enforce the eight-link limit, reject gaps and revoked links, and
start at the current pinned head so an old link cannot roll back the pin. A
ninth link requires operator verification. The successor keeps the pinned
trust level.

Key history. The client keeps every accepted public record with a validity
interval: from its signed `created_at` to its successor's signed `created_at`,
or to `compromised_from` if it was revoked. Archived mail verifies against the
record named by `(hub, principal, kid)` when the envelope's signed
`created_at` falls inside that interval, allowing `MAX_FUTURE_SKEW` for the
retirement bound only. The lookup returns the immutable record plus provenance,
revocation and validity evidence. Intervals start inclusively and end
exclusively at the successor time. Revoked history uses only the separate
receipt rule below, never skew to extend the compromise cutoff. Hub timestamps
(`received_at`, `created_at` columns) never enter these checks.

Revocation. Revoking a node (`revoke_node`) or a principal
(`revoke_principal`) marks all its key records revoked in the same
transaction and records `compromised_from`. The operator states that time with
a planned `--compromised-from <time|key-creation|now>` option, defaulting to
`key-creation`, the conservative choice. Keep that compromise default. Before
sealed-mail rollout add a distinct authenticated routine credential-rotation
operation that atomically replaces only the bearer-token digest/generation,
invalidates the old token and leaves principal keys, grants, pins and signed
history intact. It is not revoke-then-reenroll and cannot revive a previously
revoked key or principal. A revoked kid can never be published
again, and continuity from a revoked key is never accepted. A hub's revocation
can only reduce trust, so clients honor it without a signature: no new mail is
sealed to the key. A revocation statement signed with the manifest key carries
the same information to no-hub contacts and to stations that do not trust the
hub to report it. When a hub revocation and a signed statement disagree, the
client uses the earlier `compromised_from`.

Policy A, approved on 2026-10-27, retains principal names as attributed claims.
An otherwise valid complete signed list is accepted when an older signer or
an authenticated unsigned hub allegation names a different principal for a
kid. Each `(hub, kid)` keeps the earliest cutoff and every principal's source
attribution. Unsigned allegations keep a separate minimum per principal.
A typed `kid_binding_conflict` records the disagreement in the same atomic
generation as the accepted list, marks and barriers. Evidence exhaustion
refuses the whole transaction without eviction. Within one signer's stream,
omitting an earlier kid, changing its principal or moving its cutoff later
still refuses the whole list. Replacing the signer starts an independent
baseline, and reactivating a signer resumes its earlier baseline.

Binding disagreements use ordinary storage, with up to 8192 groups bounded by
the retained barriers. A separate table holds up to 4096 authenticated signer
or floor conflicts. The operator approved up to 12,288 evidence records across
the two tables, with the existing 56 MiB ordinary and 64 MiB total limits.
Ordinary accounting includes binding groups and their source claims.
Authenticated evidence can use the remaining total capacity. A new floor
still needs ordinary storage and table headroom. Capacity refusal preserves
the complete previous generation without dropping claims or evidence.
Floor, barrier and unsigned-claim table limits are shared across hubs. Filling
one table for one hub can prevent another hub's admission to that table.

The original trust-foundation slice stored manifest anchors, membership,
revocations, rollback floors and conflict evidence in one POSIX snapshot.
It stopped at evidence storage. The current peer acceptance library below
adds bounded local acceptance and use decisions. Peer-use checks consult the
`(hub, kid)` revocation barrier without filtering by principal. Bulk
key-manifest acceptance, identity migration, CLI and message integration
remain unfinished.

Which mail from a revoked key stays readable. A thief can sign any
`created_at`, and the order in which a station learns things proves nothing.
Take a key compromised at 10:00, a forged message signed with `created_at`
09:59 and delivered at 10:10, and a station that learns of the revocation at
10:20. The forgery arrived before the station knew, but it arrived after the
compromise, so it must be rejected. The rule therefore uses only evidence the
station recorded about the envelope itself:

- Every station keeps an append-only receipt log, an owner-only SQLite table
  in the key directory. When the station first validates a complete sealed
  envelope for storage/opening (the hub inbox, `sent`, live delivery or the
  no-hub inbox), under D6's signature/content/context/freshness checks it
  appends the full-envelope SHA-256 digest and a verified signed-header digest
  (D6), `(hub, from, from_kid, client_id)`,
  the original hub message id and the local receive time, before the opened
  row is returned. Rows are never updated. Triggers refuse `UPDATE` and `DELETE`, and pruning removes
  only rows older than the 90 day freshness window whose kid is not revoked.
- The clock policy: the receive time is the station's own UTC wall clock at
  the moment of receipt, never a hub timestamp or the envelope's
  `created_at`. The operator's `compromised_from` comes from another clock.
  The assumption, shared with D6's freshness check, is that every clock is
  within `MAX_FUTURE_SKEW` (5 minutes) of true time, so two clocks can differ
  by `MAX_RELATIVE_SKEW`, 10 minutes. Both timestamps are recorded to whole
  seconds, so `TIMESTAMP_PRECISION` is 1 second. A receipt counts only when
  `local_received_at + MAX_RELATIVE_SKEW + TIMESTAMP_PRECISION <
  compromised_from`, a strict inequality. For `compromised_from` 10:00:00, a
  receipt at 09:49:58 qualifies, and receipts at 09:49:59 and 09:50:00 do not.
- Outside that assumption the station claims no proven pre-compromise
  receipt. Each receipt row stores the station's last measured clock offset
  and when it was measured. A receipt recorded while that offset exceeded
  `MAX_FUTURE_SKEW`, or with no measurement in the previous 24 hours, never
  qualifies. The offset comes only from a source independent of the hub:
  the operating system's time synchronization status (a synchronized flag
  plus its reported offset or error bound, for example from systemd-timesyncd
  or chrony on Linux, the Windows Time service, or macOS timed). The hub's
  `Date` header never qualifies a receipt. A hub could otherwise answer a
  station whose clock is slow with a `Date` that matches the wrong clock, so
  the offset check would pass and a message received at true 10:10 could be
  recorded as 09:40 and accepted against a 10:00 `compromised_from`. Where
  the platform reports no synchronization status, or reports it as
  unsynchronized, there is no measurement and revoked-key history is
  unavailable, in hub and no-hub mode alike. `brigade doctor` still compares
  the hub `Date` header with the local clock and warns on a visible
  difference, as a diagnostic only.
- Other checks keep `MAX_FUTURE_SKEW` as their tolerance even though they
  also compare two clocks (an answer's signed `created_at` against the
  asker's deadline, and an envelope against a superseded hub identity). In
  those checks a smaller tolerance refuses more mail, so 5 minutes errs
  toward refusal. The receipt rule grants readability, so it takes the full
  relative bound.
- A message signed by a revoked key opens as history only when the receipt
  log holds that exact full-envelope digest with a qualifying receive time. A tombstone
  may inherit only the matching earlier receipt by the D6 transition rule.
  Everything else from that key opens as `sender_key_revoked`: mail received
  after the cutoff, mail with no receipt (a fresh station, a pruned log, or a
  station that never saw it), and mail whose digest differs from the receipt.
- History is not authority. A readable message from a revoked key opens with
  `key.status: "revoked"` and `history: "received_before_compromise"`, which
  holds under the clock assumption above, stays
  fenced, carries no `sender_label`, never authenticates an operator answer
  (D7), never completes or links a pending request (D5), and is never treated
  as approval by the Dot pack or any other consumer.

Under the default `--compromised-from key-creation`, nothing signed by the key
stays readable. An operator who knows a later compromise time sets it to keep
older history. The same-OS-user limit applies: an agent with the operator's
shell can write a false receipt, which only OS separation stops.

Downgrade and legacy mail. Once the client holds a key entry for a principal
at any status, including `unverified`, it never sends that principal plain
text, and a missing key fails with
`recipient_key_missing`. A row that carries an `envelope` never uses
hub-supplied `subject`, `body`, `ref`, `thread` or `sender_label`. Existing
plain-text rows stay readable, labeled `authenticity: "unauthenticated-legacy"`,
fenced, and never confer sender authority. A plain-text row from a pinned
sender that this client has no local record of from before the pin gets the
stricter label `unauthenticated-after-pin`, because the hub's timestamp is no
proof that the row predates pinning. In `auto` mode a hub can still push plain
text toward a principal that was never pinned by withholding its key.
`always` mode or a `require_sealed` hub closes that.

Losing a key loses only the mail sealed to it. The routing row stays and the
owner can still ack it.

### Current peer acceptance library

The internal POSIX peer APIs now support fingerprint acceptance, bounded continuity, immutable history and locked local key selection for the configured origin's persisted current Hub identity. Explicit initialization extends the trust snapshot from v1 to v2. Manual acceptance requires an independently supplied full fingerprint, the expected head digest and an explicit require-verified setting. CLI/MCP key commands, bulk manifests, identity migration and recovery, inbox opening and send/reply sealing remain unfinished.

Current use rechecks the retained continuity proof through the nearest independent fingerprint confirmation. A revoked dependency blocks use as `changed` while preserving the queried key's original provenance and history. Membership observation does not reset that dependency. Independent confirmation of a nonrevoked current successor can restore its proof boundary. The queried kid's own revocation always wins.

New key admissions and new manual confirmations reject creation dates more than five minutes beyond the local wall clock with `mesh_peer_created_in_future`. A bounded batch uses one cutoff. Stored validation, completed retries and resolution using an already independently confirmed current key remain independent of that clock. Stations need synchronized clocks. An allowed near-future substitute may still require waiting within the skew and having the real principal rotate to a later signed date. This guard does not repair previously admitted future-headed snapshots or arbitrary OS-clock errors.

Peer commits have additional admission limits: 8 MiB across canonical peer tables, at least 8 MiB free under both the ordinary and total byte ceilings after a commit, and eight retained changed candidates per peer. Resolved candidates retain their slots. These limits leave the existing 56/64 MiB evidence ceilings and signed-evidence admission rules intact. Exact retained retries reflush without new admission. Finite headroom cannot guarantee every later evidence import.

When changed evidence cannot be recorded, the top-level disposition and error still refuse that attempt. A future-dated candidate raises the fixed admission error without recording a changed claim. A later inspection can describe the prior persisted pin as eligible. Callers must honor the refusal and re-enter the locked use scope for actual local cryptography. After eight retained candidates, later distinct changes may remain unrecorded even after operator resolution. There is no automatic eviction or pruning.

## D4. Hub identity

Current foundation status: the offline POSIX identity store persists signed
identity history, endpoint bindings, signer-global marks, identity floors and
conflict evidence. First observation is `observed`. Caller-confirmed operator
or invite provenance is separate from policy freshness, which remains
`unproven`. Replacement and migration are refused. The cross-store journal
and live client integration remain unimplemented, so this
library does not complete slice 2b or activate message encryption.

The pin store is per hub, so whoever controls the hub id controls which pins
apply. Round 1 let a hub reset that namespace by serving a new random id. The
hub identity is now a key the hub process does not hold.

- The admin generates an Ed25519 hub identity key on the admin machine with a
  planned `fleet hub-identity init`. `hub_id` is `h_` plus the first 16 bytes
  of `sha256(b"brigade-mesh/v1/hub-id\0" + hub_sig_pub)`, so the id commits to
  the key.
- The admin signs an identity statement under `brigade-mesh/v1/hub-identity`:
  `v`, `hub_id`, `hub_sig_pub`, `endpoint` (URL origin), `require_sealed`,
  `max_envelope_bytes`, `envelope_versions`, `suites`, `backend`,
  `capabilities`, `created_at` and a sequence number `seq`, which follows the
  high-water rule of D3. It uploads the statement with the admin token. The hub
  stores it in `hub_meta` and serves it at `GET /mesh/identity`. The hub
  process checks that `hub_id` matches the key with `hashlib`, and does no
  signature work.
- Backend identification. `backend` is `full` or `hub-lite`, and any other
  value makes the statement malformed. `capabilities` is a sorted list of
  distinct names, with these known meanings: `messages`, `invites`, `presence`,
  `questions`, `claims`, `events` and `runs`. D3 bounds names and future unknown names. A full hub lists all seven, and
  Hub Lite in version 1 lists `messages`, `invites` and `presence`. A client
  ignores a name it does not know, so a later capability never switches on a
  code path in an older client. A tool whose capability is not listed returns
  `hub_feature_unavailable` without a request (D11), and the client applies
  the Hub Lite throttles (D12) when `backend` is `hub-lite`. Both fields sit
  under the signature and the `seq` high-water mark, so a hub cannot change
  them or roll them back without the admin key. A statement that lists fewer
  capabilities than the backend has only loses features, and a listed
  capability whose route answers 404 still maps to `hub_feature_unavailable`.
- The client keeps a hub identity store, a separate file from the pin store,
  mapping each configured endpoint origin to its current `hub_id`, key,
  status (`verified` or `observed`), acceptance time and the lineage below.
  Pins are keyed by the current `hub_id`.
- First contact. An invite token carries the hub id, so a joiner verifies the
  identity statement's signature and the hub id against the token and records
  the identity as `verified` (D9). An existing machine records it as `observed`
  until the operator runs a planned `fleet hub-identity accept --hub-id <id>`
  with the id read from `fleet hub-identity show` on the admin machine.
- A change fails closed. If a known endpoint serves a different `hub_id` or
  key, every message operation against it stops with `hub_identity_changed`,
  and no new pin namespace is created. The identity moves forward only by:
  - an authenticated migration: a statement under
    `brigade-mesh/v1/hub-migration` naming the old and new hub ids, the new
    key, the endpoint and a time, signed by the old hub identity key and served
    next to the new identity. The client verifies it with the stored old key
    and carries its pins to the new id, or
  - operator verification of the new hub id obtained out of band, with
    `fleet hub-identity accept --hub-id <new id>`, which also carries the pins.
  
  After either, every principal generates fresh signing and KEM keypairs,
  a different kid and a new-hub record with `prev_sig` by its pinned predecessor.
  Rebinding the same public keys reuses the kid, which both the frozen crypto
  continuity check and directory reject. Only the authenticated lineage may
  authorize this cross-hub link. Until a peer publishes its valid successor,
  new sends to it fail `recipient_key_missing` and stay sealed-pending, never
  plaintext. Old-hub records remain available for original-attempt recovery.
- Lineage. The store keeps, per endpoint, the ordered list of every hub
  identity it has accepted: `hub_id`, key, how it was accepted (`invite`,
  `operator`, or `migration` with the digest of the signed migration
  statement), `accepted_at` and `superseded_at`. Each link after the first is
  authenticated by a migration signed with the previous key or by an operator
  acceptance, so a hub cannot add an entry by serving one. New sends, key
  publications and replies use only the current identity. Opening (D6 step 2)
  accepts an envelope whose signed `hub` is any identity in the endpoint's
  lineage, verifies it against the key history recorded under that hub id,
  and refuses an envelope signed for a superseded identity with a signed
  `created_at` later than that identity's `superseded_at` plus
  `MAX_FUTURE_SKEW` (`hub_identity_superseded`). Old mail therefore stays
  readable after a migration, and a hub cannot keep old identities alive for
  new mail.
- Namespaces stay distinct across a migration. The seen-id cache, the receipt
  log (D3) and the pending request store (D5) are keyed by the full signed
  triple `(hub, from, client_id)`, so an envelope under the old id and one
  under the new id never collide, and a replay check under one id says
  nothing about the other. A reply made after the migration links to an older
  request because its signed `in_reply_to` names the original's hub (D2).
- A new endpoint for the same identity (the hub moved) is accepted when an
  independently configured new origin serves a higher-sequence statement
  signed by the locally stored key naming exactly that origin. Redirects alone
  never change configuration, move credentials or create a new trust entry.

### Origin, bootstrap and migration persistence

The signed origin is computed by one algorithm in CLI, client and Worker.
Require an absolute HTTP(S) URL with ASCII host and scheme, no userinfo,
query, fragment, percent escapes, backslash, controls or whitespace. Reject
paths other than absent or `/`. Lowercase scheme and DNS host, reject a
trailing DNS dot, use ASCII DNS labels (including already encoded `xn--`
labels), and do no implicit Unicode/IDNA conversion. DNS labels have 1 through
63 characters, start/end with an ASCII letter or digit, and otherwise contain
only letters, digits or hyphens, with at most 253 host characters. If the
final label is all decimal digits or matches `0[xX][0-9a-fA-F]*` (including
bare `0x`), require the entire host to be the strict dotted-decimal IPv4 form
below. Reject candidates such as `example.123` and `example.0x` that do not
meet that form. A hexadecimal label with a normal DNS suffix, such as
`0x1.example`, remains a DNS name. Bound the normalized
origin to 2048 ASCII bytes. IPv4 uses four decimal
octets without leading zeros. Bracketed IPv6 has no zone id and renders its
compressed lowercase address. Ports are decimal 1 through 65535, no leading
zeros. Remove default 443 for HTTPS and 80 for HTTP. Render no trailing slash.
The signed `endpoint` must already be this normalized string. Configured URLs
may normalize by these rules, but a valid signature for another origin fails
`hub_endpoint_mismatch` before any pin or anchor write. Do not follow a
cross-origin redirect with credentials or treat it as identity continuity.
HTTP is permitted for explicit local/test configurations and supplies no TLS
protection for bearer tokens. Production onboarding uses HTTPS.

Local custody and file-only statement validation may precede identity support.
Every hub-fetching key/statement command takes its namespace from a persisted
current endpoint identity, never the response being fetched. Without that
entry, fail `hub_identity_unverified`. This name means missing accepted
identity state. It does not silently upgrade the existing `observed` identity
path to mandatory out-of-band verification. First-contact identity acquisition
is the dedicated bootstrap exception: verify self-signature, derived id and
origin, persist it as `observed`, then permit dependent operations subject to
local sealing/key policy. Invite or operator confirmation still gives
`verified` identity provenance as D4 previously specified.

Identity provenance and policy freshness are different. Verifying a key/id
alone does not prove the identity statement is the current policy. Bootstrap
that claims current-policy verification must also receive independently trusted
identity and applicable membership floors: `(kind, signer, hub, seq, digest)`.
The planned identity accept command gains `--seq <n> --sha256 <digest>` for
that claim. An invite's existing optional `m` field binds membership only.
Without a separately delivered identity floor it verifies the hub id, not the
latest sealing policy. Persist these distinctions and show `policy_freshness:
"unproven"` until floors are satisfied. Missing floors need not forbid the
existing observed or invite-verified identity semantics, but cannot be hidden
by that label. Local `seal=always` and require-verified avoid relying on old
optional-sealing/membership policy. Even floors cannot detect a later withheld
update or prevent a signed fork above the floor without external comparison.

Slice 2b depends on slice 2's trust store. One observable crash-safe commit
advances the endpoint identity and supersession instant while preserving:
immutable original-hub records and validity intervals, pin provenance and
barriers, cumulative revocations, all `(kind, signer)` high-water marks,
floors, membership provenance and authenticated lineage. Use a transactional
local journal or database with a commit marker and recovery before any reader
uses current state. Readers see either the complete old generation or the
complete new generation, never a new namespace missing old refusal state.
Separate identity/pin files remain logical stores and cannot be independently
committed without that journal. A restart finishes or rolls back an incomplete
migration before any network use. No reader may select mixed generations.

Carry constraints forward, not signed bytes rewritten as new statements.
Keep old manifests/floors under the original hub. Current-hub observed use
requires a newly signed membership with a sequence above the preserved mark.
If there is an old-hub membership floor, also require an independently trusted
current-hub floor or explicit operator acceptance mapping that old floor to
the new signed digest. Until these checks pass observed use is blocked. With
no old floor, a valid higher-sequence current-hub manifest suffices, preserving
the optional-floor semantics and their unproven-freshness limitation. Old membership cannot authorize the new hub. Revoked kids remain
revoked across namespaces, and pending bindings and changed pins remain
blocked. An operator-accepted identity change does not waive these barriers.
The lookup exposed to the opener is `record_for(hub, principal, kid,
signed_created_at)`, returning historical validity and current refusal evidence.
Never rewrite receipt, seen-id, outbox or pending-request triples.

Server-side identity replacement must clear an unrelated stored migration in
the same transaction. Same-identity policy updates may retain a proof only if
its new id/key/endpoint still match. A client always validates that pairing
and rejects unrelated proofs, regardless of server cleanup. Enforce identity
sequence rollback/conflict checks as defense in depth. The staged policy
proposal in D8 must preserve these rules.

## D5. Sealing on send, reply, retry and the spool

`send()` and `reply()` decide whether to seal from the hub's signed identity
statement, the local policy `BRIGADE_FLEET_MESSAGES_SEAL=auto|always|never`
(default `auto`), and the recipient's key status.

| Hub policy | Local policy | Recipient key | Result |
|---|---|---|---|
| any | `never` | not pinned | plain text, refused by a `require_sealed` hub |
| any | `never` | pinned | refused, a pinned principal never gets plain text |
| plain allowed | `auto` | none, not pinned | plain text |
| any | `auto` or `always` | `verified` or `observed` | sealed, unless require-verified is set and the key is `observed` (`recipient_key_unverified`) |
| any | any | `unverified` | `recipient_key_unverified` |
| any | any | `observed_not_member` | `recipient_key_not_member` |
| any | any | `pending_binding` | `invite_binding_pending` |
| any | any | `changed` | `recipient_key_changed` |
| any | `always` | none | `recipient_key_missing` |
| `require_sealed` | `auto` | none | `recipient_key_missing` |
| any | any, extra missing, sealing needed | n/a | `mesh_extra_missing` |

Spool. For sealed rows `payload_hash` is SHA-256 over the stored envelope
bytes, never over plaintext, so the hub cannot confirm guesses about short
bodies. The client seals once, writes the sealed form into the spool record
under the spool lock, and only then transmits, so spool retries send identical
bytes. A send made offline without the recipient's key is spooled as
plaintext with `seal: "pending"` in the owner-only spool file and sealed at
drain time, again persisting the sealed form first. A `recipient_key_stale` or
`sender_key_stale` (409) means the hub stored nothing, so the client reseals
with a fresh content key and replaces the record. `_drain_locked` drops 4xx
codes it does not recognize today, so both codes join its retained set.
Drain rechecks the recipient key's status before each transmission, so a
record sealed while the recipient was usable and drained after it became
`observed_not_member`, `changed` or revoked fails with that status's code and
is not sent. A policy change to require-verified blocks an existing observed
spool entry at transmission time even if its envelope was already persisted.

Retry commitment. `rc` is `HMAC-SHA256(retry_key, b"brigade-mesh/v1/retry\0"
+ canonical(semantic fields))`, where the semantic fields are `hub`, `from`,
`to`, `kind`, `priority`, `client_id`, `deadline`, `in_reply_to` and the five
payload fields. All five payload fields are present in the commitment object,
including JSON `null` for `None`. Absent `deadline` and `in_reply_to`
are omitted, as in the frozen retry helper, never replaced by `null`. An empty `ref` contributes `""`, so it MUST
produce a different commitment from `None`. It leaves out everything that changes between seals of the
same request: the content key, the wraps, `created_at`, padding and the
signature. The hub has no retry key, so `rc` lets it test no guesses, and
because it includes `client_id` and `to`, equal values reveal nothing the
unique index does not already reveal.

Retry recovery. A new seal of the same request produces different bytes, so a
resend after a crash (patrol and the Dot pack derive `client_id` from their
own state) or a #2010 tool retry with the same `client_id` meets a conflict.
The contract:

1. Persist an attempt before its first network write: original `(hub, from,
   client_id)`, envelope bytes/digest, `rc`, original deadline, semantic-input
   commitment, retry-key identifier and local expiry. Keep prior attempts and
   their outcomes across migration. The new current identity never overwrites
   an outstanding attempt's `hub`, `rc` or reply triple.
2. Recovery precedes any new seal/attempt. Sender-scoped `sent_lookup` with
   explicit original `hub` and `client_id` returns the stored envelope,
   tombstone or retry receipt (D8). The authenticated 409 includes the existing
   id. First validate canonical bytes, signed header, own signer history,
   routing triple and accepted identity lineage, including the supersession
   time. Then recompute `rc` using the recorded attempt's original `hub`,
   deadline and caller semantics, not today's identity. Do not copy unverified
   header fields into the commitment calculation. A mismatching triple fails
   before commitment comparison.
3. Equal verified `rc` returns the existing id as delivered, including after
   ciphertext purge or visible routing deletion when a valid retry receipt
   remains. Different `rc` is permanent `message_client_id_conflict`. If retry
   key material is lost, opening the sender wrap and comparing all original
   semantic fields may recover a full envelope. It cannot recover a tombstone
   or receipt without that key. A plaintext existing row, missing historical
   key, or unverifiable content yields permanent conflict with existing id and
   `reason: "content_unverifiable"`, never delivered and never a new send.
4. A lookup `not_found` is not evidence that an in-flight request cannot commit
   later. Keep one attempt owner under the local spool lock and reconcile
   response loss before replacing it. An explicit stale-key refusal means
   that request stored nothing, but first reconcile any earlier uncertain
   request for the same attempt. Resealing uses a fresh content key and the
   same attempt hub/semantics only while that hub is still current.
5. Across migration, recover A before creating a B attempt. A new namespace
   alone does not make the old request absent. The Hub prerequisite is a
   transactionally ordered identity fence: activation serializes with sealed
   inserts, permits duplicate/receipt lookup for A, and rejects every new A
   insert after activation. A lookup for A after that fence can establish
   absence on an honest hub, including a request completing just before the
   fence. Without this capability or a terminal outcome for all old requests,
   retain the attempt as unresolved and send nothing under B. The fence must
   cover every writer of the original storage. Moving to an independent hub
   database does not provide that fence automatically and cannot prove old
   absence while the old writer may still commit. A malicious hub
   can withhold a committed row, so this provides retry correctness for an
   honest backend. Hub omission remains undetectable. Do not silently abandon an
   unresolved attempt and risk delivering twice.
6. Only after recovery/fenced absence may a new B attempt be persisted. Its
   `hub` and `rc` are new values. Record the relationship between attempts
   locally, while all old receipt/seen/pending triples remain unchanged. On
   expiry of local retry state, fail `message_retry_expired` without resealing
   or transmitting. Caller reuse of an expired request key is not permission
   to create a fresh delivery.

The Hub must scope sealed uniqueness to `(hub, sender, client_id)` and expose
exact scoped lookup before slice 6 uses this recovery. Current source does
neither. D8 proposes the compatibility and receipt storage contract. Duplicate
lookup runs before new-attempt freshness, current key-id and current-hub admission checks, so a stored
A attempt can recover after rotation/migration, but new A inserts cannot.

Publication has an analogous response-loss boundary. Persist a key candidate
before `keys_publish`. On timeout or `principal_key_exists`, fetch its kid,
verify its complete canonical record and signatures against the persisted
candidate, and confirm its state/current active head. An identical active
record reconciles success. A retired identical record proves publication but
requires continuity reconciliation before use. Revoked, substituted or
unrelated active records never count as usable success. Do not generate a new
key just because a response was lost. Statement/identity uploads similarly
retain the candidate complete digest and sequence, then read back and verify
the exact object after timeout or sequence conflict. A higher/different object
needs explicit reconciliation before reporting successful upload. Restart
resumes the persisted publication attempt, without re-signing new bytes.

Requests whose semantic fields depend on the clock. A question's signed
`deadline` is `created_at` plus the timeout, and `deadline` is a semantic
field of `rc`. If a retry recomputed it, a question whose response was lost
would reseal with a later deadline, a different `rc` and a conflict. So,
before the first transmission, the client persists an idempotency record in
the pending request store, keyed by the caller's request key (for
`fleet_ask_user`, #1951's `(caller, generation, request_id)` under the
original attempt hub id): the `client_id`, the original `deadline`, and
`HMAC-SHA256(retry_key, b"brigade-mesh/v1/ask\0" + canonical(inputs))` over
the caller's semantic inputs (recipient, question text, timeout). The record
holds no plaintext. A later call with the same request key, including after a
restart, recomputes the input commitment. Equal means a retry: the client first
recovers the original attempt, reusing
its hub, `client_id` and deadline. Only after confirmed absence may it reseal
with a fresh content key under D5's attempt rules, so migration never changes
the commitment used to reconcile the original question. Different
means a new request under an old key, and the client fails with
`question_request_conflict` without contacting the hub. A retry after the
original deadline still reuses it, and returns the question as timed out.
The record is kept for the 90 day retry window. After that window a retry
fails closed, even if longer routing retention still exposes the old row.

Pending requests and replies. Every sealed send is recorded in a local pending
request store (owner-only SQLite under the key directory) keyed by `(hub,
from, client_id)` with `to`, `kind`, `deadline` and the hub message id, and
kept until it is answered or past its window. The same database keeps an
outbox record for every sealed send for the 90 day freshness window, whether
or not the request is answered: the triple, the envelope digest, the recipient kid the
envelope was sealed to, and that key's trust status at seal time. D6 takes
the confidentiality of sent mail from it. The client treats a message as a
reply to a request only when:

- its signed `in_reply_to` equals the local record's `(hub, from,
  client_id)`, and the reply's own `hub` is the current identity or an
  identity in the endpoint's lineage (D4),
- its `from` equals the record's `to`,
- for a request with a deadline, its signed `created_at` is no later than the
  deadline plus `MAX_FUTURE_SKEW`, and
- its sender key is not revoked. Revoked-key history (D3) never links or
  completes a request.

The hub's numeric `reply_to` is a routing hint only. Presence must match both
ways: a row with `reply_to` and no signed `in_reply_to`, or the reverse, opens
as `reply_mismatch`. The hub enforces the same rule at send time.
`dot/hub_inbox.py:424` changes to match an opened row's verified
`in_reply_to` against the Dot pack's own record of the `client_id` it sent for
`msg_id`. For plain-text rows it keeps the numeric match, labeled
`unauthenticated-legacy`, which cannot approve anything.

Live delivery uses the pending store read-only and makes no extra hub call. A
reply that matches a local record shows `reply: "linked"`. A reply with no
local record (a fresh install, or a request sent from another station) shows
`reply: "unlinked"` as an ordinary message. Live delivery never completes a
pending request.

Reply construction. The hub cannot build `Re: <subject>` from ciphertext. For
a sealed original the client opens it, builds the reply subject and thread
locally, and sends a sealed message with signed `in_reply_to` and the hub's
`reply_to` set. A reply written offline to an original this machine has not
opened is spooled as `seal: "pending"` with the original's hub message id and
sealed at drain time after opening the original.

A hub must refuse plaintext replies to sealed or `content_expired` parents
with `message_sealing_required` even when global plaintext is optional. Apply
this to both `reply` and a plaintext `send` with `reply_to`, before inserting
or reflecting reply content. A missing parent is not evidence it was plaintext
and never permits a new fallback reply. Keep the intentionally retained
`sealed_reply_to` routing hint after parent expiry. Signed `in_reply_to` stays
the authority for linking, and expiry must not rewrite that triple.

Sender label. The hub refuses `sender_label` from `agent:` senders today. Once
the label is sealed the hub cannot see it, so the opening client drops it for
`agent:` senders and for any sender whose key is not `verified`.

## D6. Opening mail: verify, decrypt, then fence

Opening happens in one place, `open_rows(rows, context)`, after the page is
validated. The CLI, the MCP station, the Dot pack and live delivery all get
opened rows, and `_frame()` runs last. `list_messages()` takes an
`open_limit` so the delivery hook opens only the rows it will show.

Contexts. Every call passes an explicit context, so a station's own sent mail
never meets the recipient check and an inbox row never meets the sender
check:

| Context | Caller must equal | Signature verified with | Wrap opened | Confidentiality from | Replay check | Reply linking |
|---|---|---|---|---|---|---|
| `inbound` | `to` | the sender's key from key history and pins | the caller's recipient wrap | the sender's key, the only other key the envelope is sealed to | seen-id cache, receipt log | yes, through the pending store |
| `outbound` | `from` | the caller's own key history | the caller's sender wrap | the recipient kid and status in the local outbox record (D5) | own record: the triple must match the local outbox or pending store when one exists | no |

| Call site | Context |
|---|---|
| `send()` return row | `outbound` |
| `reply()` return row | `outbound` |
| `reply()` opening the original before building the reply | `inbound` |
| `sent_lookup` (retry recovery, D5) | `outbound` |
| `sent()` (`fleet_sent`, the CLI sent list) | `outbound` |
| `list_messages()` (`fleet_inbox`, the CLI inbox, the Dot pack inbox) | `inbound` |
| `ack()` return row | `inbound` |
| live delivery | `inbound`, with `open_limit` and local stores only |
| operator answers (D7) | `inbound` |

A context is a required argument with no default. Passing a row to the wrong
context fails with `open_context_mismatch`, so the error names the real
mistake instead of a misleading key or recipient failure.

For each sealed row, in order:

1. Pass the original raw envelope bytes through D2's `parse_envelope`,
   including size, depth, duplicate-key, schema and canonical-byte checks.
   Reject an unknown `v` or `suite` before opening or hashing.
2. Check that the hub's `sender` and `recipient` columns equal `from` and
   `to`, that the caller is the party the context requires, and that `hub`
   is the endpoint's current identity or one in its lineage, subject to the
   `superseded_at` rule (D4).
3. Find the signer's record by `from_kid` in key history under that `hub`,
   the pin store, `who` or `keys_get`, check its trust status and validity
   interval (D3), verify `sig` over the signed header, and check `ct_sha256`
   and `wraps_sha256` against the stored values. `unverified` opens as
   `sender_key_unverified`, `pending_binding` as `invite_binding_pending`,
   `changed` as `sender_key_changed`, `observed_not_member` as
   `sender_key_not_member`, and a revoked key as `sender_key_revoked` unless
   the receipt rule of D3 makes it history.
4. Find the caller's wrap by kid, open it with HPKE, decrypt `ct`, strip
   padding, and parse the payload with the existing field limits.
5. Freshness, receipts and replay:
   - Sealed freshness is fixed at 90 days, independently of the configurable
     1 through 3650 day routing retention (D8). An envelope whose signed `created_at` is older opens as `stale`.
     One dated more than five minutes past the local clock
     (`MAX_FUTURE_SKEW`) opens as `stale_future`. Stations are expected to run
     a synchronized clock. `brigade doctor` gains a check that warns on a
     visible offset.
   - After full signature, content-digest, context and freshness validation,
     the receipt log (D3) records the full-envelope digest, verified signed-header
     digest and local receive time on first sight, in both contexts, before
     the row is returned. A tombstone cannot create that historical receipt.
   - The seen-id cache maps `(hub, from, client_id)` to the first hub message
     id, full-envelope digest, verified signed-header digest, content state and
     local first-seen time, for the whole window. A different hub message id
     or different full digest opens as `replayed`, except the exact monotonic
     full-to-tombstone transition specified below. The entry is written before the
     opened row is returned, so a crash cannot reopen a replay as new.
   - A fresh station starts with an empty cache. It records when its cache
     started, and envelopes created earlier than that open with `replay:
     "unchecked"`. Unchecked rows are readable and fenced and never confer
     authority. Once a full window has passed, nothing is unchecked.
6. Take `kind`, `priority`, `created_at` (as `sent_at`) and `deadline` from the
   verified header instead of the hub's columns, and, in the inbound context,
   link replies through the pending store (D5).
7. Return a row with the plain-text keys plus `direction` (`inbound` or
   `outbound`), `sealed: true`, `signature: "verified"`, `key:
   {"fingerprint", "status"}` for the signer, `confidentiality` (below),
   `replay` (`checked` or `unchecked`), `reply` (`linked`, `unlinked` or
   absent) and, for revoked-key history, `history:
   "received_before_compromise"`.

Confidentiality is a property of each envelope, independent of the signer. It
answers one question: was every key this envelope is sealed to, other than the
caller's own, a key the station trusted as the real holder's? `end_to_end`
means that key was `verified`, `opportunistic` means it was `observed`, and
`unknown` means the station has no record of it. Signer authentication is the
separate `signature` and `key` fields. The source depends on the context:

- `inbound`: the other wrap belongs to the sender, whose kid is also the
  signer, so the label follows the sender key's status. A substituted sender
  key would mean the hub wrote the message.
- `outbound`: the station signed the envelope itself, so its own key says
  nothing about who can read it. The label comes from the outbox record (D5):
  the recipient kid in `rcpt` and that key's status when the envelope was
  sealed. A verified sender sealing to an `observed`, possibly substituted,
  recipient key gets `opportunistic`. The label is not recomputed from later
  pin changes. An outbound row with no outbox record (a lost or pruned local
  store) takes the status of that kid in the pin store and key history if
  present, and `unknown` otherwise.

The `send()` and `reply()` responses, `sent()`, `sent_lookup` recovery and
every rendered projection carry this per-envelope value.

A tombstone (D8) runs steps 1 to 3 against the signed header and returns the
failed-row shape with `open_error: "content_expired"`. Its verified signed-header
representation is exactly `canonical(signed_header + sig)`, namely the complete
envelope with only `ct` and `recipients` removed. Record the SHA-256 of those
bytes at the original full-envelope receipt, alongside the SHA-256 of the full
canonical envelope. Do not confuse it with the signature input, which excludes
`sig`, or replace the original full digest with a tombstone digest.

A tombstone from a revoked signer inherits historical receipt evidence only
when its verified signed-header digest, hub/from/kid/client-id and original
message id match an earlier qualifying full-envelope receipt under D3's clock
rule. A first-seen tombstone, an altered header, or a header-only receipt cannot
establish pre-compromise history. Matching history still has no reply-link,
pending completion, sender-label or operator authority, and no plaintext.

Replay handling accepts one full-to-tombstone transition under the same triple
and message id only when that verified representation matches the stored
header digest. Preserve the original full digest/time, add the tombstone state,
and return `content_expired`. Identical tombstone repeats are ordinary repeats
of that row. A different id/header is `replayed`. Once tombstoned, a return to
full content is refused as `replayed`, so an old snapshot cannot resurrect
content. A fresh station can verify a tombstone's signature but gets no inherited
receipt or authority, and it records only nonqualifying header/replay state.

Every row, sealed or not, gets its structural fields pattern-checked: integer
ids, ISO timestamps, principal ids, and `kind` and `priority` from their
fixed sets. A row that fails is dropped from the page with a logged count.

`_frame()` then fences every sender-chosen field exactly as for plain text.
Encryption says who wrote the text and that nobody changed it, not that the
text is safe.

A failed row keeps only structural fields (`id`, `sender`, `recipient`,
`kind`, `priority`, timestamps, `acked_at`, `acked_by`), drops `client_id` and
every other sender-chosen string, and adds `sealed: true`, an `open_error`
code and a fixed body Brigade writes itself: `[sealed message not opened:
<code>]`. `fleet_inbox_delivery.render()` and the Dot pack index `body`, so
the fixed body keeps them working. `direction`, `sealed`, `signature`,
`open_error`, `key`, `confidentiality`, `replay`, `reply`, `history` and
`authenticity` join `_MESSAGE_STRUCTURAL_FIELDS`, with values from fixed sets.

Trust labels reach every surface. A label that stops at the opened row does
nothing, because three surfaces copy fixed field sets (see Current state).
Each one keeps and shows the labels:

- Live delivery: `fleet_inbox_delivery.render()` adds the labels to each
  message header line, for example `Message 41 from node-b (question),
  sealed, key observed, opportunistic confidentiality, replay unchecked, reply
  linked:`. A plain-text row shows `unauthenticated-legacy` or
  `unauthenticated-after-pin`, and a failed row shows its `open_error`. Label
  text comes from fixed strings keyed by the fixed values, never from the row.
- The Dot pack: `DotState.hub_upsert()` stores `sealed`, `key_status`,
  `confidentiality`, `authenticity`, `replay`, `reply`, `history` and
  `open_error` as new columns, and the Dot tools that list or show messages
  render them. A stored row without the columns (written before the
  migration) renders as `authenticity: unknown`, which confers nothing.
- The legacy projection: `legacy_message()` copies the same fields into its
  output dict, and its consumers render them next to the sender.

The MCP tools already return the structural fields, and `fleet_who` shows
`key: {"fingerprint", "status"}` and `confidentiality` for each principal.

Live delivery opens only the rows it shows, imports `cryptography` lazily
inside its existing `try`, and uses only pinned and cached keys. It appends to
the receipt log and the seen-id cache, reads the pending store without
changing it, and makes no extra hub call, so its one hub call and 2 second
deadline stay. A sender whose key is not cached shows the fixed body with
`sender_key_unknown`. Delivery never acks.

Ack. `fleet_ack` acks by hub id and returns the opened row when the caller can
open it. MCP Events payloads already carry no subject or body.

## D7. Operator questions (#1951)

#1951 gives the hub the whole decision about operator authority (see Current
state). A malicious or compromised hub can mark any question answered, choose
the answer text, and set `source` and `answered_by` so that `_terminal()`
reports `operator_authenticated: true`. The question text also sits in
plaintext in `fleet_questions`, and its `payload_hash` lets anyone with the
database confirm a guessed question.

Until the integration slice (plan slice 7b) lands, ask_user answers are
hub-trusted wherever plaintext is allowed, and the fleet messaging docs say so: whoever controls the hub can
produce an answer the client reports as operator-authenticated. Under
`require_sealed`, questions remain disabled until slice 7b, with
`message_sealing_required`. Do not silently alter the existing authority flag
or add an adapter that bypasses that refusal before signed verification lands.

Target design:

- The operator is the principal named by `BRIGADE_FLEET_OPERATOR_RECIPIENT`.
  Its key must be `verified` on the asking station. With an `observed` key the
  question still goes out, but no answer is ever operator-authenticated.
- `AskUser.ask` stops calling `messages._online()` for the question. It seals
  a `kind: question` message to the operator with a signed `deadline` of
  `created_at` plus the timeout, records the pending request locally (D5), and
  sends the envelope with `question_create`. The hub stops building the
  message itself (`fleet_hub_questions.py:165`).
- Before the first transmission, `ask()` persists the ask's idempotency record
  (D5): the `client_id`, the original deadline and a keyed commitment over the
  recipient, question text and timeout, under `(caller, generation,
  request_id)`. Any later `ask()` with the same `request_id`, including one
  after a restart or after the clock has moved on, reuses that `client_id` and
  deadline instead of computing new ones, so a retry after a lost response
  reseals to the same `rc` and finds the existing question.
- `fleet_questions` holds references and routing metadata only:
  `question_id`, `caller`, `generation`, `request_id`, `operator`,
  `message_id` (the question's `fleet_messages` row), `deadline`, `status`,
  `answer_id` (the answer's `fleet_messages` row) and `answered_at`. It holds
  no question text, no answer text, no envelope copy and no hash computed
  over plaintext. `question` and `answer` columns are dropped and
  `payload_hash` is removed. The trigger sets `status`, `answer_id` and
  `answered_at` from the inserted answer row and copies nothing else.
- Idempotency comes from the envelope and the idempotency key. A
  `question_create` with an existing `(caller, generation, request_id)` reads
  the referenced question row's signed header and compares its `to`, `kind`
  and `rc` with the new envelope's. Equal values return the existing
  question, and anything else is `question_request_conflict`. `rc` is a keyed
  commitment (D5), so the hub learns nothing it could test guesses against.
  After ciphertext purge the tombstone keeps the signed header, so the
  comparison still works.
- Because `fleet_questions` stores references, ciphertext purge and retention
  (D8) cover questions and answers with no extra step: when the message row's
  ciphertext is purged, no other copy remains, and a question row whose
  message rows have expired is removed with them.
- The operator answers with a sealed, signed reply whose `in_reply_to` names
  the question's full `(hub, from, client_id)` triple (D2).
- One verification function decides `operator_authenticated`, and it is true
  only when all of these hold:
  - the answer verifies under the operator's `verified` key and the accepted
    hub identity,
  - its `in_reply_to` equals the full `(hub, from, client_id)` triple of the
    local pending record for this exact ask. The answer's own `hub` may be
    the current identity or one in the endpoint's lineage (D4), so a question
    asked under `h_old` and answered after a migration to `h_new` still
    authenticates, and an answer naming any other triple never does,
  - its `from` is the operator principal and its `to` is the asking principal,
  - its signed `created_at` is no later than the question's deadline plus
    `MAX_FUTURE_SKEW`,
  - its replay status is `checked`, and
  - the operator key is not revoked. Revoked-key history (D3) never
    authenticates an answer.

  Hub-returned `status`, `source`, `answered_by`, `operator` and `answer`
  fields are hints. No unsigned hub field can set operator authority.
- Desktop answers go through the same function. A dialog shown by the asking
  station records the typed answer locally and reports it to the hub as a
  sealed audit envelope. Without an operator signature it comes back
  `operator_authenticated: false` and fenced, as #1951 treats desktop reports
  today. On a machine that holds the operator principal's own key, the desktop
  answer is signed with it and verifies like a hub-inbox answer.
- Backends. Version 1 supports `fleet_ask_user` on the full hub only. Hub Lite
  and no-hub mode return `{"code": "hub_feature_unavailable", "permanent":
  true}` (D11).

## D8. Hub storage, retention and policy

Schema changes on the Python hub. Each slice that changes the schema bumps
`SCHEMA_VERSION` or registers additive columns, as the startup gate in
`fleet_hub.py` requires. The baseline is schema 41 from #1939.

- `fleet_messages` gains `envelope TEXT`, `envelope_bytes INTEGER` and
  `content_state` (`sealed` or `content_expired`). Sealed rows store
  `subject = ''` and `body = ''`, keep `reference`, `thread` and
  `sender_label` NULL, and set `payload_hash` to the envelope digest.
- `fleet_message_keys(principal_id, kid, record, state, created_at,
  retired_at, revoked_at, compromised_from)`, with one active key per
  principal enforced by a partial unique index.
- `hub_meta` holds the signed identity statement, any migration statement, and
  the policy.
- `fleet_mesh_statements(kind PRIMARY KEY, seq, statement, uploaded_at)`
  stores the
  latest operator-signed membership manifest, key manifest and revocation
  statements as opaque blobs, uploaded with the admin token and served to
  principals with a `manifests_get` action. The hub checks only size and that
  `seq` increases. Clients verify signatures and apply the high-water rule
  (D3). A hub that withholds an update leaves stations on the last version
  they accepted, which it could not make more permissive than the operator
  signed.
- `fleet_questions` drops `question`, `answer` and `payload_hash` and
  references the question and answer message rows instead (D7).

Retention (decision Q2). The hub clears `ct` and `recipients` 7 days after a
sealed message is acked and keeps the signed header and signature as a
`content_expired` tombstone, so routing, retries (D5) and audits stay
verifiable. Unacked sealed ciphertext has a hard TTL: at most 90 days on the
Python hub
and 14 days on Hub Lite (D12), or earlier when routing retention removes it.
Routing rows and visible tombstones expire with the configured retention.
Longer routing retention can retain a header, but cannot extend sealed
freshness or keep unacked ciphertext beyond the sealed hard TTL. This 90-day hard TTL/admission
separation is a revision 5 proposal, to be reviewed with the receipt policy below,
not a claim about current runtime enforcement.

On the Python hub, `BRIGADE_FLEET_MESSAGES_RETENTION_DAYS` selects routing
retention at startup. It accepts decimal integers from 1 through 3650,
defaulting to 90 days. Invalid values refuse startup before binding. Each
server freezes its own policy, including startup migration and hourly
maintenance. Restart to change it. The policy applies equally to plaintext
legacy rows, sealed rows, tombstones, and legacy operator-question audit rows.
New mail older than this window is refused. Reads, counts, acknowledgments
and reply lookup exclude expired routing rows even before cleanup removes
them. Direct library callers without a policy retain the 90-day default.
The seven-day post-ack ciphertext purge, 90-day sealed freshness/hard TTL,
statistics retention and ten-minute directory snapshot TTL are separate policies.
For a new sealed attempt, admission requires signed `created_at` no
older than 90 days and no more than five minutes in the future, in addition to
routing retention admission. A previously stored retry is looked up first and
never inserted again because its routing row expired. A routing shrink must
report its prospective deletion count before activation. It is destructive
operator work, not an incidental part of a dependency or client update.

Routing cleanup uses the existing SQL sweep, without a row batch limit.
Ciphertext cleanup processes at most 32 envelopes per batch, with commits
between maintenance batches.

### Proposed retry receipts, pending parent/Astra/Daybreak review

Keep the documented routing setting. Recommend a separate bounded retry-receipt
table retaining only signed-header/commitment metadata for 90 days after first
admission, independently of visible routing deletion. This is a product policy
proposal: routing deletion would no longer delete all sender/idempotency metadata.
An alternative is to shorten the retry guarantee to routing retention and fail
closed as soon as that horizon is reached. That avoids additional retention
but breaks the current 90-day retry promise on short-retention hubs. Neither
choice authorizes implementation until the parent settles it with the reviewers.

If adopted, the exact lookup/uniqueness prerequisite is:

- Persist `envelope_hub` from the parsed signed header on sealed rows. Enforce
  unique `(envelope_hub, sender, client_id)` for sealed attempts. Keep a
  separate legacy `(sender, client_id)` uniqueness rule for plaintext rows
  (`envelope_hub` absent). Backfill existing sealed rows by parsing their
  canonical envelope/tombstone and fail migration on ambiguity or malformed
  data, never infer their hub from the current identity.
- Create the receipt in the same insert transaction as the message, not during
  later cleanup. Key it uniquely by `(hub, sender, client_id)` with original
  message id, original full-envelope digest, canonical signed-header-plus-sig
  bytes, first admitted time and expiry. That representation retains `rc`,
  payload/wrap digests and routing linkage, but no `ct`, wraps, plaintext,
  tokens or private keys. A receipt proves only what its verified signature
  and commitment cover. It is not a new mailbox row or approval receipt.
- Proposed scoped request is exactly `{"action":"sent_lookup","hub":<id>,
  "client_id":<id>}`. Sender is the authenticated caller. It returns the
  existing `{"message":...}` envelope/tombstone while routing is visible,
  otherwise `{"receipt":{hub, sender, client_id, id, header, envelope_sha256,
  first_admitted_at, expires_at}}`. `header` is canonical JSON text of the
  signed-header-plus-sig, `envelope_sha256` is lowercase full hex, and times
  use D2 grammar. Clients distrust unsigned ids/times as evidence of content
  or authority and verify the signed header and locally recorded attempt.
  Absence is 404 `not_found`, never an instruction to reseal automatically.
- The existing request without `hub` remains accepted for legacy clients. One
  matching visible row/receipt returns its existing result shape (a receipt
  becomes a `content_expired` message projection with its original id/header).
  More than one match across hub or legacy namespaces returns permanent 409
  `message_lookup_ambiguous` with no arbitrary row selection. Zero matches
  stays 404. New clients always supply hub. If scoped lookup has no sealed
  match but a legacy plaintext row shares the sender/client-id, return that
  own plaintext row as `{"message":...}` for conflict reconciliation. A new
  sealed insert with that legacy blocker returns 409 with its existing id.
  The client reports `content_unverifiable`, never sealed delivery or an
  implicit plaintext-to-sealed new attempt. An existing exact sealed match
  takes precedence for scoped lookup. Omitted-hub lookup still refuses ambiguity.
- Duplicate bytes return the original id before current key/hub checks.
  Different bytes or a receipt-only match return 409
  `message_client_id_conflict` with the original id for signed `rc`
  reconciliation. Never redeliver from the receipt. The duplicate check,
  identity fence, grants and insert share one transaction. Foreign-hub or
  `mesh` new envelopes and key publications are refused when an active hub
  identity exists. Historical lookup remains available to its authenticated
  sender. Match a sealed reply's parent using all three signed linkage fields.
- Receipt expiry is fixed at first admission plus 90 days, never extended by
  reads/retries. Local retry expiry is fixed before first send at its original
  creation plus 90 days, and never extended by reseal/migration. After local
  expiry, or loss of the attempt record/commitment needed to prove semantics,
  fail `message_retry_expired` or `content_unverifiable` without new delivery.
  The guarantee is bounded: a caller bypassing the client and inventing a
  new post-expiry attempt cannot get eternal deduplication from deleted state.
- Account for canonical receipt bytes, visible header/envelope bytes and
  index/storage overhead in sender and fleet quotas. A committed send reserves
  its receipt space through expiry. Cleanup cannot evict unexpired receipts
  to make room. Quota exhaustion refuses new writes. Counts, inbox, ordinary
  sent lists, ack and reply lookup exclude receipt-only rows, so short routing
  retention does not leave visible mail. Hub Lite's 30-day visible tombstone
  deletion must leave the separate bounded receipt if this proposal is adopted.

Static keys give no forward secrecy, so a key stolen later opens every copy that still
exists. Deletion protects only against a later compromise of an honest hub. A
malicious hub can keep copies forever and cannot be made to delete them.

Storage budget. Stored bytes are about messages per day times retained days
times the average envelope size. At 10,000 messages a day of 96 KiB
envelopes, 90 days of retention is about 88 GB, and 7 days is about 6.9 GB. A
short typical message is about 5 KiB sealed, mostly the two hybrid wraps. The
Python hub gains a per-sender stored-bytes limit (default 1 GiB) and an
optional fleet-wide limit, both refusing writes with `sender_storage_quota`
or `hub_storage_full` before the disk fills. `brigade doctor` warns when no
fleet-wide limit is set.

Policy. `brigade fleet serve --require-sealed` (or
`BRIGADE_FLEET_MESSAGES_REQUIRE_SEALED=1`) refuses plain-text sends with
`message_sealing_required`, and the identity statement reports it. Stored
plain-text rows stay readable as legacy. Hub Lite always requires sealing.

### Proposed safe policy activation, pending parent/Astra/Daybreak review

The current upload guard requires a statement to match frozen runtime policy,
and startup requires the stored statement to match configuration. Together
these prevent a safe `require_sealed` flip. Preserve both active-policy guards.
Do not omit signed fields, loosen both guards, or serve `require_sealed: true`
before runtime actually refuses plaintext.

Proposed path: admin stages a complete higher-sequence signed identity as an
inactive candidate, bound to the expected active identity digest/sequence and
exact target runtime settings. Staging returns `restart_required` and does not
change `/mesh/identity` or current enforcement. On controlled restart, before
binding/listening or accepting writes, validate the candidate, expected active
generation, target configuration and any migration proof. Under the exclusive
startup/write lock activate the candidate and its policy generation atomically,
then serve only with that same frozen runtime policy. A mismatch refuses startup
and leaves active state unchanged. Crash before commit leaves the old generation.
Crash after commit restarts only with the committed settings, never old flags.
In-process activation is an alternative only with one atomic enforcement/identity
generation and a write barrier covering every plaintext/sealed entry point.
It requires more concurrency proof than the staged restart path.

Rollback after activation is a new explicit higher-sequence operator-signed
transition, never restoration of the old sequence, omitted fields or an older
binary. Relaxing policy requires a separate operator decision. Existing schema
version refusal remains the downgrade boundary. A staged candidate is not a
current identity, cannot seed pins, and is never returned as active policy.
Automatic client policy changes may only tighten confidentiality. A staged
candidate must keep the same identity, endpoint, backend, suites and envelope
versions, cannot add capabilities, cannot turn required sealing off, and
cannot raise the envelope cap. Clients apply these restrictions even if a
malicious Hub serves an inactive candidate as its current identity. A higher
sequence does not authorize relaxation. Version 1 does not implement automatic
remote relaxation. A future local operator-acceptance or independently signed
activation protocol needs its own reviewed contract. Tightening early can
reduce availability, but cannot authorize plaintext that the accepted policy
previously forbade.
Tests must kill/restart around every stage/activation boundary, attempt concurrent
plaintext writes, simulate stale candidates and old runtime rollback, and
check that advertised strictness always equals effective enforcement. This
proposal also needs rollout documentation and parent/security review before code.

For a sealed send the hub derives `to`, `kind`, `priority`, `client_id` and
`deadline` from the envelope header. It refuses a send when `from` is not the
caller, when `from_kid` is not the caller's active kid (`sender_key_stale`),
when the recipient kid is not the recipient's active kid
(`recipient_key_stale`, 409, nothing stored), or when `reply_to` and
`in_reply_to` are not both present or both absent. Quotas, grants and
recipient checks are unchanged.

## D9. Invites and teammate principals

Token format:

```text
bfi1.<hub_id>.<invite_id>.<secret>.<inviter_fingerprint>[.m<seq>-<digest>]
```

- `hub_id`: the hub's identity (D4). It commits to the hub identity key.
- `invite_id`: 16 random bytes, base64url.
- `secret`: 32 random bytes, base64url, never sent to the hub as is.
- `inviter_fingerprint`: the first 16 bytes of the inviting principal's
  fingerprint (D3), or `none` for a bootstrap invite.
- An optional sixth field, `m<seq>-<digest>`, carries the trusted membership
  floor of D3: the minimum manifest `seq` and the full SHA-256 of that
  manifest's canonical form, base64url. The planned `fleet invite create
  --membership-floor` adds the latest manifest the admin machine holds. The
  joiner enforces it as D3 describes. A token without the field leaves the
  joiner with the fresh-station limitation of D3.

The invite is not a navigable URL. Round 1 put the secret in the fragment of a
link to the hub, but a page served from the hub's origin can read its own
fragment, so a malicious hub could collect secrets from anyone who opened the
link. The token is redeemed only by the CLI, the hub URL travels as a separate
field, and the hub serves no join page. The planned `fleet join` takes the
token from a file (`--invite-file`) or from stdin, never from argv, which
shows in process lists and shell history. Pasting a token into an agent
transcript exposes it to anyone who can read that transcript until it is
redeemed or expires. For that use the docs recommend `--expires 1h`.

Two keys come from the secret with HKDF-SHA256: `redeem_key` with info
`brigade-invite/v1/redeem` and `bind_key` with info `brigade-invite/v1/bind`.
The hub receives only `sha256(redeem_key)` at creation and never sees
`bind_key`.

Create, list and revoke. The planned `fleet invite create --role teammate
--expires 24h` (with `--max-uses`, `--as <principal>`, `--grant <pattern>` and
`--name <person>`) needs the admin token. `--as` names the inviting principal,
whose private key must be on the machine running the command, so the
fingerprint and `bind_key` stay where the teammate's mail will be opened.
Expiry defaults to 24 hours, with a maximum of 7 days. Only the admin token
can create, list or revoke invites. A node or agent token alone cannot.
`--as` may name a node id or an `agent:` principal, and the bootstrap node
(below) is the usual inviter.

Join, with idempotent redemption:

1. Parse the token. Fetch `/mesh/identity` from the given hub URL, verify the
   statement's signature, and check that `hub_id` is the hash of its key and
   equals the token's. Record the identity as `verified` for that endpoint. A
   known endpoint with a different identity stops with
   `hub_identity_changed`.
2. Create or reload a join attempt in an owner-only local file before any
   network write: a random `join_attempt_id`, the hub id, the invite id, a
   fresh key pair and key record, a client-generated principal token, and the
   requested name. This mirrors principal enrollment, where the client
   supplies the request id and the token.
3. `POST /invites/redeem` with `invite_id`, `redeem_key`, `join_attempt_id`,
   the token, the name, the key record and `binding =
   HMAC-SHA256(bind_key, canonical(key record))`.
4. The hub, under `BEGIN IMMEDIATE`, first compares `sha256(redeem_key)` in
   constant time and refuses an unknown invite or a wrong secret with the
   generic refusal. Then:
   - A retry: the invite already has a use with this `join_attempt_id`. If
     the token digest, key record and name are identical and the use's
     principal is still active, it returns the same success without counting
     a new use. If any value differs, or the principal was removed or its
     token revoked, it returns the generic refusal and changes nothing, so a
     retry after member removal never revives a credential or creates a new
     principal.
   - A new attempt: no use has this `join_attempt_id`. It is admitted while
     the invite is unexpired, unrevoked and `uses < max_uses`, and refused
     otherwise. Each admitted attempt increments `uses`, creates its own
     principal with the client-supplied token digest, stores the exact key
     record as that principal's active key, writes the reciprocal grants
     below, and writes an audit row.
   - Every use row stores `join_attempt_id`, `principal_id`, the token
     digest, the name, the full key record and the `binding`, in the full
     hub's `fleet_invite_uses` and in Hub Lite's `invite_uses` (D12).
5. The response carries the principal id, the inviter's principal id and key
   record, the hub id and the stored use row. The joiner checks the inviter's record against the
   token fingerprint, pins it `verified` with source `invite`, installs the
   token file, and only then deletes the attempt's secret material.

A lost response, or a crash between redemption and token install, is
recovered by rerunning the same command: it reloads the attempt and redeems
again, and step 4 returns the same success. Another holder of the same
secret can redeem only while uses remain, which is what `--max-uses` permits,
and each such redemption is a separate principal with its own binding.

Pending invitation trust. `invite list` and the planned `fleet invite verify`
return every use row with its stored key record and binding. For each use, the
inviter's client:

- verifies `binding` against the stored key record with `bind_key`, checks
  that the record's `self_sig` is valid and that its principal and hub match
  the use, and pins exactly that record (its kid) `verified` with source
  `invite`. If the hub currently serves a different key for that principal,
  the entry is `changed` relative to the verified record, or
- on a mismatch, records `binding_mismatch` for that use, exits nonzero and
  pins nothing, or
- when the hub serves no binding or no key record, records `binding_missing`
  the same way.

Until a use verifies, its principal is `pending_binding` on the inviting
station, and sends to it and mail from it are refused with
`invite_binding_pending`. A missing or failed binding never downgrades to
`observed`. The inviter's local invite record keeps `bind_key`, even past the
invite's expiry, until the invite can produce no unprocessed use: every
permitted use has been redeemed and processed, or the invite has expired or
been revoked and every recorded use has been processed. Deleting it after the
first success would leave later uses of a multi-use invite unverifiable.
Removing a member marks that use processed. On other stations a teammate is
`unverified` until the operator adds it to a signed key manifest (D3).

Bootstrap. A fresh hub has no keyed principal, so the first invite carries
`none` as the inviter fingerprint and is created with `--bootstrap --role
node`. The first machine joins on the strength of the token alone, which still
verifies the hub identity, and every later invite comes from it.

Redemption is rate limited per source address and globally. Failed attempts
are counted per invite per hour so a guessing run cannot fill the audit table.

Teammate principals. A teammate machine becomes an `agent:` principal, for
example `agent:dana.laptop`, with a `role` column set to `teammate` and a
`person` column. The `agent:` prefix keeps every existing grant check in
force. The prefix test repeated in `permits()`, `require_granted()`,
`require_recipient_cap()`, `_station_read()` and `fleet_hub_events.permits()`
becomes one helper that looks the caller up and treats only enrolled node ids
as unrestricted, so an unknown prefix fails closed.

Each machine gets its own principal, token and key.

Grants (decision Q6). For any pair of principals that includes a teammate,
grants are symmetric: the sender's grant must name the recipient and the
recipient's grant must name the sender. Both checks run in the same
`BEGIN IMMEDIATE` transaction as the insert, so a grant revoked concurrently
either blocks the send or takes effect for the next one, never half of each.
Agent-to-agent mail with a teammate on either end needs reciprocal grants.

Node grants. A node has no grant rows today, so a node inviter could never
satisfy the recipient half of the rule. `fleet_message_send_grants` accepts an
enrolled, unrevoked node id as `principal_id`, with the same patterns and the
same 64-pattern limit. A node's rows are consulted only in a pair that
includes a teammate, which keeps nodes unrestricted toward everyone else.
Tightening node-to-agent grants is a separate follow-up. The existing admin
action `grants` on `POST /message-principals` accepts a node id for an
enrolled node and refuses an unknown or revoked one, and the existing
`brigade fleet msg principals grants` command gains a planned `--node <id>`
option. Revoking a node deletes its grant rows in the same transaction.

Redemption writes the reciprocal pair. In step 4 the hub adds the new
teammate to the inviting principal's grant, node or agent, and gives the
teammate a grant naming the inviter plus the invite's `--grant` patterns, all
in the redemption transaction. Mail between the inviter and the teammate then
works in both directions, and removing either side's grant blocks both.

| Capability | Teammate default |
|---|---|
| `fleet_send`, `fleet_reply` | only to the inviting principal plus any `--grant` patterns, and only where the recipient's grant names the teammate |
| Receiving mail | only from principals in its grant list |
| `fleet_who` | principals and nodes in its grant, with key fingerprints. No session repo, branch or harness details. |
| `fleet_claims` | refused with `principal_role_forbidden` |
| MCP Events | `message.created` for its own inbox only |
| `fleet_submit`, dispatch, runs, admin reads, invites | not available |

Grants are widened with the existing `brigade fleet msg principals grants`
command, and with its planned `--node` option for a node's side.

Audit. An append-only `fleet_audit_log(id, at, actor, action, subject,
detail)` records `invite.create`, `invite.redeem`, `invite.redeem_refused`
(aggregated), `invite.revoke`, `member.remove`, `grants.change`,
`key.publish`, `key.rotate`, `key.revoke`, `hub.identity`, `hub.migration`
and `statement.upload`. `detail` holds ids, roles, grant patterns and counts, never
secrets, tokens or message content.

Revocation stops delivery at once: a revoked token gets 401 on its next call,
and sends to a revoked principal fail with `message_recipient_inactive`.

## D10. No-hub mode

#2007 asks for three paths to be compared.

### Path a: an iroh daemon shipped as a native engine

iroh 1.0 shipped on 2026-06-15, and 1.4.1 is current (2026-10-22) [IROH-REL].
It is dual licensed MIT or Apache-2.0, and `iroh-relay` ships as prebuilt
binaries for Linux, macOS and Windows [IROH-REL]. A relay sees encrypted QUIC
plus "source and destination IP addresses, connection times, and the amount of
data transferred" [IROH-PUB]. A self-hosted relay can restrict access to an
allowlist of endpoint ids, a shared token or an HTTP callout [IROH-RLY]. n0's
public relays are free, rate limited and "suitable for development and
testing" [IROH-CON]. Default address lookup publishes signed pkarr records
with each endpoint's home relay to n0's DNS, and mDNS is opt-in [IROH-LKP].
The Python binding (`iroh` 1.2.0 on PyPI) is marked Alpha [IROH-PY].

iroh gives transport: NAT traversal, LAN discovery and a relay the user can
host. It has no durable mailbox, so offline delivery, receipts, retries and
storage are Brigade's to define.

### Path b: interoperate with Reason Network

Reason Network is the closest existing system: per-machine daemons on iroh, a
stdio MCP shim, invites and an `ask_user` tool [RN-README] [RN-DES]. Read at
release v0.12.9 (commit 039b957) on 2026-10-22:

- It does version its wire protocol: `src/net.rs` defines ALPNs `tincan/1` and
  `tincan/join/1`, `PROTO = 1`, and a `Hello { team, proto }` frame, and logs a
  peer that speaks another protocol number [RN-NET]. Presence frames also
  carry a feature list.
- Messages travel as `Wire::Msg` frames with no per-message signature. Only
  `ask_user` approvals are wrapped in a signed type [RN-NET] [RN-PROTO].
- The iroh machine key is the identity and the team roster signing key.
  Confidentiality is iroh's QUIC encryption between machines, with no content
  layer [RN-DES].
- Its design notes say a removed machine "is not made cryptographically
  powerless: this is for friends" (DESIGN.md line 166) and that `ask_user`
  approvals are "a speed bump, not a guarantee" (line 365) [RN-DES].
- The project was created on 2026-10-16 and shipped five releases in about 18
  hours around v0.12.9 [RN-REL].

Brigade does not pursue interop, for these reasons:

- Identity and storage requirements differ. #2005 needs per-principal keys
  separate from transport keys and sealed content that rests on hubs and
  anchors. Reason Network binds identity to the machine's transport key and
  keeps confidentiality at the transport layer.
- It has no general message signature, which D2, D5 and D7 depend on.
- There is no published interop contract beyond the source and DESIGN.md, and
  no demonstrated compatibility across releases.
- Tracking a young protocol would be an ongoing compatibility cost.

Revisit if Reason Network publishes a wire specification with a compatibility
policy. Users can already run its MCP server next to Brigade's.

### Path c: hub-less store-and-forward

Each node keeps its own inbox and outbox, peers deliver to each other, and an
optional relay carries only ciphertext. This is a protocol, not a transport:
behind NAT, two peers still need something like iroh.

### Decision (Q5)

Path c's model over path a's transport, staged:

1. A bounded spike (plan slice 15) measures binary size, cold start, relayed
   connection between two networks and the direct-path rate against
   thresholds fixed before measuring.
2. A written wire protocol (slice 15b) specifies receipts, retries,
   admission and storage before any production code:
   - transport receipts (`stored`) and signed application acks under
     `brigade-mesh/v1/ack`,
   - outbox retry with backoff and idempotency on `(from, client_id)`,
   - admission: frame size, per-contact rate and per-contact stored bytes,
   - inbox storage caps and retention matching D8.
3. The production daemon (slices 18a to 18e) starts only after the spike
   passes and the protocol is reviewed.

Shape, unchanged from round 1 where not stated:

- Brigade owns the protocol: the same version 1 envelopes, signed key records
  as contact cards, a local SQLite inbox, and the existing sender spool as the
  outbox.
- `engines/mesh/` adds a small Rust binary, `brigade-mesh`, on iroh 1.x,
  installed by `brigade setup` like the code-graph engine. It accepts frames
  on ALPN `brigade-mesh/1`, writes received envelopes to the local inbox
  database, sends from the outbox and returns receipts. Python and the engine
  share the SQLite database in WAL mode. The engine never decrypts.
- The iroh endpoint key is separate from the identity key. The key record's
  `transport.iroh` field, covered by `self_sig`, binds them.
- Offline delivery: the outbox retries for 7 days. An optional later anchor
  mode lets an always-on Brigade node hold sealed envelopes for contacts that
  opted in. Users who need delivery between machines that are never online
  together can deploy Hub Lite.
- The relay is an explicit user choice at the planned `mesh init` (decision
  Q4): `--relay <url>` for a self-hosted `iroh-relay` with an endpoint
  allowlist, `--relay n0-public` (labeled for development and testing, rate
  limited, publishes the endpoint id and home relay to n0's DNS), or
  `--relay none` (LAN, tailnet and direct addresses only). There is no
  default.
- No-hub invites use a `bmi1.<payload>` token through the same `fleet join`
  rules: file or stdin only. The payload carries the inviter's key record with
  its endpoint id, relay and address hints, a one-use secret and an expiry.

## D11. One MCP tool contract for hub, Hub Lite and no-hub

`brigade fleet mcp` picks a backend from `fleet.mode` (`hub`, the default when
a hub URL is configured, or `mesh`). Hub Lite is the `hub` backend with a
different URL and token, and it reports itself through the signed `backend`
and `capabilities` fields of its identity statement (D4). All backends expose the same eight tools: `fleet_ask_user`,
`fleet_who`, `fleet_send`, `fleet_inbox`, `fleet_ack`, `fleet_reply`,
`fleet_sent` and `fleet_claims`. The live delivery hook and
`brigade fleet msg` move onto the same interface.

Input schemas stay exactly as the integration baseline defines them in
`tool_specs()`, including #1951's `fleet_ask_user`, #2010's `client_id` and
annotations and #2009's `count_only`. Ids and cursors are opaque strings to
callers. Each backend defines its own cursor encoding: #1939's opaque `who`
and `claims` cursors on a hub, and local opaque cursors in mesh mode.

| Tool | Full hub | Hub Lite | No-hub |
|---|---|---|---|
| `fleet_ask_user` | sealed questions and signed answers (D7) | `hub_feature_unavailable` | `hub_feature_unavailable` |
| `fleet_who` | nodes, principals, sessions, key fingerprints, status and confidentiality | the same, from Hub Lite's presence | contacts with key fingerprints, `online` and `last_seen_at`. `sessions` is empty. |
| `fleet_send`, `fleet_reply` | sealed per D5, `client_id` replay per #2010 | sealed only, 32 KiB cap | always sealed. `delivery` is `delivered` or `spooled`. |
| `fleet_inbox` | hub long-poll, `count_only` | the same, `wait_ms` up to 20 s | local database, waited on locally |
| `fleet_ack` | hub ack | hub ack | local ack plus a signed ack receipt to the sender |
| `fleet_sent` | hub sent list | hub sent list | local outbox with receipt state |
| `fleet_claims` | claims | `hub_feature_unavailable` (deferred, D12) | `hub_feature_unavailable` |

`hub_feature_unavailable` is returned as `{"code": "hub_feature_unavailable",
"permanent": true}`. On a hub the client decides from the accepted identity
statement: a tool whose capability (`questions` for `fleet_ask_user`,
`claims` for `fleet_claims`) is not listed returns the code without a
request, so no probing is needed. Hub Lite also answers the `question_*`
actions and `/claims` with 404, and the client maps a 404 on a listed
capability to the same code without retrying.

Mesh mode never falls back to a hub. With `fleet.mode = "mesh"`, every tool is
served by the mesh backend even when a hub URL and token are also configured,
and a tool the mesh backend lacks returns `hub_feature_unavailable`. Sending a
question or a claim to a configured hub would move data the user chose to keep
off a hub, so switching requires changing `fleet.mode`.

New fixed error codes join `_SAFE_ERROR_CODES`: `mesh_extra_missing`,
`mesh_backend_unsupported`, `recipient_key_missing`, `recipient_key_changed`,
`recipient_key_stale`, `recipient_key_unverified`,
`recipient_key_not_member`, `sender_key_stale`,
`local_key_superseded`, `invite_binding_pending`, `hub_identity_changed`,
`hub_identity_unverified`, `hub_endpoint_mismatch`, `message_retry_expired`,
`message_lookup_ambiguous`,
`signed_statement_rollback`, `signed_statement_conflict`,
`message_sealing_required`, `message_too_large`,
`message_client_id_conflict`, `question_request_conflict`,
`sender_storage_quota`, `hub_storage_full`,
`hub_daily_quota`, `principal_role_forbidden`, `hub_feature_unavailable` and
`mesh_engine_unavailable`. Open errors on rows (D6) are a separate fixed set.

The no-hub docs say plainly that operator questions, claims, runs, patrol,
dispatch and MCP Events need a full hub.

## D12. Hub Lite

A template under `workers/hub-lite/` that a user deploys into their own
Cloudflare account. Brigade runs no shared instance.

Shape. One Worker routes requests to one SQLite-backed Durable Object per
fleet. The object does authentication, storage and long-poll wake-ups.

Scope change from #2008 (decision Q7). #2008 lists presence, the inbox and
claims. Claims are deferred until messaging, invites, presence and the
free-plan measurements in plan slice 14 pass, and then get their own issue.
`brigade run` keeps using a full hub for claims. Operator questions are also
out of version 1 (D7, D11).

| Route | Actions | Auth |
|---|---|---|
| `GET /health` | liveness | none |
| `GET /mesh/identity` | the signed identity statement (D4) | none |
| `POST /messages` | `send`, `reply`, `list` (`wait_ms` up to 20 s), `count`, `sent`, `sent_lookup`, `ack`, `who`, `keys_publish`, `keys_get`, `manifests_get` | principal or node token |
| `POST /invites` | `create`, `list`, `revoke` | admin token |
| `POST /invites/redeem` | redeem | invite secret |
| `POST /message-principals` | `get`, `grants` (agent or node principal), `revoke` | admin token |
| `POST /hub-identity` | upload a statement or migration | admin token |
| `POST /mesh/statements` | upload a signed membership manifest, key manifest or revocation statement | admin token |
| `POST /sessions`, `GET /sessions` | presence heartbeat and list | node token |
| `GET /audit` | audit log page | admin token |

Every other route or action returns 404, including the `question_*` actions,
`/claims`, `/events`, `/status`,
`/cloud`, `/models`, `/policy`, `/preference`, `/owners`, `/work`, `/ci`,
dashboard enrollment, deck pages and MCP Events webhooks.

Storage layout (SQLite in the Durable Object):

```text
meta(key PRIMARY KEY, value)                      -- identity statement, schema version
principals(principal_id PRIMARY KEY, role, name, person, label,
           token_hash UNIQUE, created_at, revoked_at)
grants(principal_id, pattern, PRIMARY KEY(principal_id, pattern))  -- agent or node ids
statements(kind PRIMARY KEY, seq, statement, uploaded_at)
keys(principal_id, kid PRIMARY KEY, record, state, created_at, retired_at,
     revoked_at, compromised_from)
messages(id INTEGER PRIMARY KEY AUTOINCREMENT, sender, recipient, kind, priority,
         hub, client_id, reply_to, header, envelope, envelope_bytes, payload_hash,
         content_state, created_at, received_at, acked_at, acked_by,
         UNIQUE(hub, sender, client_id))  -- proposed sealed namespace from D8
retry_receipts(hub, sender, client_id, id, header, envelope_sha256,
               first_admitted_at, expires_at, PRIMARY KEY(hub, sender, client_id))
               -- proposed separate 90-day metadata retention, D8
usage(sender, day, sends, stored_bytes, PRIMARY KEY(sender, day))
sessions(node_id, session_id, harness, state, repo_label, branch,
         heartbeat_at, expires_at, PRIMARY KEY(node_id, session_id))
invites(invite_id PRIMARY KEY, redeem_hash, role, inviter, name, grants,
        created_at, expires_at, max_uses, uses, revoked_at)
invite_uses(invite_id, join_attempt_id, principal_id, token_hash, name,
            key_record, binding, at, PRIMARY KEY(invite_id, join_attempt_id))
audit(id INTEGER PRIMARY KEY AUTOINCREMENT, at, actor, action, subject, detail)
```

`messages` has no `subject` or `body` column, so the Worker cannot store
plaintext by mistake, and sends without an envelope are refused. `header`
keeps the signed header after `envelope` is purged.

Free plan limits, from Cloudflare's docs as of 2026-10-22 [CF-WLIM] [CF-WPRC]
[CF-DOPRC] [CF-DOLIM]:

| Limit | Free plan | Effect on Hub Lite |
|---|---|---|
| Worker requests | 100,000 per day, then Error 1027 | Every hub call is one Worker request. |
| Durable Object requests | 100,000 per day | Every hub call is also one object request. |
| Durable Object duration | 13,000 GB-s per day, billed at 128 MB | One object awake all day uses 10,800 GB-s (83 percent). |
| SQLite rows written | 100,000 per day | Likely the binding limit for mail. |
| SQLite rows read | 5,000,000 per day | Not binding at fleet scale. |
| Storage | 5 GB per account. One FAQ line says 1 GB per object (UNVERIFIED). | The budget assumes 1 GB per object until slice 14 measures it. |
| Worker CPU | 10 ms per request | Envelope checks are string and JSON work. |
| Over a limit | further operations fail with errors, no billing | Hub Lite refuses with its own codes first. |

Storage budget and admission. Stored bytes are messages per day times retained
days times the average envelope size, so Hub Lite bounds all three:

- Envelope cap 32 KiB (`max_envelope_bytes` in the identity statement).
- Ciphertext is deleted at the earlier of 7 days after ack and 14 days after
  creation (the hard unacked TTL). The visible tombstone header is deleted at 30
  days. The proposed D8 retry
  receipt has its own 90-day metadata horizon, subject to parent review.
  Cleanup runs from a Durable Object alarm.
- Admission refuses writes before Cloudflare does: a fleet ciphertext ceiling
  of 800 MB (80 percent of the assumed 1 GB, leaving headroom for tombstones,
  indexes and cleanup lag) returns `hub_storage_full`, a per-sender ceiling of
  100 MB returns `sender_storage_quota`, and a fleet send ceiling of 6,000 a
  day returns `hub_daily_quota`. Any adopted D8 receipt budget must fit in
  the total storage ceiling and reserve bytes before admission. All three are
  retryable for the client's spool and visible in `brigade doctor`.

Worked numbers. Over the 14 day maximum residency, 800 MB holds about 11,000
typical 5 KiB messages a day, or about 1,800 a day at the 32 KiB cap. The
6,000-a-day send ceiling keeps a typical mix inside both the storage ceiling
and the rows-written estimate below, and the per-sender ceiling stops one
sender of large mail from using the whole fleet budget. Slice 14 replaces
every one of these figures with measurements.

Request budget, to be measured rather than trusted:

- Session presence upserts on every `PreToolUse` and `PostToolUse`
  (`fleet_session_presence.py`), live delivery polls on tool events, and a
  waiting MCP station issues three 20 second long-polls a minute. Hub Lite
  needs two client throttles: presence coalesced to one heartbeat per session
  per 60 seconds unless state changes, and a 60 second delivery poll. With a
  waiting station, a busy session then costs about 7,200 requests a day, so
  roughly 10 to 12 sessions busy all day, before sends.
- Durable Object duration is billed per object while awake, so one object per
  fleet stays under the cap even with several waiting stations. The docs
  recommend one Hub Lite per Cloudflare account on the Free plan.
- A message costs roughly ten written rows over its life. The 6,000 send
  ceiling uses about 60 percent of the rows-written budget, leaving the rest
  for presence, keys and cleanup.
- Presence keeps heartbeat times in memory and writes a session row only on a
  state, repository or branch change, or once per third of the TTL.

Long-poll. `list` with `wait_ms` parks the request on an in-memory waiter keyed
by recipient. Messages are written to SQLite before any waiter is woken, and
clients already retry an interrupted wait. WebSocket hibernation would remove
the duration cost of idle waiters [CF-DOWS], but the Python client has no
WebSocket library, so it waits for a later slice.

Tests run against workerd. The shared HTTP contract suite (plan slice 11) runs
against `wrangler dev`, which runs the Worker in workerd with a local Durable
Object. That run is the acceptance bar for each Hub Lite slice. Unit tests
with a `node:sqlite` fake stay as fast checks and never count as acceptance on
their own.

Deployment. A "Deploy to Cloudflare" button clones the template into the
user's GitHub or GitLab account and provisions bindings including Durable
Objects [CF-DEPLOY]. It needs a public repository and treats a subdirectory as
the repository root, so `workers/hub-lite/` is self-contained. Whether the
button applies the Durable Object migration on first deploy is UNVERIFIED, so
slice 14 tests it on a fresh account, and the docs give `npx wrangler deploy`
from a clone as the fallback. Hub Lite needs a URL, so the template sets
`workers_dev = true`. After deploy the admin uploads the identity statement
and creates the bootstrap invite.

Cloudflare Access can sit in front of the admin routes [CF-ACCESS]. Hub Lite's
own tokens and sealed bodies do not depend on it.

## D13. What stays plain text, and why

| Data | Why the hub must read it |
|---|---|
| Claims (target, owner, holder digest, expiry) | The hub arbitrates mutual exclusion, and patrol and the deck read them. |
| Presence and sessions | Routing, `fleet_who` and the deck. Teammates see a reduced view (D9). |
| Run events and status | Patrol, run recovery, policy routing and the deck. |
| Message routing metadata, including question deadlines | Authorization, grants, quotas, retries, ordering, expiry and `message.created` events. |
| Keys and the hub identity statement | Public by design. |
| Grants, invites and the audit log | The hub enforces them. The audit log holds no secrets or content. |

The user docs say this in one place: a hub operator can see who messages whom,
when, how often and roughly how much, and everything about claims, runs and
presence. A hub operator cannot read sealed subjects, bodies, questions or
answers exchanged between `verified` keys.

That last sentence does not hold for `observed` peers on first contact. A hub
that substitutes an `observed` peer's key before the station first pins it can
read everything sealed to the substituted key, and the pin then keeps the
substitution in place. This is opportunistic confidentiality: it protects
against a hub that is honest at first contact and only reads stored data
afterwards. Rows, `fleet_who` and every rendered surface label it
`confidentiality: "opportunistic"` (D6), and the docs recommend
`BRIGADE_FLEET_MESSAGES_REQUIRE_VERIFIED=1` for anyone who verifies
fingerprints. Teammates and unlisted principals never get this path (D3).

## Threat model

Assets: message subjects and bodies, operator questions and answers, private
keys, principal tokens, and the integrity of who said what.

| Party | Can see | Can do | Cannot do |
|---|---|---|---|
| Hub operator (self-hosted hub or Hub Lite owner) | routing metadata, padded sizes, claims, presence, runs, keys, grants, audit log, tokens as presented | Drop, delay or reorder mail. Replay an envelope under a new id, which a station that saw the original catches and a fresh station labels `unchecked`. Serve a substituted key on first contact for a principal the membership manifest lists, and read what is then sealed to it (opportunistic confidentiality, labeled on every surface), or withhold a never-pinned key so `auto` sends plain text. Pass off a revoked key as retired, which matters only with a stolen key. Report a revocation with an earlier `compromised_from` than the operator stated. The earlier-wins rule honors it, so the hub can suppress legitimate history from that key, an availability limit that never forges authority. Withhold a newer signed statement, or serve a fresh station an old signed first copy, unless the station holds a trusted floor (D3). Mint principals with the admin token. Keep copies of ciphertext forever. Until slice 7b, manufacture an operator answer. | Read sealed content exchanged between `verified` keys. Get a teammate, or any principal the membership manifest does not list, accepted as `observed` by presenting it as own-fleet. Keep a removed member usable on a station that has accepted the manifest removing it. Forge a message from a pinned principal. Roll a station back to an older signed statement. Change its identity for a known endpoint without the old identity key or the operator. Link a reply to a request it was not sent for. Make an unsigned field confer operator authority (after slice 7b). Get plain text sent to a pinned peer. |
| Relay (iroh relay, n0 public relays, anchor) | endpoint ids, IP addresses, timing and volume [IROH-PUB]. An anchor also sees routing ids and padded sizes. | drop or delay traffic | read or forge messages or key records |
| Cloudflare, for Hub Lite | the same as a hub operator, plus bearer tokens in TLS, and it can change the running code | everything a hub operator can, including sending as a principal with a stolen token. Such mail is either plain text, which a pinned recipient labels `unauthenticated-after-pin` and never treats as authoritative, or signed by a key the recipient has not pinned, which opens as `sender_key_changed`. | the same limits as a hub operator |
| A teammate | its own mail, and ids, labels and fingerprints of principals in its grant | send to principals that granted it. Forward a signed message it received: Ed25519 signatures are not deniable, so a message is transferable proof of authorship. | read other principals' mail or claims. Re-address a message it received to a third party. |
| A compromised node or station | everything sealed to that principal, including stored envelopes (no forward secrecy), its outbox and spool | send as itself. Publish a rotated key with a valid continuity signature until revoked. | messages from other principals, or a key for another principal |
| Whoever steals a principal's signing key | what that key can sign | sign mail with any `created_at` until the revocation reaches each station | have mail received after `compromised_from` read as history, or have any mail from the key confer authority once revoked (D3) |
| An agent with shell access as the same OS user | everything that user can read, including private keys and pin stores | accept a key or hub identity by editing local state or running the CLI | nothing the OS user could not do. Only OS separation (D3) stops it. |
| Anyone who can read a transcript holding an invite token | the token | redeem it before the invitee does, and keep redeeming it while uses remain (`--max-uses`), each use a separate principal the inviter must still verify | redeem a token whose uses are exhausted, or that has expired or been revoked. Take over an admitted join attempt, whose retry needs that attempt's principal token and key record. |
| A future quantum adversary holding harvested envelopes | stored ciphertext | forge Ed25519 signatures on archived mail | decrypt `bm1` envelopes while ML-KEM-768 holds |

End-to-end means "to the process that holds the private key": the MCP station
or the CLI on the recipient's machine. A hosted agent connected to that
station through a connector (Dot, or Duet under #1943) receives plaintext, and
so does its platform.

Out of scope: hiding who talks to whom from the hub, deniable authentication,
forward secrecy, post-quantum signatures (the `bm2` path in D2), and OS-level
separation of keys from agents (D3).

## Decisions on the open questions

Recorded from the operator's decisions on #2005, #2007 and the PR, and the
conductor's decisions for revisions 2 to 4. The operator delegated
best-practice calls to the conductor.

1. Dependency (G1): approved. `brigade-cli[mesh]` on pyca `cryptography`,
   originally floor 48. Current source and CI use 50.0.2. Keep that floor
   pending source-backed platform review, with the old Intel guidance held (D1).
2. Retention: ciphertext cleared 7 days after ack, `content_expired`
   tombstones, a hard unacked TTL. A malicious hub cannot be forced to delete
   copies (D8).
3. Key changes: fail closed everywhere. Acceptance takes an operator-supplied
   fingerprint or a signed manifest, never what was fetched (D3).
4. No-hub relay: an explicit choice at the planned `mesh init`, no default
   (D10).
5. Native engine (G2): approved, staged as a bounded spike, then a written
   protocol, then the production daemon after measured results. iroh gives
   transport, not durable mailboxes (D10).
6. Teammate grants: symmetric, enforced in one transaction, tested for
   revocation and agent-to-agent reciprocity (D9).
7. Hub Lite claims: deferred until messaging, invites, presence and the
   free-plan measurements pass. This narrows #2008's scope (D12).
8. Own-fleet `observed` keys: allowed only for principals in an
   operator-signed membership manifest. Every other principal needs a verified
   binding, and hub-provided roles never select the weaker path. The path is
   labeled opportunistic confidentiality (D3, D13).
9. Revoked keys: notification order is not evidence. History stays readable
   only with a local receipt strictly earlier than `compromised_from` minus
   the 10 minute relative clock error and the 1 second timestamp precision,
   and never confers authority (D3).
10. Operator questions on Hub Lite and in no-hub mode: `hub_feature_unavailable`
    in version 1. Mesh mode never falls back to a configured hub (D11).
11. `bm1` wire profile: frozen to the pinned draft revisions. A behavior change
    needs a new suite id (D1).
12. G4: `wrangler` as a pinned development dependency of `workers/hub-lite/`
    only, with a committed lockfile, approved. It is not a runtime dependency
    of `brigade-cli`.
13. Membership removal: the current accepted membership manifest gates every
    use of an `observed` key. A removed member keeps its pin and history and
    is blocked as `observed_not_member` until it is verified or listed again
    (D3).
14. Confidentiality labels: computed per envelope from the key it was sealed
    to, recorded at seal time for sent mail, and separate from signer
    authentication (D6).

## Revision 5 review disposition and rollout boundary

The four foundation reviews were read as claims and compared with current
crypto, key-directory, identity and sealed-storage source at `c32ad821`. The
parent's synthetic probe receipt `20261027-190546-work-verify-2cda0a` reproduced
the named storage behaviors. This receipt establishes those probes, not client
or live readiness. Earlier round mappings in the plan remain intact.

| Review finding | Revision 5 disposition | Design / plan slices |
|---|---|---|
| Astra 2, Opus C: statement bytes and missing manifest public key | Exact schemas, full signed digest, explicit key file plus independent fingerprint, shared fixtures proposed | D3 / 2, 2b, 11 |
| Astra 3/5, Opus A/B, Fable client contracts: migration and trust precedence | Depend on 2, preserve immutable history/marks/floors atomically, fresh keypairs, new signed membership, use refusal independent of provenance | D3, D4 / 2, 2b, 5, 6 |
| Daybreak first-contact rollback, Opus C: origin and bootstrap | Exact normalized signed origin, id provenance distinct from seq/digest policy freshness, withholding limits | D3, D4 / 2, 2b, 10 |
| Astra 4/7, Opus D/E, Daybreak hub namespace | Original attempt recovery before new send, exact publication reconciliation, scoped lookup and fence prerequisites | D5, D8 / 3, 4, 6 |
| Astra 6: revoked tombstone | Full-envelope and verified signed-header receipt digests, monotonic replay transition, no inherited authority | D3, D6 / 5 |
| Opus retention, Daybreak routing expiry | Preserve 1..3650 routing configuration, fixed sealed freshness, proposed separate bounded retry receipts. Parent must decide metadata retention versus shorter retry guarantee | D6, D8, D12 / 4, 6, 11, 12c |
| Fable 1/2/3/4/5, Opus minor 1/2/4 | Keep compromise default, add routine token rotation, refuse plaintext sealed-parent replies, clear unrelated migration, enforce origin/hub and monotone identity. Staged activation remains a proposal | D3, D4, D5, D8 / 3, 4, 6, 7 |
| Fable 6: retained reply hint | Retain intentional `sealed_reply_to` after parent expiry. Signed triple governs linkage | D5 / 4, 5 |
| Opus dependency request | Do not restore 48. Retain 50.0.2 floor and mark Intel route stale pending platform evidence | D1 / 1, 7 |
| Opus sender-view limitation | Document bm1 limit, defer multi-recipient/new commitment to separately reviewed suite | D2 / future bm2 |
| Daybreak operator authority and all reviewers' rollout findings | Questions disabled under require-sealed until 7b, labeled Hub-trusted elsewhere. Require opening, sealing, renderers, docs and adversarial/mixed-version evidence together | D7, this section / 5, 6, 7, 7b, 11 |

The first sealed-mail milestone requires slices 2, 2b, 5, 6 and 7, closed Hub
prerequisites, shared parser/statement fixtures, adversarial retry/storage tests,
visible labels on every projection and explicit mixed-version handling. Opening
and sealing ship in the same client release. With required sealing enabled,
legacy clients cannot send or reply in plaintext, questions remain disabled
until 7b, and old clients must be upgraded or kept off sealed mail surfaces.
Do not treat their blank-row rendering or ack as successful receipt/opening.
The deployment checklist inventories every station's opener/sealer version,
exercises a legacy client against the updated Hub, and proves refusal of
plaintext replies to full and tombstoned parents even before global enforcement.

Merging foundations, accepting this revision or passing a round trip does not
deploy that milestone. Runtime policy activation is separately authorized
operator work after the transition proposal and integrated gates are reviewed.
A release claiming hub-independent operator authority also requires 7b,
its plaintext-data migration and adversarial answer verification. Teammate
invites, Hub Lite and no-hub retain their separate existing milestones.

## Sources

All retrieved on 2026-10-22.

- [PY] Python 3.14 Cryptographic Services: https://docs.python.org/3.14/library/crypto.html
- [CRY-PYPI] cryptography on PyPI: https://pypi.org/pypi/cryptography/json
- [CRY-CHG] cryptography changelog (47.0.0 adds HPKE, 48.0.0 adds ML-KEM and ML-DSA to the wheels, 49.0.0 removes x86_64 macOS wheels): https://cryptography.io/en/latest/changelog/
- [CRY-HPKE] cryptography 50.0.2 HPKE API, including `KEM.MLKEM768_X25519`: https://cryptography.io/en/stable/hazmat/primitives/hpke/
- [CRY-MEAS] Local measurement on 2026-10-22 with the cryptography 48.0.0 Linux x86_64 wheel: `Suite(KEM.MLKEM768_X25519, KDF.HKDF_SHA256, AEAD.AES_256_GCM).encrypt` of a 32 byte key returned 1168 bytes and decrypted. The same call under `KEM.X25519` returned 80 bytes.
- [CRY-INST] cryptography installation: https://cryptography.io/en/latest/installation/
- [CRY-MLKEM] cryptography 50.0.2 ML-KEM API (`from_seed_bytes`, `decapsulate`, added in 47.0.0): https://cryptography.io/en/stable/hazmat/primitives/asymmetric/mlkem/
- [XWING] draft-connolly-cfrg-xwing-kem revision 11, last updated 2026-09-23, Independent Submission stream (sections 5.2 key generation, 5.3 combiner, 5.6 use in HPKE, 7 IANA: `0x647a`, Nenc 1120, Npk 1216, Nsk 32, Appendix C test vectors): https://datatracker.ietf.org/doc/draft-connolly-cfrg-xwing-kem/11/
- [HPKE-PQ] draft-ietf-hpke-pq revision 05, last updated 2026-07-06, HPKE working group document (section 4 hybrid KEMs, section 8.2 `0x647a` MLKEM768-X25519 with Nenc 1120 and Npk 1216, Appendix A.5 vectors with ChaCha20-Poly1305): https://datatracker.ietf.org/doc/draft-ietf-hpke-pq/05/
- [HPKE-PQ-TV] HPKE PQ JSON test vectors (13 vectors, `0x647a` only with AEAD `0x0003`, and vector encryptions use associated data such as `Count-0`): https://raw.githubusercontent.com/hpkewg/hpke-pq/main/test-vectors.json
- [ASTRA-MEAS] Round 2 review of this PR at e13e0a6 (GPT Astra): a 20 KiB padded payload sealed into a 31,639 byte envelope. Taken from the review. Revision 3 did not repeat the measurement.
- [NACL-PYPI] PyNaCl on PyPI: https://pypi.org/pypi/PyNaCl/json
- [NACL-CHG] PyNaCl changelog: https://pynacl.readthedocs.io/en/latest/changelog/
- [RN-README] Reason Network README at v0.12.9: https://github.com/reason-machines/reason-network/blob/039b9571dab11f980813918b60a827b8bb27fbfd/README.md
- [RN-DES] Reason Network DESIGN.md at v0.12.9: https://github.com/reason-machines/reason-network/blob/039b9571dab11f980813918b60a827b8bb27fbfd/DESIGN.md
- [RN-NET] Reason Network `src/net.rs` at v0.12.9 (ALPNs and `PROTO` at lines 32 to 34, `Wire` at 49 to 55, the protocol check at 1035 to 1040): https://github.com/reason-machines/reason-network/blob/039b9571dab11f980813918b60a827b8bb27fbfd/src/net.rs
- [RN-PROTO] Reason Network `src/proto.rs` at v0.12.9 (signed approvals at lines 291 and 325 to 327): https://github.com/reason-machines/reason-network/blob/039b9571dab11f980813918b60a827b8bb27fbfd/src/proto.rs
- [RN-REL] Reason Network releases: https://github.com/reason-machines/reason-network/releases
- [IROH-REL] iroh releases: https://github.com/n0-computer/iroh/releases
- [IROH-RLY] iroh-relay README, access control: https://github.com/n0-computer/iroh/blob/main/iroh-relay/README.md
- [IROH-CON] iroh relays concept page: https://docs.iroh.computer/concepts/relays
- [IROH-PUB] iroh public relays: https://docs.iroh.computer/iroh-services/relays/public
- [IROH-LKP] iroh address lookup: https://docs.iroh.computer/concepts/address-lookup
- [IROH-PY] iroh Python package: https://pypi.org/pypi/iroh/json
- [CF-WLIM] Workers limits: https://developers.cloudflare.com/workers/platform/limits/
- [CF-WPRC] Workers pricing: https://developers.cloudflare.com/workers/platform/pricing/
- [CF-DOPRC] Durable Objects pricing: https://developers.cloudflare.com/durable-objects/platform/pricing/
- [CF-DOLIM] Durable Objects limits: https://developers.cloudflare.com/durable-objects/platform/limits/
- [CF-DOWS] Durable Objects WebSockets and hibernation: https://developers.cloudflare.com/durable-objects/best-practices/websockets/
- [CF-WC] Workers Web Crypto: https://developers.cloudflare.com/workers/runtime-apis/web-crypto/
- [CF-DEPLOY] Deploy to Cloudflare buttons: https://developers.cloudflare.com/workers/platform/deploy-buttons/
- [CF-ACCESS] Cloudflare Access for Workers: https://developers.cloudflare.com/workers/configuration/cloudflare-access/
- [STALLINGS] W. Stallings, Cryptography and Network Security, 8th edition, sections 12.7, 14.3 to 14.4, 15.3 and 16.4 (replay protection with timestamps and nonces, key distribution and separation, and signing the whole message to stop surreptitious forwarding). Used for the replay, signature coverage and key separation requirements in D2 and D6.
