# Agent mesh: sealed Fleet Hub mail, teammate invites, no-hub mode and Hub Lite

Design for #2005, #2006, #2007 and #2008 (parent #1121). The implementation
plan is [phase-agent-mesh-plan.md](phase-agent-mesh-plan.md).

Operator direction: get end-to-end encryption working, let a user invite a
teammate's agents, and offer a no-hub option for people who do not want to run
or trust a hub. This document records the decisions and the reasons. It does
not change code.

Research for this design was retrieved on 2026-10-22. Sources are listed at the
end with their URLs. Claims the research could not confirm from a primary
source are marked UNVERIFIED.

## Decisions at a glance

| Topic | Decision |
|---|---|
| Crypto library | Optional extra `brigade-cli[mesh]` backed by pyca `cryptography>=47,<52`. Needs operator agreement on #2005 before slice 1 lands. |
| Envelope | Version 1 JSON envelope: a random content key encrypts the payload with AES-256-GCM, and HPKE base mode (X25519, HKDF-SHA256, AES-256-GCM) wraps that key for the recipient and for the sender. An Ed25519 signature covers the whole envelope. |
| Sealed fields | Subject, body, ref, thread and sender_label. |
| Routing fields | Sender, recipient, kind, priority, client_id, reply target, timestamps and padded size stay readable by the hub. |
| Keys | One Ed25519 signing key and one X25519 encryption key per principal, generated where its token lives, published with `keys_publish` and served by `who`. Clients pin keys. A change without a continuity signature fails closed. |
| Rotation | `brigade fleet keys rotate` signs the new key with the old one. Revoking a node or principal revokes its keys, and continuity from a revoked key is not accepted. |
| Open order | Check the routing header, verify the signature, unwrap and decrypt, check the replay cache, then fence. One choke point in the client message layer serves the CLI, MCP and live delivery. |
| Invites | `https://<hub>/join#bfi1.<hub-id>.<invite-id>.<secret>.<inviter-fingerprint>`, admin-created, one use by default, 24 hour default expiry. Teammates are `agent:` principals with role `teammate`, symmetric send grants and no claims, runs or events beyond their own inbox. Every invite and membership change is written to a hub audit log. |
| No-hub path | Path c (Brigade's own store-and-forward protocol, local inbox, sender outbox) carried over path a (an iroh transport shipped as an optional native engine). Path b (Reason Network interop) is rejected for now. |
| Tool contract | The seven fleet MCP tools keep their input schemas in every mode. Outputs gain structural fields (`sealed`, `signature`, `key`). Missing features return a fixed error code. |
| Hub Lite | One Worker and one SQLite-backed Durable Object per fleet. It serves the messaging, key, invite and presence subset of the hub API, then claims. It requires sealed mail. |
| Plaintext by design | Claims, presence, run events, grants, keys, the audit log and message routing metadata. |

## Current state

These facts come from the code on `main` at the time of writing.

- Messages live in the hub's `fleet_messages` table
  (`src/brigade/fleet_hub_messages.py`). `subject` and `body` are `TEXT NOT
  NULL` plaintext columns, next to `reference`, `thread`, `sender_label`,
  `kind`, `priority`, `client_id`, `reply_to` and the timestamps. Rows expire
  after `RETENTION_DAYS = 90`.
- `payload_hash` is SHA-256 over the plaintext recipient, subject, body,
  reference, priority, reply target and metadata. The hub uses it with the
  unique `(sender, client_id)` index to make sends idempotent: a retry with the
  same `client_id` and the same hash returns the stored row, and a different
  hash is a conflict.
- `reply()` builds the reply subject on the hub as `Re: <original subject>` and
  copies the original `ref` and `thread`. A spooled reply looks up the accepted
  message's subject, reference and thread on the hub.
- Principals are node ids (`nodes` table, `src/brigade/fleet_hub.py`) and
  `agent:<name>` message principals (`fleet_message_principals`). Both store a
  SHA-256 digest of a bearer token. Node token rotation is "revoke, then add
  again", which mints a new token.
- Send grants (`src/brigade/fleet_hub_message_grants.py`) restrict only
  `agent:` senders. `permits()` returns true for any sender id that does not
  start with `agent:`, so a new principal prefix would be unrestricted by
  default.
- `who` and `claims` (`_station_read`) let agent principals see nodes,
  principals, sessions and claims filtered by their grants.
- The client (`src/brigade/fleet_client_messages.py`) posts every message
  action to `POST /messages`, and spools sends and replies to a local JSONL
  file when the hub is unreachable. Retries reuse the same `client_id`.
- The MCP station (`src/brigade/fleet_mcp.py`) exposes `fleet_who`,
  `fleet_send`, `fleet_inbox`, `fleet_ack`, `fleet_reply`, `fleet_sent` and
  `fleet_claims`. `_frame()` moves every sender-chosen message field into one
  `wrap_untrusted` fence with the body. `_frame_directory()` fences every
  non-structural string in `who` and `claims` results.
- Live delivery (`src/brigade/fleet_inbox_delivery.py`) runs from a prompt or
  tool hook, makes one hub call with a 2 second deadline, shows at most three
  messages, and reuses `_frame()`. It must never raise.
- MCP Events (`docs/fleet-mcp-events.md`) already keep `message.created`
  payloads thin: ids, sender, recipient, priority, kind and timestamps, never
  subject or body.
- Dashboard enrollment codes (`src/brigade/fleet_hub_enrollment.py`) are
  random, stored as digests, expire after 300 seconds, and are consumed under
  `BEGIN IMMEDIATE`. Invites reuse that pattern.
- The Dot connector's Cloudflare Access check (`src/brigade/dot/access_auth.py`)
  already imports `cryptography` and `jwt` lazily and raises
  `OptionalDependencyError` when they are missing. The `grokbot` extra already
  depends on `pyjwt[crypto]`, which pulls in `cryptography`.
- `workers/github-webhook-relay/` is the only Worker in the repo. It uses D1,
  plain JavaScript and `node:test` with hand-written fakes, and no CI job runs
  its tests today.

## D1. Crypto library and the optional extra

Python's standard library has no X25519, Ed25519, AEAD or HKDF. The 3.14
"Cryptographic Services" chapter lists only `hashlib`, `hmac` and `secrets`
[PY]. Brigade keeps zero runtime dependencies, so sealing ships as an extra:

```toml
[project.optional-dependencies]
mesh = ["cryptography>=47,<52"]
```

Comparison as of 2026-10-22 [CRY-PYPI] [CRY-CHG] [NACL-PYPI] [NACL-CHG]:

| | pyca `cryptography` 50.0.2 | PyNaCl 1.6.2 |
|---|---|---|
| Released | 2026-09-30, 11 releases in 12 months | 2026-01-01, 2 releases in 12 months, 3.7 year gap before 1.6.0 |
| X25519, Ed25519, HKDF | yes, yes, yes | yes, yes, no HKDF |
| AEAD | AES-GCM, ChaCha20-Poly1305, no XChaCha20 | XChaCha20-Poly1305 (`nacl.secret.Aead`), AES-GCM bindings |
| HPKE (RFC 9180) | yes, `cryptography.hazmat.primitives.hpke`, added in 47.0.0, one-shot base mode | no. `SealedBox` is anonymous with no sender authentication |
| Linux wheels | manylinux and musllinux, x86_64 and aarch64 | same |
| macOS wheels | arm64 only since 49.0.0. 48.0.0 still shipped `universal2` | `universal2` |
| Windows wheels | amd64 only (arm64 dropped in 46.0.4, 32-bit in 49.0.0) | amd64, arm64, win32 (arm64 removal is on `main`, unreleased) |
| Wheel size (manylinux x86_64) | about 4.8 MB, 16 MB installed | about 1.4 MB, 4.2 MB installed |
| Already in Brigade's dependency tree | yes, through the `grokbot` extra and the Dot Access check | no |

Recommendation: `cryptography`.

- It is the only one with HPKE, so Brigade does not hand-assemble a KEM, KDF
  and AEAD. PyNaCl would need a local HKDF and a hand-built sender
  authentication scheme on top of `SealedBox`.
- It ships fixes on a monthly cadence and statically links OpenSSL in its
  wheels.
- Brigade already depends on it in one extra and imports it in the Dot pack, so
  maintainers and users already carry it.

Costs, stated so the operator can weigh them:

- Intel Macs get no wheel for 49.0.0 and later. With the `>=47` floor, pip
  still tries to build the newest sdist (Rust, OpenSSL, libffi). The install
  docs will give Intel Mac users
  `pip install "brigade-cli[mesh]" --only-binary cryptography`, which resolves
  to 48.0.x, the last `universal2` release that has HPKE.
- Windows arm64 has no wheel, so a Rust toolchain is required there.
- The download is about 3.4 times larger than PyNaCl.

The upper bound `<52` follows the project's two majors in 2026. CI tests the
floor (47) and the newest release.

Without the extra, nothing changes for plain-text mail. Sealed mail shows up as
a row with `sealed: true` and `open_error: "mesh_extra_missing"` and no body.
Sending to a hub that requires sealing fails with `message_sealing_required`.

AGENTS.md requires a conversation before adding a runtime dependency. An
optional extra is not a core runtime dependency, but this one changes what
users install, so slice 1 of the plan waits for the operator's agreement on
#2005.

## D2. Envelope format

A sealed message is one JSON object, stored by the hub as text and handed to
recipients unchanged. Binary values are unpadded base64url. Every header value
is an integer or an ASCII string checked by a fixed pattern, so the canonical
form (`json.dumps(..., sort_keys=True, separators=(",", ":"),
ensure_ascii=True)`) is the same in Python and JavaScript.

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
  "in_reply_to": {"from": "agent:dana.laptop", "client_id": "..."},
  "pad": 1024,
  "recipients": [
    {"kid": "k1_<recipient>", "wrap": "<enc || HPKE ciphertext of the content key>"},
    {"kid": "k1_<sender>", "wrap": "<same, sealed to the sender's own key>"}
  ],
  "ct": "<AES-256-GCM ciphertext of the padded payload>",
  "sig": "<Ed25519 signature>"
}
```

`hub` is the hub id (D6) or the literal `mesh` in no-hub mode. `in_reply_to` is
absent on a new message.

What is sealed. The payload holds `subject`, `body`, `ref`, `thread` and
`sender_label`, encoded as binary fields (a one byte tag, a four byte big-endian
length, then the UTF-8 bytes) rather than JSON, so quotes, backslashes and
non-ASCII text cost no escaping overhead. These are the sender-chosen fields `_frame()`
already fences, minus `client_id`, which the hub needs for idempotent retries.
The
payload is padded with one `0x80` byte and zeros up to a multiple of 1 KiB
(minimum 1 KiB), so the stored size reveals the length only to the nearest
kilobyte. `pad` records the bucket and is covered by the signature.

What stays readable. `from`, `to`, `kind`, `priority`, `client_id`,
`created_at`, `in_reply_to`, the key ids and the padded size. The hub needs
sender, recipient and `client_id` for authorization, quotas and idempotent
retries. `kind` (three values) and `priority` (four values) are fixed enums
the hub already uses in `message.created` events. `in_reply_to` names the original message by its
sender and `client_id`, which is the same in hub and no-hub mode. The hub keeps
its integer `reply_to` column as a routing field and checks it against
`in_reply_to`.

How it is built:

1. `header` is the envelope without `recipients`, `ct` and `sig`, plus a
   `rcpt` list of the recipient key ids in order. `hb` is the canonical bytes
   of `header`.
2. A fresh random 32 byte content key `cek` encrypts the padded payload with
   AES-256-GCM, a zero nonce and `hb` as associated data. A zero nonce is safe
   only because each content key encrypts exactly one message. Every seal,
   including a reseal after `recipient_key_stale`, draws a new `cek`. No code
   path may reuse a content key recovered from the sender's own wrap, because
   the same key and nonce under different associated data would expose the
   GCM authentication key. A test asserts that two seals of the same input
   produce different `cek` values.
3. For each recipient key, `Suite(KEM.X25519, KDF.HKDF_SHA256,
   AEAD.AES_256_GCM).encrypt(cek, recipient_public_key, info)` with
   `info = b"brigade-mesh/v1/cek\0" + sha256(hb)`. The pyca HPKE API takes
   `info` but no associated data, so the header is bound through `info`
   [CRY-HPKE].
4. The sender signs `b"brigade-mesh/v1/sig\0" + canonical(envelope without
   sig)` with its Ed25519 key.

The sender always includes its own key as the second recipient. That costs
about 80 bytes and gives two things the current tools depend on: `fleet_sent`
can still show the sender its own message, and a spooled send can be retried
from the stored envelope without re-reading the plaintext.

Why this shape:

- HPKE base mode is anonymous, so the Ed25519 signature authenticates the
  sender. If an attacker strips the signature and signs the same ciphertext
  under their own name, `from` changes, `hb` changes, and the recipient's HPKE
  open fails because `info` no longer matches. A sender therefore cannot claim
  a message it could not read.
- The hub can check `from`, `to`, `from_kid` and the recipient kid against its
  own records with string comparisons, so the Python hub needs no crypto
  library to enforce routing.
- AES-GCM instead of ChaCha20-Poly1305 because Workers WebCrypto implements
  X25519, HKDF and AES-GCM but not ChaCha20 [CF-WC]. A future browser or Worker
  client (the desktop panel in #1887 slice 6) can then open mail with
  WebCrypto alone. Hub Lite itself never decrypts.
- A whole envelope sealed per recipient would double a 64 KiB body. Wrapping a
  content key adds about 80 bytes per recipient and leaves room for
  multi-recipient mail later.

Versioning. `v` is the envelope version and `suite` names the exact primitives.
A reader refuses an unknown `v` or `suite` with `open_error:
"envelope_unsupported"` and never guesses. The hub stores unknown versions it
can route, because routing does not depend on the sealed part, but a hub
running with `require_sealed` accepts only versions listed in its
`/mesh/identity` response. A new suite (for example an ML-KEM hybrid) gets a
new `suite` value, and keys advertise which suites they accept.

Limits. The plaintext limits stay as they are: 64 KiB for the body
(`BODY_LIMIT`), 200 bytes for the subject, 256 for `ref`, plus the thread and
label patterns. With binary fields the largest payload is about 66 KiB, padded
to 66 KiB, which base64url turns into about 88 KiB. The hub caps a stored
envelope at 96 KiB, which holds the largest legal message with room for the
header and two wraps. The client checks the encoded size before sending and
fails with `message_too_large` if a future field change breaks that margin. The list page byte cap (`MAX_LIST_RESPONSE_BYTES`,
7 MiB) already cuts pages by serialized size, so 100 large envelopes split
across pages without a protocol change.

## D3. Keys: generation, publication, pinning, rotation and revocation

Each principal (a node id or an `agent:` principal) has one active key record:

```json
{
  "v": 1,
  "principal": "agent:dana.laptop",
  "hub": "h_3q9GZ2pX8vN0aT1kLm4w",
  "kid": "k1_...",
  "sig_pub": "<Ed25519 public key>",
  "enc_pub": "<X25519 public key>",
  "suites": ["bm1"],
  "transport": {"iroh": "<endpoint id>"},
  "created_at": "2026-10-22T12:00:00+00:00",
  "prev_kid": "k1_...",
  "self_sig": "<signature by sig_pub>",
  "prev_sig": "<signature by the previous key>"
}
```

`kid` is `k1_` plus the first 16 bytes of `sha256(b"brigade-mesh/v1/kid\0" +
sig_pub + enc_pub)` in base64url. The fingerprint is the full 32 byte SHA-256
of `sig_pub`. People see it as 52 base32 characters in groups of four, and
invite links carry its first 16 bytes. Those are the only two forms.

`self_sig` and `prev_sig` both sign `b"brigade-mesh/v1/key\0" +
canonical(record without self_sig and prev_sig)`, so they cover `v`,
`principal`, `hub`, `kid`, both public keys, `suites`, `transport`,
`created_at` and `prev_kid`. A record therefore cannot be replayed onto
another principal or hub, and its suites cannot be downgraded. A fingerprint
names only `sig_pub`, so every check that accepts a key by fingerprint (pin,
invite, `keys accept`) also verifies `self_sig`, which is what authenticates
`enc_pub`. `transport` appears only in no-hub mode.

Generation. Keys are generated where the principal's token lives, the first
time the client needs them (`brigade fleet keys init`, or automatically on the
first sealed send or open). They are stored in
`<brigade_home>/fleet/mesh-keys/<hub-id>-<principal-hash>.json`, written with
the same owner-only, no-symlink rules `read_message_token_file` applies to
token files. `--mesh-key-file` overrides the path, next to
`--principal-token-file`. Private keys never leave the machine.

Publication. A new `keys_publish` action on `POST /messages` takes a key
record. The hub requires that the record's `principal` equals the
authenticated caller and checks formats. If the caller already has an active
key, the new record must carry `prev_kid` equal to it and a `prev_sig`. The hub
cannot verify signatures without a crypto library, so it checks presence and
format, and clients verify. This rule stops a second machine holding the same
agent token from replacing the first machine's key by accident: it gets
`principal_key_exists` and the operator either copies the key file or rotates
deliberately. It does not stop a deliberate replacement. Anyone holding the
token can publish a record with a junk `prev_sig`. Peers then see `changed`,
because they verify `prev_sig`, and the original machine's next send fails the
hub's `from_kid` check, which the client reports as `local_key_superseded`.

Serving. `who` adds a `keys` entry for each visible node and principal with its
active record. A `keys_get` action returns retired records by kid so a client
can verify signatures on older mail. The MCP `fleet_who` tool does not hand raw
keys to agents. It shows `key: {"fingerprint": "...", "status":
"pinned|new|changed|none"}`, and both values come from fixed formats, so they
are structural fields rather than fenced text.

Pinning. The client keeps a pin file per hub:
`principal -> {kid, fingerprint, source, pinned_at}` with `source` one of
`tofu`, `invite` or `manual`. The first key seen for a principal is pinned on
first use. A later record is accepted automatically only when its `prev_kid`
is the pinned kid, its `prev_sig` verifies with the pinned key, and the pinned
key was retired rather than revoked. Retired versus revoked is the hub's
statement, so a hub colluding with someone who stole a retired key can pass
off a revoked key as retired. In no-hub mode there is no revocation authority
at all, so a user stops trusting a key there by removing the contact. Any other change sets status `changed`.
Sending to that principal then fails with `recipient_key_changed`, and mail
signed by the new key opens as a failed row with `open_error:
"sender_key_changed"`. `brigade fleet keys accept <principal> --fingerprint <fp>` is the only
way past it. It is a CLI command for a person. No MCP tool can change a pin,
so a prompt-injected agent cannot accept a substituted key.

Downgrade. Once a principal is pinned, the client never sends it plain text.
If the hub stops serving that principal's key, the send fails with
`recipient_key_missing`. Receiving works the same way: a plain-text row whose
sender is pinned opens as a failed row with `open_error:
"unsealed_from_pinned"`. Otherwise a hub could insert an unsigned row under a
pinned peer's name, and `sender` is printed outside the fence. A row that
carries an `envelope` never uses hub-supplied `subject`, `body`, `ref`,
`thread` or `sender_label`, even if the hub fills them in.

In `auto` mode the hub can still force plain text toward a principal the
client has never pinned, by not serving that principal's key. `always` mode,
or a hub with `require_sealed`, closes that gap.

Rotation. `brigade fleet keys rotate` creates a new record signed by the old
key and publishes it. The old private key is kept, marked retired, so mail
sealed to it can still be opened. `brigade fleet keys prune` deletes retired
private keys after the hub's 90 day retention has passed.

Revocation and token rotation. Revoking a node (`revoke_node`) or a principal
(`revoke_principal`) marks all its key records revoked in the same
transaction. A revoked kid can never be published again. Because node token
rotation is "revoke, then add", the rotated node must publish a fresh key, and
peers do not accept continuity from a revoked key, since revocation may mean
the old key was stolen. Peers see `changed` and re-verify. After a planned
fleet-wide rotation the operator runs `brigade fleet keys accept --all-changed
--hub <url>` once per machine, which prints every fingerprint it accepts.

Losing a key loses only the mail sealed to it. The hub keeps the routing row,
and the owner can still ack it.

## D4. Sealing on send, reply and the spool

`fleet_client_messages.send()` and `reply()` decide whether to seal from three
inputs: the hub's policy from `GET /mesh/identity` (cached per hub), the local
policy `BRIGADE_FLEET_MESSAGES_SEAL=auto|always|never` (default `auto`), and
whether the recipient has a key.

| Hub policy | Local policy | Recipient key | Result |
|---|---|---|---|
| any | `never` | any | plain text, refused by a `require_sealed` hub |
| plain allowed | `auto` | none, not pinned | plain text |
| plain allowed | `auto` | present or pinned | sealed |
| any | `always` | none | `recipient_key_missing` |
| `require_sealed` | `auto` | none | `recipient_key_missing` |
| any | any, extra missing, sealing needed | n/a | `mesh_extra_missing` |

Spool. The hub deduplicates on `(sender, client_id)` and compares
`payload_hash`. For sealed rows the hash is SHA-256 over the stored envelope
bytes, never over plaintext, because a plaintext hash would let the hub confirm
guesses about short bodies. Every retry of a sealed send must therefore send
byte-identical envelopes. The client seals once, writes the sealed form into
the spool record under the spool lock, and only then transmits. If the
recipient's key is unknown while offline, the record is spooled as plaintext
with `seal: "pending"` (the spool is already a local owner-only file) and
sealed at drain time, again persisting the sealed form before the first
transmission. A definitive `recipient_key_stale` or `sender_key_stale` (409)
means the hub stored nothing, so the client reseals with a fresh content key
and replaces the spool record. `_drain_locked` drops any 4xx it does not
recognize today, so both codes must be added to its retained-and-reseal set,
or rotating a key would silently delete spooled mail.

The hub runs its `(sender, client_id)` duplicate check before the key id
checks, so a retry of an envelope the hub already stored still returns the
stored row after a rotation.

Callers with deterministic client ids. Patrol (`runs_patrol_actions.py`) and
the Dot pack (`dot/hub_inbox.py`) derive `client_id` from their own state and
resend after a crash. A new seal of the same content produces different bytes,
so the hub answers with a conflict. On a conflict for a sealed send, the
client fetches its stored row with `sent` by id, opens the sender wrap, and
treats the send as delivered when the plaintext fields and routing fields
match. Only a real difference is reported as a conflict.

Reply. The hub cannot build `Re: <subject>` from ciphertext. For a sealed
original the client opens it, builds the reply subject and thread locally, and
sends a normal sealed message with `reply_to` set. The hub validates `reply_to`
as it does today and checks that the original row's sender and `client_id`
match the envelope's signed `in_reply_to`. The spooled-reply path that reads
the accepted message's subject from the hub is replaced, for sealed rows, by
the subject and thread already inside the spooled envelope.

A reply written while the hub is unreachable cannot be sealed yet when the
original has not been opened on this machine, because sealing needs the
original's sender, `client_id` and subject. Such a reply is spooled as
`seal: "pending"` with the original's hub message id. At drain time the
client fetches and opens the original (as `_reply_recipient` already fetches
it to find the recipient) and seals the reply before the first transmission.

Sender label. The hub refuses `sender_label` from `agent:` senders today. Once
the label is sealed, the hub cannot see it, so the opening client drops
`sender_label` from any message whose sender starts with `agent:`.

## D5. Opening mail: verify, decrypt, then fence

Opening happens in one place, a new `open_rows()` called by
`fleet_client_messages.list_messages()`, `sent()`, `ack()`, and on the row
returned by `send()` and `reply()`, after the page is validated. Every consumer (the CLI, the MCP station and the live delivery hook)
gets opened rows, and `_frame()` keeps running last. `list_messages()` takes an
`open_limit` so the delivery hook opens only the rows it will show. Rows past
the limit come back in the failed-row shape described below, with
`open_error: "not_opened"`.

For each sealed row, in order:

1. Parse the envelope with size and pattern checks. Reject an unknown `v` or
   `suite`.
2. Check that the hub's `sender` and `recipient` columns equal the envelope's
   `from` and `to`, that the caller is `to` (or `from`, for `fleet_sent`), and
   that the envelope's `hub` equals the cached `/mesh/identity` hub id. This
   stops a hub from relabeling who sent a message or replaying mail from
   another hub.
3. Look up the sender's key by `from_kid` (pin file, then `who`, then
   `keys_get`), check it against the pin, and verify `sig`.
4. Find the caller's wrap by kid, open it with HPKE, decrypt `ct` with `hb` as
   associated data, strip padding, and parse the payload with the existing
   field limits (`SUBJECT_LIMIT`, `BODY_LIMIT`, `REF_LIMIT`, the thread and
   label patterns).
5. Check freshness and replay. An envelope whose signed `created_at` is older
   than the 90 day retention, or more than five minutes in the future
   (`MAX_FUTURE_SKEW`), opens as `stale`. A local map from `(from, client_id)`
   to the first hub message id seen is kept for the same window. The same pair
   under a different hub message id opens as `replayed`. The cache is per
   machine, so a fresh install or a second station starts empty and cannot
   catch a replay inside the window. That residual is listed in the threat
   model.
6. Take `kind`, `priority` and `created_at` (as `sent_at`) from the verified
   header instead of the hub's columns. If the hub's `reply_to` is set, check
   that the referenced row's sender and `client_id` match the signed
   `in_reply_to`, and otherwise open as `reply_mismatch`. Dot matches replies on
   `reply_to`, so this check is what makes the reply link trustworthy.
7. Return a row with the same keys as a plain-text row (`subject`, `body`,
   `ref`, `thread`, `sender_label`, ...) plus `sealed: true` and `signature:
   "verified"`.

Every row, sealed or not, also gets its structural fields pattern-checked:
integer ids, ISO timestamps, principal ids matching the node and `agent:`
patterns, and `kind` and `priority` from their fixed sets. A row that fails is
dropped from the page with a logged count. `_validate_page` checks only ids
today, and once the hub is treated as an adversary, those fields are hub text
shown outside the fence.

`_frame()` then fences every sender-chosen field exactly as it does for plain
text. Encryption says who wrote the text and that nobody changed it. It says
nothing about whether the text is safe, so a verified teammate message is
fenced like any other peer message.

A row that fails any step keeps only structural fields (`id`, `sender`,
`recipient`, `kind`, `priority`, timestamps, `acked_at`, `acked_by`), drops
`client_id` and every other sender-chosen string, and adds `sealed: true`, an
`open_error` code and a body that Brigade writes itself: `[sealed message not
opened: <code>]`. The fixed body keeps existing consumers working.
`fleet_inbox_delivery.render()` indexes `framed["body"]` after the cursor is
saved, and the Dot pack reads `row["body"]`, so a missing body would lose shown
rows or raise. A test asserts that no sender-chosen string appears outside the
fence on a failed row. `sealed`,
`signature`, `open_error` and `key` join `_MESSAGE_STRUCTURAL_FIELDS`, with
values drawn from fixed sets.

Live delivery. The hook opens only the rows it shows (at most three) and
imports `cryptography` lazily inside the existing `try`. It uses only pinned
and locally cached keys and makes no `who` or `keys_get` call, so its one hub
call and 2 second deadline stay as they are. A message from a sender whose key
is not cached shows the fixed body with `sender_key_unknown`, and the next
`fleet_inbox` call resolves the key. Delivery still never acks.

Ack. `fleet_ack` acks by hub id and does not need the plaintext. It returns the
opened row when the caller can open it.

MCP Events. `message.created` payloads already carry no subject or body. No
change.

`ask_user` (#1887 slice 4, not built yet). When it falls back to the operator's
hub inbox, the question goes out as a sealed message of kind `question`, and
the answer is a sealed, signed reply. The signature gives the same "this person
answered" property that Reason Network gets from its signed approvals [RN-DES].

## D6. Hub storage and policy

Schema changes on the Python hub. Each plan slice that changes the schema
bumps `SCHEMA_VERSION` or registers its columns as additive, as the startup
gate in `fleet_hub.py` requires:

- `fleet_messages` gains `envelope TEXT` and `envelope_bytes INTEGER`. Sealed
  rows store `subject = ''` and `body = ''`, keep `reference`, `thread` and
  `sender_label` NULL, and set `payload_hash` to the envelope digest.
- `fleet_message_keys(principal_id, kid, record TEXT, state, created_at,
  retired_at, revoked_at)` with one active key per principal enforced by a
  partial unique index.
- `hub_meta(key, value)` holds `hub_id`, 16 random bytes generated on first
  start, and `require_sealed`.

`GET /mesh/identity` (no authentication) returns `{"hub_id", "require_sealed",
"envelope_versions": [1]}`. The hub id is not a secret. It lets clients bind
key records and invites to one hub.

For a sealed send the hub derives `to`, `kind`, `priority` and `client_id` from
the envelope header, so there is one source for each. It refuses a send when
`from` is not the caller, when `from_kid` is not the caller's active kid, or
when the first recipient kid is not the recipient's active kid
(`recipient_key_stale`, 409, nothing stored). Quotas, grants and recipient
checks are unchanged.

Policy. `brigade fleet serve --require-sealed` (or
`BRIGADE_FLEET_MESSAGES_REQUIRE_SEALED=1`) refuses plain-text sends with
`message_sealing_required`. Plain-text rows already stored stay readable. Hub
Lite always requires sealing.

Retention. Proposed default: the hub clears `envelope` seven days after a
sealed message is acked and keeps the routing row until the 90 day retention.
Static keys give no forward secrecy, so a key stolen later opens every stored
envelope sealed to it. Shorter storage limits that exposure. This is an open
question below because it changes what `fleet_sent` can show after a week.

## D7. Invites and teammate principals

Link format:

```
https://hub.example/join#bfi1.<hub_id>.<invite_id>.<secret>.<inviter_fingerprint>
```

- `hub_id`: the hub's public identity from `/mesh/identity` (D6).
- `invite_id`: 16 random bytes, base64url. Used to look the invite up.
- `secret`: 32 random bytes, base64url. Never sent to the hub as is.
- `inviter_fingerprint`: the first 16 bytes of the SHA-256 of the inviting
  principal's signing key (D3).

The secret sits in the URL fragment, which browsers do not send to servers, so
clicking the link by mistake does not put it in an access log. `GET /join`
serves a static page that tells the reader to run `brigade fleet join
'<link>'`. The page does not read the fragment.

Two keys come from the secret with HKDF-SHA256:
`redeem_key = HKDF(secret, info="brigade-invite/v1/redeem")` and
`bind_key = HKDF(secret, info="brigade-invite/v1/bind")`. At creation the CLI
sends the hub only `sha256(redeem_key)`. The hub never sees `bind_key`.

Create, list and revoke. `brigade fleet invite create --role teammate --expires
24h [--max-uses 1] [--as <principal>] [--grant <pattern>...] [--name
<person>]` needs the admin token, the same authority that enrolls nodes and
principals today. `--as` names the inviting principal (default: this node),
and its private key must be on the machine running the command. The command
therefore runs on the inviting station with the admin token supplied by file.
That is what puts the fingerprint, `bind_key` and the joiner's pin where the
teammate's mail will be opened. Taking the fingerprint from `who` instead
would fall back to trusting the hub.
Expiry defaults to 24 hours, with a maximum of 7 days. The CLI keeps `bind_key`
in an owner-only local invite record until the invite is used or expires.
`invite list` shows state and uses. `invite revoke` ends an unused invite.
`brigade fleet members remove <principal>` revokes a joined principal and its
keys. Node principals cannot create invites, so a compromised node cannot mint
identities.

Join. `brigade fleet join '<link>' [--name <device>]`:

1. Fetches `/mesh/identity` from the link's origin and stops if the hub id
   differs from the one in the link. The hub id is public and proves nothing
   by itself. It keeps a link from being redeemed against a different hub at
   the same origin, and it is bound into the joiner's key record.
2. Generates the key pair locally and builds its key record.
3. Posts `POST /invites/redeem` with `invite_id`, `redeem_key`, the requested
   name, the key record and `binding = HMAC-SHA256(bind_key,
   canonical(key record))`.
4. The hub, under `BEGIN IMMEDIATE`, checks the invite exists, is unexpired,
   unrevoked and under its use limit, compares `sha256(redeem_key)` in
   constant time, creates the principal with a fresh token, stores the key and
   the binding, records the use and writes an audit row. Any failure returns
   one generic refusal.
5. The response carries the token, the principal id, the inviter's principal
   id and key record, and the hub id. The joiner checks the inviter's key
   against the fingerprint in the link and pins it with source `invite`, then
   writes the token to an owner-only token file.

The inviting side verifies the binding the next time it sees the new principal
in `who` (or at `invite list`), using the `bind_key` it kept, and pins the new
key with source `invite`. Both sides now hold keys they got from the link
rather than from the hub, so a hub that swaps keys during the join is caught
by either side, as long as the link travelled over a channel the hub operator
does not control. A binding that fails to verify sets the joiner's status to
`changed`, makes `invite list` report `binding_mismatch`, and exits nonzero. It
does not pin anything.

Bootstrap. A fresh hub (Hub Lite in particular) has no principal with a key,
so the first invite has no inviter fingerprint. `brigade fleet invite create
--bootstrap --role node` emits a link whose fingerprint field is the literal
`none`. The first machine joins on trust in the link alone, and every later
invite is created from it.

Redemption is rate limited per source address and globally, and failed
attempts are counted per invite per hour so a guessing run cannot fill the
audit table.

Teammate principals. A teammate machine becomes an `agent:` principal, for
example `agent:dana.laptop`, with a new `role` column set to `teammate` and a
`person` column. The `agent:` prefix is deliberate: every existing grant check
already restricts `agent:` senders, and a new prefix would be treated as an
unrestricted node by `permits()`. The `agent:` prefix test is repeated in
`permits()`, `require_granted()`, `require_recipient_cap()`, `_station_read()`
and `fleet_hub_events.permits()`. The plan replaces all five with one helper
that looks the caller up and treats only enrolled node ids as unrestricted, so
a future prefix fails closed. Changing the pure `permits()` alone would not,
because it cannot see the `nodes` table.

Each machine gets its own principal, token and key. A teammate's laptop cannot
speak for the same teammate's desktop, and nobody can speak for another person
without that person's key.

Default teammate grant:

| Capability | Teammate default |
|---|---|
| `fleet_send`, `fleet_reply` | only to the inviting principal plus any `--grant` patterns |
| Receiving mail | only from principals in its grant list. Grants are symmetric for teammates, so other fleet agents cannot message a teammate unless the operator widens the grant. |
| `fleet_who` | principals and nodes in its grant, with key fingerprints. No session repo, branch or harness details. |
| `fleet_claims` | refused with `principal_role_forbidden` |
| MCP Events | `message.created` for its own inbox only. `claim.expired` and `run.failed` are refused. |
| `fleet_submit`, dispatch, runs, admin reads, invites | not available. Those need node or admin tokens already. |

Grants are widened with the existing `brigade fleet msg principals grants`
command.

Audit. A new append-only `fleet_audit_log(id, at, actor, action, subject,
detail)` records `invite.create`, `invite.redeem`, `invite.redeem_refused`
(aggregated), `invite.revoke`, `member.remove`, `grants.change`,
`key.publish`, `key.rotate` and `key.revoke`. `detail` holds ids, roles, grant
patterns and counts, never secrets, tokens or message content. `brigade fleet
audit list [--since]` reads it with the admin token.

Revocation stops delivery at once: a revoked token gets 401 on its next call,
and sends to a revoked principal already fail with `message_recipient_inactive`.

## D8. No-hub mode: the spike

#2007 asks for three paths to be compared.

### Path a: bridge the fleet tools to an iroh daemon shipped as a native engine

iroh 1.0 shipped on 2026-06-15, and 1.4.1 is current (2026-10-22) [IROH-REL].
It is dual licensed MIT or Apache-2.0, and `iroh-relay` ships as prebuilt
binaries for Linux, macOS and Windows on every release [IROH-REL]. A relay sees
encrypted QUIC plus "source and destination IP addresses, connection times, and
the amount of data transferred" [IROH-PUB]. A self-hosted relay can restrict
access to an allowlist of endpoint ids, a shared token or an HTTP callout
[IROH-RLY]. n0's public relays are free but rate limited and "suitable for
development and testing" [IROH-CON]. A paid shared relay tier starts at
$19/month [IROH-SHR]. Default address lookup publishes signed pkarr records
with each endpoint's home relay to n0's DNS, and mDNS is opt-in [IROH-LKP].
A Python binding exists (`iroh` 1.2.0 on PyPI, built from iroh-ffi) but is
marked Alpha and has no Intel Mac wheel listed [IROH-PY].

Strengths: real NAT traversal and LAN discovery, a stable 1.x library, and a
relay that can be self-hosted under the user's control.

Weaknesses: iroh is a transport. It has no mailbox, so offline delivery still
needs a store. Brigade would own a long-running networking daemon. The Python
binding is not mature enough to put in the core path.

### Path b: interoperate with Reason Network

Reason Network is the closest existing system: per-machine daemons on iroh, a
stdio MCP shim, invites, and an `ask_user` tool [RN-README] [RN-DES]. As read
on 2026-10-22:

- It was created on 2026-10-16 and is at v0.12.9, with five releases in about
  18 hours [RN-REL].
- There is no wire specification. The protocol is serde JSON frames on ALPN
  `tincan/1` with a signed team state, documented only by DESIGN.md and the
  source [RN-SRC].
- The iroh machine key is the identity and the roster-signing key. Individual
  messages are not signed, and only `ask_user` approvals are [RN-SRC].
- No content encryption exists beyond iroh's QUIC TLS. Confidentiality is
  machine to machine [RN-SRC].
- Default relays are operated by Reason, and without a team relay each machine
  still publishes its address record to n0's DNS [RN-DES].
- Offline mail waits in the sender's outbox (dropped after 7 days) or the
  target machine's per-session mailbox (one day). An always-on "anchor" is
  planned and not started [RN-DES].
- The team link is reusable with no fixed expiry and rotates on member removal.
  Four-word codes are one-use and need the inviter online [RN-DES].
- Its own design notes say a removed machine "is not made cryptographically
  powerless: this is for friends" and that `ask_user` approvals are "a speed
  bump, not a guarantee" [RN-DES].

Interoperating now would tie Brigade to an unversioned protocol that changed
five times in a day, and to an identity model without per-message signatures or
a content layer, which #2005 requires. Rejected for now. Revisit if Reason
Network publishes a versioned wire spec. Users can already run its MCP server
next to Brigade's, because both are ordinary MCP servers.

### Path c: hub-less store-and-forward

Each node keeps its own inbox and outbox, peers deliver to each other, and an
optional relay carries only ciphertext. This is a protocol, not a transport:
behind NAT, two peers still need something like iroh to reach each other.

### Decision

Use path c's model over path a's transport.

- Brigade owns the protocol: the same version 1 envelopes, signed key records
  as contact cards, a local SQLite inbox, and the existing sender spool as the
  outbox.
- `engines/mesh/` adds a small Rust binary, `brigade-mesh`, built on iroh 1.x
  and installed by `brigade setup` like the code-graph engine. It accepts
  frames on ALPN `brigade-mesh/1`, writes received envelopes to the local
  inbox database, sends rows from the outbox, and returns `stored` receipts.
  Python and the engine share the SQLite database in WAL mode. Python never
  links Rust, and the engine never decrypts.
- The iroh endpoint key is separate from the Ed25519 identity key. The key
  record's `transport.iroh` field, covered by `self_sig`, binds them. This keeps
  network-facing code away from the message keys and keeps signing contexts
  separate.
- Offline delivery: the sender's outbox retries for 7 days, and an optional
  later "anchor" mode lets any always-on Brigade node hold sealed envelopes for
  contacts that opted in. An anchor is another peer, not a new server type, and
  it sees only ciphertext and routing ids. Users who need delivery between
  machines that are never online together can also deploy Hub Lite.
- Relay choice is explicit at `brigade mesh init`: `--relay <url>` for a
  self-hosted `iroh-relay` with an endpoint allowlist (iroh's minimal preset,
  no n0 contact), `--relay n0-public` (labelled for development use, rate
  limited, and publishes the endpoint id and home relay to n0's DNS), or
  `--relay none` (LAN, tailnet and direct addresses only). Brigade runs no
  relay and picks no default for the user (open question 4).
- No-hub invites use the same `brigade fleet join` command with a
  `bmi1.<payload>` token instead of an https link. The payload carries the
  inviter's key record (with its iroh endpoint id), relay and address hints, a
  one-use secret and an expiry. The joiner adds the inviter as a contact at
  once and sends a join request over iroh. If the inviter is offline, the
  request waits in the outbox, so the join finishes when both are next online.

The spike's first slice measures what this design assumes: binary size, cold
start, whether two machines on different networks connect through a
self-hosted relay, and how often a direct path forms. If the engine misses
those marks, the fallback is no-hub mode limited to LAN and tailnet addresses
plus Hub Lite for everything else, and this section gets rewritten.

## D9. One MCP tool contract for hub, Hub Lite and no-hub

`brigade fleet mcp` picks a backend from `fleet.mode` (`hub`, the default when
a hub URL is configured, or `mesh`). Hub Lite is the `hub` backend with a
different URL and token. Both backends implement one interface: `who`, `send`,
`reply`, `list`, `ack`, `sent` and `claims`.

The live delivery hook and `brigade fleet msg` call `fleet_client_messages`
directly today. They move onto the same backend interface, or no-hub mode
would have no live delivery and no CLI.

Input schemas for all seven tools stay exactly as `tool_specs()` defines them.
IDs and cursors stay decimal strings within SQLite's range. In mesh mode they
are local inbox and outbox row ids.

| Tool | Hub and Hub Lite | No-hub |
|---|---|---|
| `fleet_who` | nodes, principals, sessions, key fingerprints | contacts with key fingerprints, `online` and `last_seen_at`. `sessions` is an empty list. |
| `fleet_send`, `fleet_reply` | sealed per D4 | always sealed. `delivery` is `delivered` (the peer stored it) or `spooled`. |
| `fleet_inbox` | hub long-poll | local database, waited on locally |
| `fleet_ack` | hub ack | local ack plus a signed ack receipt to the sender |
| `fleet_sent` | hub sent list | local outbox with receipt state |
| `fleet_claims` | claims | `{"code": "hub_feature_unavailable", "permanent": true}` |

New fixed error codes join `_SAFE_ERROR_CODES`: `mesh_extra_missing`,
`recipient_key_missing`, `recipient_key_changed`, `recipient_key_stale`,
`message_sealing_required`, `principal_role_forbidden`,
`hub_feature_unavailable` and `mesh_engine_unavailable`.

The docs for no-hub mode say plainly that claims, runs, patrol, dispatch and
MCP Events need a hub.

## D10. Hub Lite

A template under `workers/hub-lite/` that a user deploys into their own
Cloudflare account. Brigade runs no shared instance.

Shape. One Worker routes requests to one SQLite-backed Durable Object per
fleet, addressed by a fixed name. The object does authentication, storage and
long-poll wake-ups. One object per fleet is a budget decision as well as a
consistency one (see the limits below).

API subset, matching what the MCP station, `brigade fleet msg`, `brigade fleet
join` and session presence call today:

| Route | Actions | Auth |
|---|---|---|
| `GET /health` | liveness | none |
| `GET /mesh/identity` | hub id, `require_sealed: true`, envelope versions | none |
| `GET /join` | static instructions page | none |
| `POST /messages` | `send`, `reply`, `list` (with `wait_ms` up to 20 s), `sent`, `ack`, `who`, `claims`, `keys_publish`, `keys_get` | principal or node token |
| `POST /invites` | `create`, `list`, `revoke` | admin token |
| `POST /invites/redeem` | redeem | invite secret |
| `POST /message-principals` | `get`, `grants`, `revoke` | admin token |
| `POST /sessions`, `GET /sessions` | presence heartbeat and list | node token |
| `POST /claims`, `GET /claims` | `acquire`, `renew`, `release` with holder fencing tokens and TTL, in a later slice | node token |
| `GET /audit` | audit log page | admin token |

Out of scope: `/events` (run events), `/status`, `/cloud`, `/models`,
`/policy`, `/preference`, `/owners`, `/work`, `/ci`, dashboard enrollment, deck
pages and MCP Events webhooks. The `scope: "node"` claim supersede extension
is also left out, so `brigade run` keeps using a full hub for claim recovery.

Everyone joins through invites, including the operator's own machines
(`--role node`). The admin token is a Worker secret set at deploy time.

Storage layout (SQLite in the Durable Object):

```
meta(key PRIMARY KEY, value)                      -- hub_id, schema version
principals(principal_id PRIMARY KEY, role, name, person, label,
           token_hash UNIQUE, created_at, revoked_at)
