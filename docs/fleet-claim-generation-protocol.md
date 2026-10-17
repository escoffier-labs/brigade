# Proposed fleet claim generation protocol

## Status and boundary

The opt-in `/claims/v2` contract below remains a proposal. The first storage
prerequisite now implements offline schema25 to schema26 migration and shared
durable generations for the existing `/claims` writers. No v2 endpoint,
capability, client, epoch provisioning, or live activation ships in this slice.
The subsequent legacy fix adds optional generation comparisons and durable
retired-holder fences to `/claims`, without implementing this v2 proposal.
See [implemented legacy fencing](fleet-claim-fencing.md) for the current
compatibility contract, refusal reasons, and schema 34 migration. The storage
prerequisite and pinned-base descriptions below record the earlier boundary.

#1576 supplied schema25. This slice changes offline storage only and performs no
live reads or deployment. Version 0.28.0 and database schema metadata do not
establish support for v2. The historical implementation description below is
pinned to public base `c47ae4185c5d81b79ac91868b6e6989ac72a17ad`.

## Implemented legacy storage prerequisite

Schema26 adds `claim_fences(target, generation)` in the same SQLite authority as
`claims`. Migration preserves existing claim rows, including their private
holder and local-lock fields, and seeds each existing target at generation 1.
An unseen target has no durable fence and its inspect response omits the
`generation` field. All current rows have legacy provenance, reported as
`protocol: "legacy"`. No unused v2
origin or epoch columns are allocated. A later separate migration must add the
v2 row tag and immutable attempt provenance before mixed-protocol writes exist.

Every legacy ownership mutation uses one `BEGIN IMMEDIATE` transaction:

- New acquire and exact dead-lock supersede increment the target generation.
  A live same-node/same-holder retry extends TTL and keeps its generation and
  `acquired_at`. Renew retains the generation.
- An accepted holder, node, or force release increments the generation,
  including a missing release on a free target. The existing missing response
  remains `released: false`. Repeated missing legacy releases each advance the
  fence. Refused releases leave both row and fence unchanged.
- Acquire's global expiry pruning increments each retired target once before
  deletion. Reacquiring that target consumes another generation. Inspect and
  renew keep their legacy expiry behavior: inspect hides expired rows without
  retirement, and renew refuses an expired row without retirement.

Hub time is read after obtaining the transaction. The decision response is built
from that transaction's snapshot and committed before return. A subsequent
writer cannot substitute a later owner in a grant, renewal, or refusal response.
Claim payloads and listings expose the sanitized generation and legacy protocol.
Inspect also returns the durable stored generation when free. For an entirely
unseen target the field is absent, preserving the legacy response. Holder and
lock tokens remain private. Owned inspection retains the existing node-local
`lock_run_dir` recovery field.

Fences persist indefinitely, including free, renamed, and retired targets.
Distinct stored targets are capped at 100,000. Each newly inserted target is
limited to 1,024 UTF-8 bytes, including multibyte input. The byte limit applies
only to new fences: migration preserves longer legacy targets, and those targets
remain operable. Invalid UTF-8 returns HTTP 422. An overlong new target returns
HTTP 409 with `reason: "claim-target-too-long"`, `owner: {}`, and an
`invalid-field` error naming `target`, `limit: 1024`, and `unit: utf8-bytes`,
without echoing the submitted value.

A new target at capacity fails closed with structured HTTP 409 and the static
message `claim generation fence capacity exceeded`, while existing targets
remain usable. Migration exceeding the row bound refuses without changing the
old schema or its claims. Capacity, signed-64-bit exhaustion, byte-limit
rejection, and any pre-commit failure roll back the entire operation, including
expiry pruning. No fence is automatically purged or evicted to admit new targets.

At capacity, investigate enrolled-node activity and revoke an abusive node
through the supported operator workflow, `brigade fleet nodes revoke <node_id>`.
Preserve a backup and every fence while investigating. Revocation prevents that
node's future writes but does not free fence capacity. Recovery requires a
reviewed capacity increase, or a future verified epoch-reset procedure after
that authority is implemented. There is no destructive inline remediation or
automatic reset in this slice. The byte bound limits future target storage,
but an enrolled node can still consume the remaining row budget.

