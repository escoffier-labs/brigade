# Worklore checkpoint path admission

Status (2026-10-17): shipped in #1701 (64d4c4da6).

Refs #1595. A fenced checkpoint declares its full current repository path scope.
Admission uses the existing SQLite ownership transaction and immutable events.

## Transaction contract

Authenticated historical replay returns the recorded ownership revision before
admission. It does not reacquire scope or change an older `not-performed` marker.
New checkpoints pass the existing authority, generation, revision, item-state,
event-capacity and serialized byte-size checks before comparing other items'
latest ownership events. An oversized event returns `field-bound` without
conflict details or an inserted event, even if its paths overlap. Admission runs
immediately before insertion. Both share `BEGIN IMMEDIATE`.

Repository identifiers compare case-insensitively while retaining stored
spelling. Path components compare case-sensitively. Identical paths and
ancestor/descendant paths overlap. `src/a` and `src/ab` remain disjoint.
Different repositories and disjoint paths can proceed in parallel. A checkpoint
replaces the item's entire previous declaration. A refused replacement preserves
its old scope, revision and event count. Empty scope reserves no path and skips
writer admission.

Owned and handoff-pending scopes remain reserved across terminal item states,
handoff acceptance, node revocation and absent or expired presence. Reservations
also include pre-upgrade declarations marked `not-performed`. Explicit scope
replacement, release or an existing valid ownership exit removes a declaration.
Admission does not evict or reconcile imported duplicates.

## Refusal and processing bounds

`worklore_scope.py` groups all ownership events to select the latest event per
other work item, excluding unrelated event types. The outer query retains only
accepted, checkpoint, reported and handoff-offered events before ordering and
limiting results. Latest offers, releases and withdrawals consume no scan slot,
and their earlier reservations do not reappear. The query returns at most 1,025
retained snapshots, including an overflow sentinel. At most 1,024 are assessed. Each
snapshot is read as at most 65,537 bytes, including a byte overflow sentinel.
Existing scope bounds allow 32 paths of 256 characters per item. The query bounds
application processing and decoded data. SQLite may traverse additional index
entries to group and select the latest events.

The scan cap is hub-wide: every other item's latest unreleased owned or
handoff-pending event counts, including empty scopes and holders in other
repositories. Lost holder nonces retain those slots, and supported recovery for
lost nonces remains pending. Holder release frees a slot, and departed histories
no longer count.

HTTP 409 refusals use fixed messages and resolution codes:

- `path-scope-conflict` includes validated work IDs, owner nodes, requested paths
  and held paths. Each record carries `replace-scope-or-release-holder`. The
  conflict list contains at most 8 records and 7,168 escaped JSON bytes, keeping
  the response under 8 KiB. `truncated` reports omitted conflicting pairs.
- `path-scope-capacity-exhausted` reports that the latest-snapshot set exceeds the
  scan bound. Its resolution is `reduce-snapshot-set-or-retry-after-scope-review`.
  Explicit holder release removes that item from the scan count while preserving
  its immutable history. Terminal item states and revoked nodes remain counted
  when their latest ownership events retain reservations.
- `path-scope-unavailable` refuses unsafe, malformed or oversized stored
  declarations. Its resolution is `review-stored-scope-declarations`.

Refusals do not return holder hashes, capabilities, grants, checkpoint prose or
unrelated item fields. Stored identities and paths are validated before they
enter conflict output. For nonempty peer scope, safe repository identity is
validated and compared before paths. Malformed paths in a reliably different
repository do not block admission here. Malformed JSON, unknown or unsafe
repository identity, and unsafe declarations in the same repository still
refuse admission. Stored action must agree with the event type. Accept,
checkpoint and report events must retain owned state, and handoff events must
retain handoff-pending state. Their owner must match the stored authenticated
node principal. These checks and canonical lowercase SHA256 digest validation
also apply to empty scopes and holders in other repositories. Inconsistent
active events refuse admission rather than freeing a reservation.
Future validator changes must preserve compatibility with legitimately persisted
ownership events or use a reviewed versioned migration.

Admission collects canonical digests from every validated active holder before
empty-scope and other-repository shortcuts. The ownership caller also supplies
the trusted current holder digest after its existing fences, covering the item
excluded from the peer query. This set contains at most 1,024 peer digests plus
one current holder digest. It requires no additional history query or cache.

Conflict records remain private until the complete bounded scan finishes. Count
and escaped-byte bounds apply before a record enters this buffer. Then every
distinct work ID, owner node, requested path and held path in the buffered output
passes the private-data guard against the full digest set. Conflict output cannot
reflect another active holder's capability even when that holder appears later
in scan order, has empty scope or holds paths in another repository. The guard
refuses case-equivalent holder digests and embedded 32-byte nonce candidates
whose SHA256 matches any active digest, including canonical base64url, standard
base64 and hex spellings. Each distinct bounded output string is scanned once.
Each candidate is decoded and hashed once before set membership comparison,
independent of holder count. Authentication and CAS keep their existing checks.
Omitted records and disjoint paths require no reflection scanning. Generic
active-event and scope validation still applies before overlap comparison, even
after the output budget is exhausted. Malformed data or a reflection failure
returns the fixed `path-scope-unavailable` refusal without buffered output. An
incomplete assessment never admits a writer.

This guard covers conflict responses. The existing ownership GET read policy
still returns stored scope metadata. Rejecting foreign holder capabilities at
metadata ingestion and addressing previously stored values remain follow-up
work under #1595.

Resolution codes describe an end state for ownership review. They do not
instruct callers to release another holder automatically.

These structured details are supported through raw HTTP. The existing Python
client exposes the refusal's error code and sanitized message, discarding
`conflicts`, `resolution` and `truncated`. Bounded client propagation remains
epic work.

## Historical observation and limits

Successful new checkpoints record `conflict_check=path-admission`. Subsequent
handoff and report revisions retain that checkpoint's marker. The ownership CLI
accepts and displays `path-admission` and `not-performed`, defaulting to the latter
for older responses. Unknown markers produce a sanitized invalid-response
refusal. Liveness stays `unknown`, and reads retain `consistency=separate-reads`.
This supersedes the always-unchecked marker described by the earlier ownership
read-view slice.

Admission records a Hub declaration check at an ownership revision. Physical
filesystem writers, process cessation, provider readiness, dependencies, merge
reconciliation, delegated scope, resource arbitration and three-owner execution
qualification remain outside this slice. Operator-reviewed reservation recovery
for a lost holder capability also remains epic work: release requires the
current nonce and an enrolled, unrevoked holder node. Admin and operator
privileges cannot force release. A successor item cannot checkpoint over a lost
holder's reserved paths. Existing imported duplicates can continue to exist
until explicitly reconciled. No schema, migrations, runtime
dependencies, provider changes or verification tooling changes are introduced.

## Verification ownership

Regression tests exercise the real authenticated HTTP handler and SQLite store
in `tests/test_worklore_scope.py`. Separate SQLite connections contend on expansion
and acquisition. The focused gate includes ownership, HTTP, client, ownership CLI,
Dot and module-size tests. Root owns independent review, the full gate, CI,
publication and merge.
