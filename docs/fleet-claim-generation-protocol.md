# Proposed fleet claim generation protocol

## Status and boundary

This is an offline, opt-in protocol design for #1189, grounded in public base
`c47ae4185c5d81b79ac91868b6e6989ac72a17ad`. It documents proposed `/claims/v2`
semantics. No endpoint, schema change, client support, or live activation ships
with this document. The legacy `/claims` unknown-outcome/late-acquire orphan race
remains unprotected. Neither #1189 nor parent #1121 is complete.

The operator-supplied deployment boundary is live Hub schema22 and public main
schema24. This task performs no live reads. #1576 alone owns the next offline
schema25 slice. This design allocates no migration number and requires a later,
separately approved storage change after #1576. Version 0.28.0 and database schema
metadata do not establish support for this protocol.

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

Proposed `cancel_acquire(expected_generation, holder)` must also carry the full
attempt `A`, including `request_id`. It uses the originating authenticated node,
not a holder-only operator shortcut. After epoch validation, without retiring
unrelated expiry:

1. If the target is free at exactly `g=A.expected`, increment once and commit a
   free tombstone. The delayed original CAS can no longer succeed.
2. If the v2 row is exactly tenure `A.expected+1` with matching full origin
   `A` and holder, delete it and increment once. This covers an acquire committed
   before its response was lost.
3. If neither case applies to an otherwise valid state, acknowledge
   `cancel-resolved` without mutation. Newer tenures, legacy rows, and
   different-holder or different-attempt rows remain untouched, even if expired.
   A future
   expected generation (`A.expected > g`) is invalid, not cleanup success.

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
| Unknown outcome after acquire commit | Commit `A` at `k+1`, drop response, cancel `A`. Row is removed, fence becomes `k+2`. Late reply cannot admit work. |
| Unknown outcome after cancel commit | Drop cancel response, replay cancel and original POST. No extra fence advance and no resurrection. |
| Same-holder retry/reacquire | Exact live retry returns identical tenure without TTL extension. Different attempt or changed arguments fails. Fresh acquire after ending tenure survives old cancel/renew/release. |
| Simultaneous conductors | Two connections inspect `k`, race CAS, and yield one tenure only. Include same-holder/different-attempt collision. |
| Expiry versus renew/acquire | Retirement at expiry increments once before stale comparison. Renew before expiry can extend. Retired tenure never revives through retry. |
| Release versus renew | Both transaction orders yield the exact release and eventual stale renew. Response owner comes from the decision snapshot. |
| Stale node/force recovery | Every v2 scope with stale holder or tenure leaves the newer row/fence unchanged, including expired unretired rows. Missing exact credentials refuses recovery. |
| Legacy/v2 collision and refusal | Both acquire orders, legacy supersede, holder/node/force delete, matching secret, and expired unretired v2 row obey tag refusal. |
| Legacy fence advancement | New tenure, missing/positive release, supersede, and expiry advance fences. Replay delayed legacy acquire: orphan remains possible, stale v2 CAS fails. |
| Cancellation provenance | Cancel a different holder/attempt or a newer same-holder row without mutation, including expired unretired rows. Future expected generation is invalid. All replay orders remain idempotent. |
| Crash/commit failure | Rollback leaves row and fence together. Failure after commit is resolved by exact retry/cancel, never speculative reacquisition. |
| Retention and restore epochs | Long-delayed requests after retirement fail. Restore of old state refuses until verified new epoch. Old epoch and generation exhaustion produce zero mutations. |
| Old Hub and malformed capability | Fake legacy-only server returns 404, missing/malformed/unsupported capability, or misleading 0.28.0/schema metadata. Observe zero claim POSTs, zero claim writes, and refused admission. |

Future acceptance requires this matrix, malformed-field/authentication checks,
sanitized protocol/epoch/generation listing, and proof that every legacy writer
and expiry path uses the shared fence transaction. Demonstrate unknown-outcome
cleanup independently of any fixed network-delay window. Verify no downgrade,
same-holder ABA, cross-version deletion, or post-commit response substitution.
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
   `fleet_claim_lifecycle.py`, exact attempt cleanup and fail-closed admission,
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