A fence must be an integer in `[1, 9223372036854775807]` once stored. Missing or
corrupt fences on existing rows refuse reads and mutations. This refusal can
reach the whole fleet: startup validation prevents the hub from starting,
claim listings fail in dashboards and routing occupancy, and a corrupt expired
claim can block acquisition of other targets during global pruning. The
`/claims`, authenticated HTML dashboard, and Command Deck readers return
structured, sanitized HTTP 503 with `reason: "claim-fence-refused"` for fence
refusal. Claim mutation and heartbeat paths return HTTP 409 with that reason
and `owner: {}`. SQLite lock and I/O errors
retain their SQLite error classification and existing startup retry policy.

Startup validates current-schema fencing state without recreating it. An
old-schema upgrade also preserves and validates any existing fence history,
without resetting generations or backfilling missing fences. Only an absent
fence table in an old schema is created and seeded from legacy claims. Deleted
current-schema fence storage is never silently rebuilt. Startup validation
also requires the exact supported fence table definition: a `target TEXT NOT
NULL PRIMARY KEY` with binary equality and a `generation INTEGER NOT NULL`
integer-range `CHECK`. A hand-built table of any other shape, or with extra
indexes or triggers, refuses without repair, recreation, or backfill.

A newer schema refuses startup. A claim request also re-checks the schema
version after it acquires its transaction, so a newer hub that migrates between
the first check and the write lock cannot be written to by this hub. The
following requests return a static HTTP 503 that discloses no database path
when the database is newer: the authenticated `GET` routes `/status`,
`/claims`, `/nodes`, `/cloud`, `/models`, `/preference`, `/sessions`, and the
`/policy` routes, the authenticated `POST` routes `/events`, `/claims`,
`/nodes`, `/cloud`, `/models`, `/grokbot`, `/sessions`, and `/policy`,
`PUT /preference`, the dashboard pages, and the Command Deck overview and
repository pages. The dashboard and deck pages map the error only after
authorization succeeds through an admin bearer token or a trusted Tailscale
identity. A request authorized only by a dashboard session cookie can still get
HTTP 401, because the cookie check opens the database and treats the refusal as
a failed authorization. Other routes, including enrollment, the
dashboard session API, the roster and policy form pages, observed work, the
work queue, and the `/work/` routes, are not mapped by this slice and still
return their generic database error.

Fence refusals on claim mutations and heartbeats use HTTP 409 with a reason
and an empty `owner` object. Read-only API GETs, dashboards, and Command Deck
use HTTP 503 with a reason. The empty `owner` object on terminal 409 refusals,
including `stale-holder`, `stale-generation`, `claim-fence-refused`, and
`claim-target-too-long`, keeps the base legacy client on its terminal `held`
path. Without it, that client treats an owner-less 409 as `missing`, retries,
and admits local-only work. Current clients preserve
`claim-fence-refused` as terminal. Upgrade clients before relying on capacity
fail-closed and verify the deployed versions. HTTP 503 for unsupported newer
schemas still permits legacy local fallback and provides no cross-machine
exclusion.

At the storage-prerequisite checkpoint, these generations alone did not fix delayed legacy acquire, same-holder ABA,
or backup restore and rollback. A missing release can advance the fence and a
later unfenced legacy POST can still acquire an orphan. A delayed legacy renew
or holder release can affect a later tenure with the same identities. Force
release retains its existing unfenced behavior. The storage cannot detect a
deleted fence for a free target or a restored older database without external
history authority. The epoch/restore contract, v2 provenance, cancellation,
capability, client admission, and original orphan-race acceptance remain open.
Older hub versions refuse schema26. Rollout and deployment remain separate work.

## Current implementation at the pinned base

At this base `fleet_hub_claims.py` does not exist. Arbitration is in
[`fleet_hub.py`](../src/brigade/fleet_hub.py), principally
`_validate_claim_request`, `handle_claim`, and `list_claims`:

