# Worklore ownership metadata

An operator can offer a Worklore item to an enrolled node. The named node accepts
with a private nonce, then records checkpoints, offers a handoff, or releases
ownership. The Hub records these actions as immutable events in its existing
`work_events` table. Hub schema 41 adds a nonunique covering index on
`(event_id, work_id)` for campaign take key lookups. Startup upgrades existing
databases without changing stored events or the item-scoped event primary key.

Ownership grants authority to mutate this Hub metadata. It does not establish
filesystem exclusivity, prove that a process stopped, authenticate a verification
result, or complete an item. Every ownership read derives a liveness state from
existing Hub facts at read time; the Hub stores no liveness value. See
[owner liveness](#owner-liveness).
New checkpoints record `conflict_check: "path-admission"`, a historical check of
declared repository paths at that ownership revision. Older unchecked snapshots
retain `not-performed`. See [checkpoint path admission](phase-worklore-path-scope-admission.md)
for atomic replacement, overlap rules and bounded refusals. Local filesystem and
repository locks still control physical exclusion. A previous writer may keep
running after a handoff has fenced its Hub authority.

## Authority and lifecycle

### Prepared handoff context

`brigade fleet work handoff show <work-id> [--json]` makes one authenticated
`GET /work/items/<id>/prepared-handoff`. The HTTP envelope is
`{"prepared_handoff": VIEW}`, with schema `brigade.prepared_handoff.v1`.
The command prints the validated view. It reads existing context and grants no
claim, acceptance, dispatch, or execution authority.

The view uses one deferred database read transaction and reports
`consistency: "single-db-snapshot"`. `observed_at` follows the first read.
Item `version` remains separate from ownership `revision`, `generation`, and
`last_seq`. Own acceptance criteria and effective source criteria remain distinct.
Absent checkpoint and report data remain null.

Existing node or Hub-admin credentials provide fleet-wide read access
(`visibility: "fleet-trust"`). Dashboard cookies cannot read this route.
Person attribution and historical human attribution are `unrecorded`.
This route adds no project isolation or person identity records.

Checkpoint conflict admission is historical. Evidence stays reported and
unverified, liveness is derived on read, and report freshness is `recent`,
`stale`, `future`, or `unknown` rather than `unassessed`.
Checkpoint binding to the current item version and execution binding to the
ownership generation are unavailable. Item edits and accepted handoffs may
retain older checkpoint/report data. Report timestamps and session sequences
are preserved as reported metadata, separate from Hub ownership counters.

`run_refs` covers recorded execution events only. It reads the newest 51
execution-linked rows, deduplicates composite node/run identities within the
first 50, and reports truncation when a 51st row exists, even for duplicates.
It does not establish a complete execution inventory. Generic links,
descriptions, raw event detail, credential fields, and transcript bodies are
omitted. Existing text validation does not classify every secret or instruction.
Included prose remains untrusted reported context.

The existing `fleet work ownership show` command retains its separate reads.

### Existing ownership reads and writes

`GET /work/items/{id}/ownership` returns an `ownership` object for an existing
item. With no ownership events, the state is `unowned`, revision 0, generation 0.
The read uses the existing fleet authentication policy. An absent item returns
`not-found`. Reads and writes never create an item. A read that also sends
`X-Worklore-Holder` asks to be treated as the holder: it succeeds only for the
current holder node with the current capability at the current generation, and
otherwise returns `holder-mismatch` (403). Admin cannot present a holder
capability. Python callers pass `holder_nonce` to `worklore_client.get_ownership`,
which sends it only under the node token.

`POST /work/items/{id}/ownership` takes an action and the exact fields below.
Every mutation requires `If-Match` with the current ownership revision and an
explicit `Idempotency-Key`. Ownership revisions are separate from item versions.

| Action | Required body fields besides `action` | Authority | State change |
| --- | --- | --- | --- |
| `offer` | `target_node`, `exclusions`, `authorization_ref`, `attempt_budget` | Admin or configured operator node | Unowned to offered |
| `withdraw` | None | Admin or configured operator node | Offered to unowned |
| `accept` | `generation` | Authenticated named target node | Offered or handoff-pending to owned |
| `checkpoint` | `generation`, `repo_identity`, `write_scope`, `source_revision`, `next_action`, `evidence_refs` | Current holder | Owned remains owned |
| `report` | `generation`, `report` | Current holder | Owned remains owned |
| `handoff` | `generation`, `target_node` | Current holder | Owned to handoff-pending |
| `release` | `generation` | Current holder | Owned or handoff-pending to unowned |
| `recover` | `generation`, `holder_quiesced: true`, `evidence_ref` | Admin or configured operator node, campaign members only | Owned or handoff-pending to unowned, increments generation |

These per-item offer and handoff rules apply to noncampaign items. Campaign members
use the bounded campaign offer and `take` described in [fleet campaigns](fleet-campaigns.md).

The offer event is the authorization root. `granted_by` comes from the bearer
principal, and the operator supplies exclusions, authorization reference, and
attempt-budget provenance. Offers and handoffs require an enrolled target with
no revocation. An operator node can accept only when it is the named target.
Operator privileges cannot substitute for holder authority. Admin cannot accept,
checkpoint, report, hand off, or release on behalf of a holder.

Accept increments the generation. Release, withdrawal, and new offers retain
that counter. A handoff copies the grant, exclusions, and attempt-budget metadata
on the server. The previous holder may release while the handoff is pending.
Checkpoint requires owned state. Accepting the handoff fences the previous
generation and nonce, including a handoff on the same node. An offer targets a
node principal, not a particular process. Any process with that node's token can
race to accept the initial offer or handoff and supply its own fresh nonce.

Completed, canceled, and archived items permit release and withdrawal, plus explicit
campaign recovery. They refuse new offers, accepts, checkpoints, reports, and handoffs. The protocol never changes
item status, item version, attempt count, or burn eligibility. For noncampaign
items, attempt budget is operator-authenticated handoff metadata.
Campaign node execution and attempt writes also enforce the current campaign grant
and its budget. Existing attempt and burn rules remain authoritative.

## Owner liveness

The ownership read derives `liveness`, `liveness_basis`, and `liveness_freshness`
from facts the Hub already stores. Derivation is read-only: no table, index,
column, or fleet event, nothing stored, and no row written. The prepared handoff
carries the same derived `liveness` value.

Every fact is scoped to this item and this ownership, never to the owner node in
general. A session or claim counts only for the snapshot's `repo_identity`, and
only the newest session for that repository counts. A linked run counts only
when the owner node itself linked it after the current ownership generation
began. A previous owner's run, another node's run, and another repository's
session or claim never decide this item's liveness. When the snapshot has no
`repo_identity`, sessions and claims are not read at all.

The facts are a bounded set of parameterized reads: the owner node's newest
`interactive_sessions` row for this repository (excluding a finished v2 cloud
report), unexpired `claims` rows whose target names this repository, the
snapshot's explicit campaign lease deadline, and the terminality of the newest
`events` row for the newest `execution-linked` run that this owner linked in the
current generation. A missing sessions or claims table, a NULL, or a malformed
timestamp yields no fact and never raises.

| State | Exact meaning | Basis |
| --- | --- | --- |
| `live-owner` | The owner node has a live session or a live claim for this item's repository. It does not prove the owner is working on this item, only that the node is active in that repository. | `session` or `claim` |
| `idle-owner` | The owner node's newest session for this repository ended after the ownership snapshot's last update, and the item is not terminal. Work may be unfinished, but nothing currently shows the owner running. | `session` |
| `expired-observation` | The snapshot's own campaign lease deadline has lapsed. This wins over any session or claim: the rest of the system already treats the item as reclaimable. | `lease` |
| `terminal-execution` | The newest run this owner linked in the current generation is terminal, with no live session or claim for this repository. | `run` |
| `unknown` | No owner, a terminal item, or no scoped fact at all; also the fallback. | `none` |

Precedence, first match wins: no `owner_node` or a terminal item is `unknown`
(`none`); a lapsed lease is `expired-observation` (`lease`); a live session,
then a live claim, for this repository is `live-owner` (`session`, then
`claim`); a terminal linked run of this owner in this generation is
`terminal-execution` (`run`); a counted session end on a non-terminal item is
`idle-owner` (`session`); anything else is `unknown` (`none`). A live owner is
therefore never reported as terminal or expired, and an abandoned item is never
kept live by another node's or another repository's activity.

`liveness_freshness` applies the deck's 1,800 second rule to the newest timestamp
among the facts that survived that scoping (the scoped session heartbeat and
counted end, scoped claim renewal, and the scoped run event): `recent`, `stale`,
`future`, or `unknown`. Unrelated sessions and claims on the same node never
contribute.

The read is public within the fleet. The three fields reveal only the state,
basis, and freshness words; a claim target, session id, process id, path, or
another node's capability is never returned.

Prepared handoff report freshness uses the same four words. An older reader
accepts only `liveness: "unknown"` and freshness `"unassessed"` and refuses the
new values, so it fails closed with an invalid-response refusal. A newer reader
still accepts an older Hub's `unassessed`, but an older CLI cannot read a newer
Hub's prepared handoff. Upgrade the Hub and the CLI together; the refusal is
fail-closed rather than a silently wrong reading.

## Nonce and replay

Accept, checkpoint, report, handoff, and release require `X-Worklore-Holder`. Its value
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
operator or admin privilege on every request, including replay. Campaign recovery
requires the same current privilege.
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

Raw HTTP checkpoint refusals include bounded `conflicts`, `resolution` and
`truncated` details when applicable. The existing Python client preserves only
the error code and sanitized message. Bounded propagation of those structured
details through the client remains epic work.

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

Checkpoint evidence records carry references only. Outcomes, raw results, transcripts,
execution handles, merge conclusions, and check conclusions are refused. The
source system controls access and authority for any result behind a reference.
Later checkpoints never rewrite older snapshots or failed, canceled, and
superseded history.

The separate `report` action accepts bounded Dot session metadata through
`worklore_client.report_ownership`. It stores `last_report` with
`evidence_verification: "reported-unverified"` and
`provider_lifecycle: "unobserved"`. Reports do not establish verification or
provider liveness. Reporter/session sequence must increase, observation time
must not move backwards, and session linkage must remain consistent.

## Capacity and recovery

Each item permits 200 ordinary ownership events, with a total maximum of 201.
Offer, handoff, and campaign take require a prior count at most 197. Accept,
checkpoint, and report must leave the count at most 200. Release, withdrawal, and
campaign recovery are safety exits that may append event 201. Take leaves room
for a checkpoint, progress observation, and safety exit. An exhausted chain cannot
start another ownership cycle.
`ownership-capacity-exhausted` requires a successor item.

Claim expiry, session end, lease expiry, node revocation, and unknown liveness
never transfer ownership automatically or remove a reserved path scope. Release
requires the current holder nonce and an enrolled, unrevoked holder node. Admin
and operator privileges cannot force release on noncampaign items. A successor item
addresses an exhausted event chain but cannot checkpoint over another item's
reserved paths, including a lost holder's scope. Noncampaign lost-holder recovery
remains unsupported.

Campaign members add an explicit operator recovery action as an intentional policy
addition required by campaign cancellation. Use the existing ownership POST with
`If-Match` naming the exact current ownership revision, `Idempotency-Key`, and:

```json
{"action":"recover","generation":1,"holder_quiesced":true,"evidence_ref":"receipt-quiescence"}
```

Only the existing trusted Hub admin bearer or a configured operator node bearer
may recover a campaign member. A request cannot supply its own operator status.
The generation must also match exactly. `holder_quiesced` must be literal JSON
`true`, and `evidence_ref` must pass the existing nonempty bounded private-data
reference validation. The operator must first establish that the holder/process
has stopped writing. The event records the authenticated actor, evidence, and
acknowledgement as `operator-reported-unverified`. The Hub has no proof that a
physical process was stopped. It never infers quiescence from expiry, cancellation,
revocation, or absent presence.

Recovery works independently of campaign eligibility and lease expiry. In one
transaction it appends `ownership-recovered`, increments generation, and clears
authority, checkpoint scope, and report. Old capabilities cannot write or release.
It retains the existing 201-event ceiling and never deletes history. An exact
replay returns its original result without clearing a newer holder or incrementing
again. Conflicting replay is refused. No real fleet recovery is performed by tests.

Atomic checkpoint path admission is implemented as described in the
[phase document](phase-worklore-path-scope-admission.md). Dependencies,
authoritative verification and review ingestion, GitHub merge reconciliation,
resource arbitration, delegated scope, bounded client detail propagation,
noncampaign lost-holder recovery, and issue completion require later slices.
