# Worklore ownership metadata

An operator can offer a Worklore item to an enrolled node. The named node accepts
with a private nonce, then records checkpoints, offers a handoff, or releases
ownership. The Hub records these actions as immutable events in its existing
`work_events` table. This protocol requires no schema migration.

Ownership grants authority to mutate this Hub metadata. It does not establish
filesystem exclusivity, prove that a process stopped, authenticate a verification
result, or complete an item. Every projection reports `liveness: "unknown"` and
`conflict_check: "not-performed"`. Local filesystem and repository locks still
control physical exclusion. A previous writer may keep running after a handoff
has fenced its Hub authority.

## Authority and lifecycle

`GET /work/items/{id}/ownership` returns an `ownership` object for an existing
item. With no ownership events, the state is `unowned`, revision 0, generation 0.
The read uses the existing fleet authentication policy. An absent item returns
`not-found`. Reads and writes never create an item.

`POST /work/items/{id}/ownership` takes an action and the exact fields below.
Every mutation requires `If-Match` with the current ownership revision and an
explicit `Idempotency-Key`. Ownership revisions are separate from item versions.

| Action | Required body fields besides `action` | Authority | State change |
| --- | --- | --- | --- |
| `offer` | `target_node`, `exclusions`, `authorization_ref`, `attempt_budget` | Admin or configured operator node | Unowned to offered |
| `withdraw` | None | Admin or configured operator node | Offered to unowned |
| `accept` | `generation` | Authenticated named target node | Offered or handoff-pending to owned |
| `checkpoint` | `generation`, `repo_identity`, `write_scope`, `source_revision`, `next_action`, `evidence_refs` | Current holder | Owned remains owned |
| `handoff` | `generation`, `target_node` | Current holder | Owned to handoff-pending |
| `release` | `generation` | Current holder | Owned or handoff-pending to unowned |

The offer event is the authorization root. `granted_by` comes from the bearer
principal, and the operator supplies exclusions, authorization reference, and
attempt-budget provenance. Offers and handoffs require an enrolled target with
no revocation. An operator node can accept only when it is the named target.
Operator privileges cannot substitute for holder authority. Admin cannot accept,
checkpoint, hand off, or release on behalf of a holder.

Accept increments the generation. Release, withdrawal, and new offers retain
that counter. A handoff copies the grant, exclusions, and attempt-budget metadata
on the server. The previous holder may release while the handoff is pending.
Checkpoint requires owned state. Accepting the handoff fences the previous
generation and nonce, including a handoff on the same node. An offer targets a
node principal, not a particular process. Any process with that node's token can
race to accept the initial offer or handoff and supply its own fresh nonce.

Completed, canceled, and archived items permit release and withdrawal. They
refuse new offers, accepts, checkpoints, and handoffs. The protocol never changes
item status, item version, attempt count, or burn eligibility. Attempt budget is
operator-authenticated handoff metadata only. Existing attempt and burn rules
remain authoritative.

## Nonce and replay

Accept, checkpoint, handoff, and release require `X-Worklore-Holder`. Its value
must be the canonical unpadded base64url encoding of exactly 32 bytes. Generate
it with `worklore_client.new_ownership_nonce()` and retain it privately. It is
never returned by the server or automatically persisted by the client. Keep it
out of URLs, JSON bodies, logs, receipts, and shared history.

Every new acceptance requires a nonce whose hash has never been accepted for
that work item, including after release and reoffer. An authorized historical
accept replay still uses its original nonce and returns its original snapshot.

The Hub rejects any body string or key, or idempotency key, containing the
presented canonical nonce, its standard base64 encoding (padded or unpadded), or
its lowercase or uppercase hex encoding. This includes embedded occurrences
and offer metadata when the holder header is supplied. The check runs before
fingerprinting, replay lookup, or storage. It returns a fixed `private-data`
error and writes no event. It does not detect arbitrary deliberate re-encodings.
An optional holder header on offer or withdrawal must also be canonical.

The Hub persists only the nonce SHA256 digest. Holder comparisons use
constant-time comparison. Current projections omit the holder hash and request
fingerprint. Globally readable events include hashes and bounded metadata under
the existing fleet read policy. They contain no raw nonce or private result body.

Each transaction uses `BEGIN IMMEDIATE` for replay lookup, ownership CAS, state
validation, capacity checking, and event insertion. Event identity includes the
stable authenticated principal class (`admin` or enrolled `node`), actor ID, and
idempotency key. Holder events always use the node identity, even after promotion
or removal of operator privileges. Offers and withdrawals require current
operator or admin privilege on every request, including replay.
A retry must use the same
body, nonce, generation, expected revision, and key. The request fingerprint
contains the nonce hash in place of the secret.