- SQLite `claims` has one row per target, a private holder token, owner node,
  timestamps, TTL, and local-lock provenance. It has no claim generation or
  durable release tombstone. Public claim payloads omit holder and lock tokens.
- Acquire uses `BEGIN IMMEDIATE`, prunes expired rows, then upserts. A live retry
  with the same node and holder preserves `acquired_at` but extends TTL. An
  exact same-node dead-lock supersede can replace another holder's row.
- Renew matches target, node, holder, and live expiry. Holder release matches
  target, node, and holder. Token-less node release requires inspected
  `acquired_at`. Force release can omit that fence. A missing release leaves no
  durable barrier to a later acquire.
- Inspect treats expired rows as free without deleting them. Listing hides
  expired rows unless requested. Acquire pruning currently deletes expired rows
  without preserving fencing state.

[`fleet_hub_http.py`](../src/brigade/fleet_hub_http.py) routes POST `/claims`,
binds a node token to the request node, and refuses admin claim writes unless
the existing explicit admin-write setting permits them. It has no `/claims/v2`
route. This proposal does not change enrollment or claim authority.

[`fleet_client.py`](../src/brigade/fleet_client.py) sends `/claims` requests via
`_claim_op`. `repo_claim` retries initial uncertainty with the same holder,
permits local-lock fallback for transport failure, and can reacquire after a
missing renew. Cleanup uses bounded joins and spaced releases.
[`fleet_claim_lifecycle.py`](../src/brigade/fleet_claim_lifecycle.py) retries
missing orphan releases within an uncertainty window and keeps confirmed local
authority from reviving after its deadline. These client deadlines do not bound
when an abandoned network request can reach or commit at the Hub. A release can
finish before a delayed acquire creates a fresh orphan for its TTL.

[`cli/fleet.py`](../src/brigade/cli/fleet.py) supports holder operations and
dead-lock inspection for token-less node recovery. Its force path skips that
proof. Claim listing exposes no generation. Repository claim admission is in
[`cli/run.py`](../src/brigade/cli/run.py): it enters the local `run_lock` before
`repo_claim` and passes dead-owner provenance and loss callbacks. The model-lease
and session handling in
[`aboyeur/orchestrator.py`](../src/brigade/aboyeur/orchestrator.py) supplies no
repository-claim generation fence. This design does not change those systems.

## Proposed shared authority and request contract

Keep one authoritative SQLite claim row per target. Add durable per-target fence
metadata in that same authority, plus v2 provenance on a tagged claim row. These
are claim lifecycle records, not an independent task registry or daemon.

Let `E` be the verified Hub restore epoch and `g` the current target generation.
Within `E`, generations never decrease or repeat, including while the target is
free. A new target starts free at `g=0`. A new tenure increments `g` once, and its
tenure generation `t` equals that new value. Release, retirement of expiry, and
cancellation of an uncommitted attempt also consume a generation. Renew does not.
Missing or corrupt fences on an existing target cause refusal, not recreation at
zero. Absent state cannot be assumed new when storage history is unverified.
Overflow refuses a mutation rather than wrapping or deleting the fence.

A v2 acquire attempt is the immutable tuple
`A=(E, target, expected_generation, authenticated_node, holder, request_id)`.
Proposed field bounds are a target of at most 1024 UTF-8 bytes, node and holder
using the current 128-character identity grammar, a 32-character lowercase hex
`request_id`, and a nonempty epoch identifier of at most 128 ASCII characters.
Generations are nonnegative signed-64-bit integers, never booleans. These are
proposed v2 validation rules, not changed legacy validation. Epoch issuance
authority remains an operator decision below.

The client creates a fresh attempt ID before sending, retains `A` across retries
and cancellation, and does not reuse it for a new attempt. The v2 row stores
`E`, protocol tag, `t`, the complete origin `A`, and immutable normalized acquire
arguments, including TTL and supplied lock/label metadata. The same attempt with
changed arguments is `attempt-mismatch`. Holder equality alone never identifies
a retry. No holder or lock secret becomes public listing metadata.