grants(principal_id, pattern, PRIMARY KEY(principal_id, pattern))
keys(principal_id, kid PRIMARY KEY, record, state, created_at, retired_at, revoked_at)
messages(id INTEGER PRIMARY KEY AUTOINCREMENT, sender, recipient, kind, priority,
         client_id, reply_to, envelope, envelope_bytes, payload_hash,
         created_at, received_at, acked_at, acked_by,
         UNIQUE(sender, client_id))
sends(sender, day, count, PRIMARY KEY(sender, day))
sessions(node_id, session_id, harness, state, repo_label, branch,
         heartbeat_at, expires_at, PRIMARY KEY(node_id, session_id))
claims(target PRIMARY KEY, owner, holder_hash, expires_at)
invites(invite_id PRIMARY KEY, redeem_hash, role, inviter, name, grants,
        created_at, expires_at, max_uses, uses, revoked_at)
audit(id INTEGER PRIMARY KEY AUTOINCREMENT, at, actor, action, subject, detail)
```

`messages` has no `subject` or `body` column, so the Worker cannot store
plaintext even by mistake. Sends without an envelope are refused. Indexes are
kept to `(recipient, id)` and the unique `(sender, client_id)`, because every
index entry counts as a row written.

Free plan limits that apply, from Cloudflare's docs as of 2026-10-22
[CF-WLIM] [CF-WPRC] [CF-DOPRC] [CF-DOLIM]:

| Limit | Free plan | Effect on Hub Lite |
|---|---|---|
| Worker requests | 100,000 per day, reset at 00:00 UTC, then Error 1027 | Every hub call is one Worker request. |
| Durable Object requests | 100,000 per day | Every hub call is also one object request. |
| Durable Object duration | 13,000 GB-s per day, billed at 128 MB | A held long-poll keeps the object awake. One object awake all day uses 10,800 GB-s (83 percent). |
| SQLite rows written | 100,000 per day (deletes and `setAlarm` count) | Likely the binding limit for mail. |
| SQLite rows read | 5,000,000 per day | Not binding at fleet scale. |
| Storage | 5 GB per account (one FAQ line says 1 GB per object, UNVERIFIED) | 90 days of mail at 96 KiB worst case is far below this. |
| Worker CPU | 10 ms per request | Envelope checks are string and JSON work. Whether the 10 ms cap also applies to object invocations is UNVERIFIED. |
| Over a limit | further operations of that type fail with errors, no billing | Clients see `hub_unavailable` and spool sends. |

Derived budgets, to be measured by the contract tests rather than trusted:

- Requests, not rows, bind first. A busy agent session makes three kinds of
  calls. Session presence upserts on every `PreToolUse` and `PostToolUse`
  (`fleet_session_presence.py`), live delivery polls up to once every 20
  seconds on tool events, and an MCP station waiting on its inbox issues three
  20 second long-polls a minute. Unthrottled, presence alone can exceed the
  cap. Hub Lite therefore needs two client-side throttles, both in the plan:
  presence coalesced to one heartbeat per session per 60 seconds unless state
  changes (1,440 requests a day), and the delivery poll interval raised to 60
  seconds when the hub is Hub Lite (1,440 a day). With a waiting station
  (4,320 a day), a busy session costs about 7,200 Worker requests and the same
  number of object requests a day. That is roughly 10 to 12 sessions busy all
  day, before sends. The contract run in the plan measures the real figure.
- Durable Object duration is billed per object while it is awake, not per held
  request, so one object per fleet stays under the duration cap even when
  several stations wait at once. A second always-awake object in the same
  account would exceed it. The Hub Lite docs will recommend one Hub Lite per
  Cloudflare account on the Free plan.
- A message costs roughly ten written rows over its life (insert and index,
  quota counter, ack, prune). That suggests on the order of 10,000 messages a
  day. The contract test reports the measured number.
- Presence would be the largest writer if every heartbeat were stored. Hub Lite
  keeps heartbeat times in memory and writes a session row only when its state,
  repository or branch changes, or once per third of the TTL. Presence is lost
  when the object is evicted, which matches what presence means.

Long-poll. `list` with `wait_ms` parks the request on an in-memory waiter keyed
by recipient, resolved by a send to that recipient in the same object. A held
request stops the object from hibernating, which the duration budget above
accounts for. The object can be evicted at any time, so messages are written to
SQLite before any waiter is woken, and clients already retry an interrupted
wait. A WebSocket Hibernation channel would remove the duration cost for idle
waiters [CF-DOWS], but the Python client has no WebSocket library, so it waits
for a later slice.

WebCrypto in workerd supports X25519 and Ed25519 [CF-WC], so Hub Lite can
optionally verify envelope signatures on send to reject junk early. The
authoritative check stays in the receiving client. Workers have no native HPKE
[CF-WC], which does not matter because Hub Lite never decrypts.

Deployment. A "Deploy to Cloudflare" button clones the template into the
user's GitHub or GitLab account and provisions bindings including Durable
Objects [CF-DEPLOY]. The button requires a public repository on github.com or
gitlab.com and treats a subdirectory as the repository root, so
`workers/hub-lite/` must be self-contained. Whether the button applies the
Durable Object migration on first deploy is UNVERIFIED, so the plan tests it
on a fresh account before the docs promise it, and documents `npx wrangler
deploy` from a clone as the fallback. Unlike the webhook relay, Hub Lite needs
a URL, so its template sets `workers_dev = true`.

Cloudflare Access can sit in front of the Worker [CF-ACCESS], but agents would
then need Access service tokens. Hub Lite's own bearer tokens and sealed bodies
do not depend on Access, so it stays optional and is documented for the admin
routes only. The commonly quoted "free for 50 users" Zero Trust limit is
UNVERIFIED and the docs will not repeat it.

## D11. What stays plain text, and why

| Data | Why the hub must read it |
|---|---|
| Claims (target, owner, holder digest, expiry) | The hub arbitrates mutual exclusion by comparing targets, and patrol and the deck read them. Keyed hashes of targets would keep equality checks but break the deck and patrol. That is a possible follow-up outside this work. |
| Presence and sessions (harness, repo label, branch, state) | Routing, `fleet_who` and the deck. Teammates see a reduced view (D7). |
| Run events and status | Patrol, run recovery, policy routing and the deck. |
| Message routing metadata | Authorization, grants, quotas, idempotent retries, ordering and `message.created` events. |
| Keys | Public by design. |
| Grants, invites and the audit log | The hub enforces them. The audit log holds no secrets or content. |

The user docs will say this in one place: a hub operator can see who messages
whom, when, how often and roughly how much, and everything about claims, runs
and presence. A hub operator cannot read sealed subjects or bodies.

## Threat model

Assets: message subjects and bodies, the private keys, principal tokens, and
the integrity of who said what.

| Party | Can see | Cannot see | Can forge or do | Cannot forge |
|---|---|---|---|---|
| Hub operator (self-hosted hub or Hub Lite owner) | all routing metadata, padded sizes, claims, presence, runs, keys, grants, audit log, tokens as presented on each request | sealed subjects, bodies, refs, threads and labels | drop, delay or reorder mail. Replay an envelope under a new message id: the replay cache catches it on a machine that saw the original, but not on a fresh install or a second station inside the 90 day window. Serve a substituted key on first contact with a principal that was never pinned through an invite or a fingerprint check. Withhold a never-pinned recipient's key so an `auto` client sends plain text. Pass off a revoked key as retired, which matters only together with a stolen key. Mint principals with the admin token. | a signed message from a pinned principal. A plain-text row under a pinned principal's name (it opens as `unsealed_from_pinned`). A reply link that disagrees with the signed `in_reply_to`. A message from another hub or older than the retention window. Plain text sent to a pinned peer. |
| Relay (an iroh relay, n0's public relays, or an optional anchor) | endpoint ids of both ends, IP addresses, timing and volume [IROH-PUB]. An anchor also sees routing ids and padded sizes. | QUIC payloads and envelope contents | drop or delay traffic | any message, or a key record (records are signed) |
| Cloudflare, for Hub Lite | the same as a hub operator. It terminates TLS, so it sees bearer tokens, and it can change the code that runs. | sealed contents, private keys (they never leave clients) | everything a hub operator can, including using a stolen token to send as a principal. Mail sent that way is either plain text, which pinned recipients refuse as `unsealed_from_pinned`, or signed by a key they have not pinned, which opens as `sender_key_changed`. Recipients that never pinned the principal are exposed in the same way they are to a hub operator. | a signed message from a pinned principal |
| A teammate | its own mail, and the ids, labels and key fingerprints of principals in its grant | claims, runs, session details, other principals' mail and metadata | send to its granted principals. Forward a signed message it received to others: Ed25519 signatures are not deniable, so a message is transferable proof of authorship. | messages from another person or another of its own machines, mail to principals outside its grant |
| A compromised node or station | everything sealed to that principal, including stored envelopes still inside retention (no forward secrecy), plus its outbox and spool | mail sealed only to other principals | send as itself to anyone its role permits. A node principal is unrestricted. Publish a rotated key with a valid continuity signature until it is revoked. | messages from other principals. A key for another principal (`keys_publish` is bound to the caller's token). |

End-to-end means "to the process that holds the private key". That is the MCP
station or the CLI on the recipient's machine. A hosted agent connected to that
station through a connector (Dot, or Duet under #1943) receives plaintext, and
so does its platform.

Out of scope for this design: hiding who talks to whom from the hub, deniable
authentication, forward secrecy, and post-quantum key exchange. The `suite`
field leaves room for the last one.

## Open questions for the operator

1. Dependency (blocks plan slice 1): approve `brigade-cli[mesh]` with
   `cryptography>=47,<52` on #2005, including the Intel Mac
   `--only-binary` instruction and the missing Windows arm64 wheel.
2. Sealed body retention: clear envelopes seven days after ack (proposed) or
   keep them for the full 90 days.
3. Key changes after a token rotation: fail closed everywhere with a bulk
   `keys accept --all-changed` (proposed), or auto-accept for the operator's
   own nodes on a self-hosted hub.
4. No-hub relay default: require an explicit choice at `brigade mesh init`
   (proposed), or default to n0's public relays with a warning.
5. Native engine: accept a new Rust engine under `engines/mesh/` with iroh 1.x,
   built in CI only when its paths change, like `engines/code-graph`.
6. Teammate inbound mail: symmetric grants (proposed) or the current
   sender-side-only grants.
7. Hub Lite claims: include acquire, renew and release in Hub Lite (proposed
   as its last slice), or keep Hub Lite to messaging and presence.

## Sources

All retrieved on 2026-10-22.

- [PY] Python 3.14 Cryptographic Services: https://docs.python.org/3.14/library/crypto.html
- [CRY-PYPI] cryptography on PyPI: https://pypi.org/pypi/cryptography/json
- [CRY-CHG] cryptography changelog (47.0.0 adds HPKE, 49.0.0 removes x86_64 macOS and 32-bit Windows, 46.0.4 drops win_arm64): https://cryptography.io/en/latest/changelog/
- [CRY-HPKE] cryptography HPKE API: https://cryptography.io/en/latest/hazmat/primitives/hpke/
- [CRY-INST] cryptography installation: https://cryptography.io/en/latest/installation/
- [NACL-PYPI] PyNaCl on PyPI: https://pypi.org/pypi/PyNaCl/json
- [NACL-CHG] PyNaCl changelog: https://pynacl.readthedocs.io/en/latest/changelog/
- [RN-README] Reason Network README: https://github.com/reason-machines/reason-network
- [RN-DES] Reason Network DESIGN.md: https://github.com/reason-machines/reason-network/blob/main/DESIGN.md
- [RN-SEC] Reason Network SECURITY.md: https://github.com/reason-machines/reason-network/blob/main/SECURITY.md
- [RN-SRC] Reason Network source, `src/net.rs`, `src/team.rs`, `src/proto.rs`, `src/store.rs` at the v0.12.9 commit: https://github.com/reason-machines/reason-network/tree/main/src
- [RN-REL] Reason Network releases: https://github.com/reason-machines/reason-network/releases
- [IROH-REL] iroh releases (v1.0.0, v1.4.1, iroh-relay binaries): https://github.com/n0-computer/iroh/releases
- [IROH-RLY] iroh-relay README, access control: https://github.com/n0-computer/iroh/blob/main/iroh-relay/README.md
- [IROH-CON] iroh relays concept page: https://docs.iroh.computer/concepts/relays
- [IROH-PUB] iroh public relays: https://docs.iroh.computer/iroh-services/relays/public
- [IROH-SHR] iroh shared relays: https://docs.iroh.computer/iroh-services/relays/shared
- [IROH-LKP] iroh address lookup: https://docs.iroh.computer/concepts/address-lookup
- [IROH-PY] iroh Python package and iroh-ffi: https://pypi.org/pypi/iroh/json and https://github.com/n0-computer/iroh-ffi
- [CF-WLIM] Workers limits: https://developers.cloudflare.com/workers/platform/limits/
- [CF-WPRC] Workers pricing: https://developers.cloudflare.com/workers/platform/pricing/
- [CF-DOPRC] Durable Objects pricing: https://developers.cloudflare.com/durable-objects/platform/pricing/
- [CF-DOLIM] Durable Objects limits: https://developers.cloudflare.com/durable-objects/platform/limits/
- [CF-DOWS] Durable Objects WebSockets and hibernation: https://developers.cloudflare.com/durable-objects/best-practices/websockets/
- [CF-WC] Workers Web Crypto: https://developers.cloudflare.com/workers/runtime-apis/web-crypto/
- [CF-DEPLOY] Deploy to Cloudflare buttons: https://developers.cloudflare.com/workers/platform/deploy-buttons/
- [CF-ACCESS] Cloudflare Access for Workers: https://developers.cloudflare.com/workers/configuration/cloudflare-access/