Replay first checks the current bearer and operation privilege, then historical
actor, action, named target or holder, generation, nonce hash, and normalized
fingerprint. It returns the original sanitized snapshot after later writes
without adding an event. A revoked bearer or lost operator privilege cannot
replay an offer. Conflicting key reuse returns `idempotency-conflict`. Missing
keys return `idempotency-key-required`. Stale revisions return `version-conflict`,
and holder failures return `holder-mismatch` or `stale-generation`.

The Python client uses node authentication for all ownership mutations, including
operator offers and withdrawal. It has no admin fallback for a mutation. Reads
retain the existing node-first authentication preference. HTTP admin callers
may offer and withdraw through the same Hub route. The client forwards the nonce
through one fixed header. It accepts no arbitrary caller headers.

```python
from brigade import worklore_client

# The operator has already offered this item to the authenticated node.
work_id = "wl-example"
offer = worklore_client.get_ownership(work_id)["ownership"]
nonce = worklore_client.new_ownership_nonce()
accepted = worklore_client.ownership_action(
    work_id,
    {"action": "accept", "generation": offer["generation"]},
    if_match=offer["revision"],
    idempotency_key="accept-example-1",
    holder_nonce=nonce,
)["ownership"]
```

## Exact bounds

Unknown fields are rejected at every body level. All strings reject controls,
line separators, private home paths, and credential-shaped values.

| Field | Bound |
| --- | --- |
| Action and idempotency key | 128 characters each |
| Node identity | 128 characters, existing safe node syntax. `admin` and `unknown` reserved |
| Ownership revision and generation | Integer 0 through 9,999,999,999. Booleans refused |
| Repository identity | 255 characters, canonical relative identifier with slash-separated segments using letters, digits, period, `_`, or hyphen |
| Write scope | Up to 32 distinct canonical relative paths, 256 characters each |
| Exclusions | Up to 32 nonblank single-line strings, 256 characters each |
| Authorization and budget source references | 128 characters, existing evidence-reference syntax |
| Attempt budget | Exact `{cap, source_ref}` object, integer cap 1 through 100,000 |
| Resume condition | 512 characters, empty allowed |
| Evidence references | Up to 20 exact `{kind, ref, source_revision}` records |
| Evidence ref | 128 characters, letters, digits, period, `_`, or hyphen |
| Source revision | Exactly 40 lowercase hexadecimal characters |
| Holder header | Exactly 43 base64url characters encoding 32 bytes |
| Serialized event detail | 65,536 UTF-8 bytes, including JSON escaping and copied metadata |

Paths cannot have absolute, drive, UNC, traversal, dot, empty, backslash, or
wildcard segments. Paths declare scope only. This slice has no filesystem or
symlink enforcement. All ownership metadata strings reject absolute POSIX
references at text boundaries and Windows drive or UNC references, including
references embedded in exclusions and resume conditions. Relative references
such as `src/module.py` and ordinary single-line prose remain supported.
These rules apply to new ownership input, without changing legacy item validators
or historical reads.

The byte ceiling supplements character limits and applies before insertion.
Escaped non-ASCII text can exceed it while satisfying individual field bounds.
An oversized event returns `field-bound` and writes nothing. A 100-item ownership
history page stays below the client's existing 8 MiB response cap. Release and
withdrawal clear metadata and remain small safety exits.

`next_action` is exactly `{kind, resume_condition}`, with
kind `implement`, `verify`, `await-review`, `await-checks`, `await-merge`, or
`blocked`. It is advisory. Evidence kind is `receipt`, `github-pr`,
`github-check-run`, or `worklore-event`.

Evidence records carry references only. Outcomes, raw results, transcripts,
execution handles, merge conclusions, and check conclusions are refused. The
source system controls access and authority for any result behind a reference.
Later checkpoints never rewrite older snapshots or failed, canceled, and
superseded history.

## Capacity and recovery

Each item permits 200 ordinary ownership events, with a total maximum of 201.
Offer and handoff require a prior count at most 197. Accept and checkpoint must
leave the count at most 200. Release and withdrawal are safety exits that may
append event 201. An exhausted chain cannot start another ownership cycle.
`ownership-capacity-exhausted` requires a successor item.

Claim expiry, session end, lease expiry, node revocation, and unknown liveness
never transfer ownership automatically. A dead holder remains recorded until an
authorized release or a later reviewed recovery mechanism. A successor item is
a bounded recovery option, not permission to take a still-running writer's files.
Atomic write-conflict checks, authoritative verification and review ingestion,
GitHub merge reconciliation, and issue completion require later slices.