Fence state is the durable cancellation tombstone. With the comparison rules
below, it needs no unbounded separate per-request history: `g > A.expected+1`
proves this attempt cannot be live or reacquire, while a row at `A.expected+1`
must carry its exact origin to be cancellable. If exact historical cancellation
receipts are later required, their retention is a separate reviewed requirement.
The proposed response acknowledges terminal cleanup, not that an acquire was
never committed.

## Serialization and operations

After authentication, validation, and epoch verification, serialize each affected
target's fence, row, expiry retirement, decision, and mutation in one SQLite
`BEGIN IMMEDIATE` transaction. Build the response from that transaction's
snapshot, then commit before sending it. Errors before commit must roll back the
entire transition. An uncertain commit or lost response after commit has an
unknown outcome, not presumed rollback.
Lock contention must return a bounded unavailable result, never admission.

Read Hub time after obtaining the write lock. Inspect, acquire, and renew first
retire a row whose Hub expiry is at or before that time: delete it and increment
its fence atomically, once. Release and cancellation end only their exact matching
tenure, including an expired but unretired row. They must not normalize expiry of
another tenure. No retry may renew or recreate a retired tenure. Inspection
returns the resulting Hub-owned `E`, `g`, protocol tag, and sanitized live row,
even when free. Inspection can therefore persist expiry retirement and initial
fence creation. Capability discovery must be read-only and separate.

All outcomes below are proposed semantic codes. A later implementation must bind
them to a reviewed wire response shape without reusing ambiguous legacy
`missing`/transport fallback handling.

| Operation | In-transaction precondition and outcome |
| --- | --- |
| Inspect | Verify epoch, normalize expiry, return authoritative fence and live state. A snapshot is advisory until acquire's CAS. |
| Acquire `A` | If free and `g=A.expected`, increment to `t=g+1` and create exactly one tagged v2 row with origin `A`. Return that tenure. |
| Acquire retry | Only the exact origin, node, holder, and arguments on the current live row at `t=A.expected+1` return the same tenure. Do not extend TTL. Use renew for extension. |
| Acquire collision | With a different live row at the expected fence, return `held`. A changed fence returns terminal `stale-generation`. Never overwrite legacy or v2 ownership. |
| Renew | Require v2 tag, exact epoch, target, owner node, holder, and `t=g` on a live row. Extend TTL atomically without changing generation. Missing, retired, or mismatched tenure is terminal. |
| Release, every scope | Require v2 tag, exact epoch, target, holder, and tenure generation, plus existing scope authorization. Holder and node scopes also match owner node. Delete that row and increment `g` atomically. Force never removes the generation/holder condition. |
| Release retry | After deletion, a free or newer state yields terminal `already-ended`/`stale-generation` without mutation. Never use missing release to delete a later tenure. |

An acquire retry must check exact live provenance before returning success despite
its already-consumed expected generation. Any other stale acquire is refused.
Same-holder reacquire after release or expiry requires fresh inspect, a new
attempt ID, and a new generation. V2 does not inherit the current timestamp-based
dead-lock supersede. Recovery must end an exact tenure before a new CAS acquire.

Two conductors inspecting free `g=k` can both send acquire. The first serialized
commit creates tenure `k+1`. The second receives `stale-generation`, including
when it happens to reuse the same holder with a different attempt ID. If renew
serializes before expiry and observes a live row, it can extend it. If retirement
wins, renew is refused. Release before renew makes renew stale. Renew before
release cannot prevent the exact release. Responses never describe a different
owner read after commit.

### Unknown-outcome cancellation

Proposed `cancel_acquire(A)` requires the full immutable origin: epoch, target,
expected generation, authenticated node, holder, and request ID. The authenticated
caller must equal `A.authenticated_node`. Validate that identity and the full
origin before any mutation. After epoch validation, without retiring unrelated
expiry:

1. If the target is free at exactly `g=A.expected`, increment once and commit a
   free tombstone. The delayed original CAS and every other in-flight acquire
   based on that generation become stale, even with a different holder or
   request ID. Those clients must inspect again and create fresh attempts.
2. If the v2 row is exactly tenure `A.expected+1` with matching full origin
   `A` and holder, delete it and increment once. This covers an acquire committed
   before its response was lost.
3. If neither case applies to an otherwise valid state, acknowledge
   `cancel-resolved` without mutation. Newer tenures, legacy rows, and
   different-holder or different-attempt rows remain untouched, even if expired.
   A future expected generation (`A.expected > g`) is invalid, not cleanup success.

Case 1 provides generation-scoped invalidation. The Hub has no record of unseen
requests, so it cannot selectively invalidate only `A`. Exact unseen-attempt
isolation would require a separate durable attempt-registration design, which
this proposal does not provide. Case 2 remains exact and selective to the
committed matching tenure. Neither case cancels an already committed unrelated
or newer tenure.

A live row at `g=A.expected` also remains untouched: it blocks the original
acquire now, and ending it must advance the fence before acquire can succeed.

Cancellation retries cannot meet either mutating condition again: case 1 leaves
free `g=A.expected+1`, and case 2 leaves free `g=A.expected+2`. A new tenure from
either free state has a different origin and generation. Repeated cancellation
therefore does not consume more generations or cancel same-holder reacquisition.
The durable fence and live origin encode the one-time cancellation provenance.

Cancellation arriving before acquire fences the original. Acquire arriving first
can create only the exact tenure that cancellation removes. A lost cancellation
reply is retried with `A`, never reconstructed from a current holder lookup.
If cancellation cannot reach the Hub, the client reports unresolved cleanup and
refuses guarded v2 admission. Protocol fencing cannot guarantee cleanup without a
committed Hub operation. A late grant received after local abandonment cannot
restore local authority. It triggers cancellation of `A`.

## Legacy coexistence is partial protection

The proposed Hub must update legacy paths as well as adding v2. Existing rows
remain tagged legacy. Do not silently convert a live legacy tenure into v2.

- Legacy acquire/renew may act on legacy rows only. A free target can receive a
  legacy new tenure, which increments the shared fence. Same-live-holder legacy
  retry/renew retains that generation. Exact legacy supersede consumes a new
  generation.
- Legacy holder, node, and force release must refuse a tagged v2 row with
  `protocol-mismatch` and no row or fence mutation, even with matching holder or
  `acquired_at`. Legacy acquire/renew also refuse that row. This restriction
  lasts through an expired but unretired v2 row. Only shared Hub retirement or a
  v2 operation normalizes its expiry, after which legacy may acquire the free
  target again. A permanent target-only-v2 policy is not proposed here.
- An accepted legacy release increments the shared fence, including a missing
  release on a free target. Refused deletes of another tenure do not. Every
  legacy expiry retirement increments the fence once, atomically with deletion.
  Existing global pruning must preserve each affected target's fence.
- Shared retirement of expired v2 rows is Hub lifecycle maintenance, never an
  unfenced legacy release. Cross-version refusals must be checked before legacy
  cleanup paths can delete a v2 row.

These cross-version refusals change legacy behavior only while a target has a
tagged v2 tenure. Compatibility therefore needs explicit documentation and
operator approval for rollout. Every writer and pruning path must participate
before capability can advertise v2. An older writer bypassing this contract is
incompatible with an active v2 authority.

For a free target at `k`, legacy acquire winning first increments to `k+1`, so
v2 acquire expecting `k` is stale. V2 winning first creates a tagged row, so
legacy acquire is refused. Legacy release winning first on a free target also
invalidates expected `k`. A delayed legacy acquire after v2 cancellation may
still create an unprotected legacy orphan. Its fence advancement protects stale
v2 requests only. It does not fix the original legacy race or extend v2 cleanup
to legacy attempts.

At the pinned base, legacy requests carry no generation, so shared fence advancement does not make
legacy-to-legacy mutations tenure-specific. A delayed renew can extend a later
live legacy row with the same target, node, and holder. A delayed holder release
can delete a later row with those same identities. Force release without
`acquired_at` can delete whichever legacy row holds the target when it executes.
Node release still requires matching owner node and inspected `acquired_at`.
It can affect a later legacy row only if those legacy conditions also match,
including a reused timestamp. These matching conditions remain the legacy
boundary. The proposed shared fence protects v2 expected-generation requests,
and tag refusals protect v2 rows, but neither solves these legacy exposures or
late-acquire orphans.

## Retention, restore, and capability decisions

Keep per-target fences/tombstones indefinitely within an epoch, including free
targets and renamed or retired target identities. Deleting one and recreating
the same identity at zero permits ABA: old requests can match reused state.
Client timestamps, timeout duration, TTL, and local lock stamps are not evidence
that a delayed request has expired. Generation arithmetic must reserve sufficient
range for each transition and refuse exhaustion without state changes.

Backup restore, database rollback, or downgrade can resurrect an old fence and
its live origin. An epoch stored only in the restored database cannot prove a
restore did not occur. Before enabling v2, the operator must choose epoch
authority outside that rollback domain, restore detection, and a runbook that
quiesces writers, invalidates old epochs/tenures, and resumes only after verified
authority. Old-epoch requests then fail `stale-epoch`. If authority is missing,
unverified, or incompatible, refuse all v2 mutations as `epoch-unverified` and
advertise no usable capability. Do not guess a replacement epoch from wall time.
Offline fixtures may supply an explicit test epoch. No production epoch scheme
or restore safety is established by this design.

Finite retention is an unresolved alternative: it would require a reviewed
Hub-issued bounded-expiry token or epoch contract, its issuance/key authority,
validation and clock assumptions, and restore behavior. Until those decisions
exist, deleting fences by age is unsafe. This proposal promises no cryptographic
scheme or implicit request lifetime.

A future client must explicitly request v2 and discover a versioned capability
that identifies the exact protocol, supported operations including cancellation,
response contract, and verified epoch. Discovery location and wire spelling are
pending implementation review. An old-Hub 404, absent/malformed capability,
unknown version, unverified epoch, or unavailable discovery refuses admission
before any acquire POST. There is no fallback to `/claims`, local-only admission,
or a legacy recovery write for a v2 request. A valid preflight cannot replace
server-side version/epoch validation on every subsequent operation. An unexpected
v2 response is terminal or unknown-outcome cleanup, never downgrade permission.

## Proposed fixture matrix and implementation acceptance

These tests are proposed and have not been executed by this documentation task.
Use fake identities, separate SQLite connections, controlled Hub time, explicit
commit barriers, and delayed transport completions rather than sleep thresholds.
Assert both response and authoritative row/fence after each interleaving.

| Fixture | Required evidence |
| --- | --- |
| Original POST delayed beyond timeout/cancel | Cancel free `k` to `k+1`, then deliver original acquire expecting `k`. It is stale, no row exists, fence stays `k+1`. |
| Unseen cancellation versus competing attempt | Delay unseen `A` and competing `B` after both inspect free `k`, using different holders and request IDs. Cancel `A` to `k+1`. Both pending acquires are stale without row or fence mutation. `B` must inspect `k+1` and create a fresh attempt to acquire tenure `k+2`, which survives replayed cancel `A`. If `B` commits at `k+1` before cancel `A`, its different origin remains untouched. |
| Unknown outcome after acquire commit | Commit `A` at `k+1`, drop response, then cancel `A` both while live and after exact tenure expiry without retirement. In both cases the matching row is removed, fence becomes `k+2` once, and replay does not advance it again. Late reply cannot admit work. |
| Unknown outcome after cancel commit | Drop cancel response, replay cancel and original POST. No extra fence advance and no resurrection. |
| Same-holder retry/reacquire | Exact live retry returns identical tenure without TTL extension. Different attempt or changed arguments fails. Fresh acquire after ending tenure survives old cancel/renew/release. |
| Simultaneous conductors | Two connections inspect `k`, race CAS, and yield one tenure only. Include same-holder/different-attempt collision. |
| Expiry versus renew/acquire | Retirement at expiry increments once before stale comparison. Renew before expiry can extend. Retired tenure never revives through retry. |
| Release versus renew | Both transaction orders yield the exact release and eventual stale renew. Response owner comes from the decision snapshot. |
| Stale node/force recovery | Every v2 scope with stale holder or tenure leaves the newer row/fence unchanged, including expired unretired rows. Missing exact credentials refuses recovery. |
| Legacy/v2 collision and refusal | Both acquire orders, legacy supersede, holder/node/force delete, matching secret, and expired unretired v2 row obey tag refusal. |
| Legacy fence advancement | New tenure, missing/positive release, supersede, and expiry advance fences. Replay delayed legacy acquire: orphan remains possible, stale v2 CAS fails. |
| Legacy delayed mutation limits | Delay renew and holder release from an ended legacy tenure, then create a later live legacy row with the same target/node/holder. Renew can extend it without changing its generation, and holder release can delete it and advance the fence. Separately replay force release without `acquired_at` against a later legacy row: it can delete that row and advance the fence. Node release refuses a changed inspected timestamp but can delete a later same-node row if the timestamp is reused. Assert these remaining exposures and that stale v2 CAS fails after fence advancement. |
| Cancellation provenance | Refuse an authenticated caller differing from `A.authenticated_node` or an incomplete origin without mutation. Cancel `A` against a different committed holder/attempt or a newer same-holder row without mutation, including expired unretired rows. Future expected generation is invalid. All replay orders remain idempotent. |
| Crash/commit failure | Rollback leaves row and fence together. Failure after commit is resolved by exact retry/cancel, never speculative reacquisition. |
| Retention and restore epochs | Long-delayed requests after retirement fail. Restore of old state refuses until verified new epoch. Old epoch and generation exhaustion produce zero mutations. |
| Old Hub and malformed capability | Fake legacy-only server returns 404, missing/malformed/unsupported capability, or misleading 0.28.0/schema metadata. Observe zero claim POSTs, zero claim writes, and refused admission. |

Future acceptance requires this matrix, malformed-field/authentication checks,
sanitized protocol/epoch/generation listing, and proof that every legacy writer
and expiry path uses the shared fence transaction. Demonstrate unknown-outcome
cleanup independently of any fixed network-delay window. Verify no v2 downgrade,
v2 same-holder ABA, cross-version deletion, or post-commit response substitution.
Implementation gates and independent current-diff review remain required.
Production acceptance also requires the operator's epoch/restore contract and
rollout decision. None of this acceptance is completed here, including native
Windows behavior.

## Ownership and dependency order

1. #1576 owns offline schema25 and lands first. Its migration and live deployment
   are outside this slice. Then review storage for shared fences, row tagging,
   provenance, and the separately allocated future migration.
2. The #1189 Hub implementation owner covers current `fleet_hub.py` arbitration
   and `fleet_hub_http.py` routing, all legacy paths, and offline SQLite fixtures
   in `tests/test_fleet_claims.py`. If `fleet_hub_claims.py` has been extracted by
   then, rebase ownership onto that module instead of duplicating authority.
3. After the Hub contract, the client owner covers `fleet_client.py`,
   `fleet_claim_lifecycle.py`, cancellation by `A` and fail-closed admission,
   with `tests/test_fleet_client_hardening.py` and relevant claim tests. CLI/run
   ownership covers `cli/fleet.py`, `cli/run.py`, generation display and exact
   recovery, with `tests/test_fleet_claim_release.py` and display fixtures.
4. The accountable parent owns independent review, publication qualification,
   PR gates, and merge verification. A docs-only merge is a design checkpoint.
   Keep #1189 and #1121 open for their remaining implementation/acceptance.

Pending operator choices are mandatory new-client default versus continued
opt-in, minimum supported client version, production rollout/enforcement,
cross-version recovery restrictions, holder custody for exact node/force
recovery, and restore epoch/runbook authority. Without exact recovery credentials,
v2 recovery must refuse and allow expiry, never expose holder secrets in public
inspection. An offline opt-in implementation can prove the fixture contract
while those choices remain pending. It cannot claim fleet-wide protection.

Dot #1609 Worklore/Deck, #1219 worker-process cancellation, model-lease parsing,
and #1238 native enumeration retain their existing owners and scope.
