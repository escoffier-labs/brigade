# Worklore checkpoint path admission

Refs #1595. A fenced checkpoint declares its full current repository path scope.
Admission uses the existing SQLite ownership transaction and immutable events.

## Transaction contract

Authenticated historical replay returns the recorded ownership revision before
admission. It does not reacquire scope or change an older `not-performed` marker.
New checkpoints pass the existing authority, generation, revision, item-state
and event-capacity checks before comparing other items' latest ownership events.
The comparison and checkpoint insertion share `BEGIN IMMEDIATE`.

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

`worklore_scope.py` selects the latest ownership event per other work item,
excluding unrelated event types. The query returns at most 1,025 snapshots,
including an overflow sentinel. At most 1,024 snapshots are assessed. Each
snapshot is read as at most 65,537 bytes, including a byte overflow sentinel.
Existing scope bounds allow 32 paths of 256 characters per item. The query bounds
application processing and decoded data. SQLite may traverse additional index
entries to group and select the latest events.

HTTP 409 refusals use fixed messages and resolution codes:

- `path-scope-conflict` includes validated work IDs, owner nodes, requested paths
  and held paths. Each record carries `replace-scope-or-release-holder`. The
  conflict list contains at most 8 records and 7,168 escaped JSON bytes, keeping
  the response under 8 KiB. `truncated` reports omitted conflicting pairs.
- `path-scope-capacity-exhausted` reports that the latest-snapshot set exceeds the
  scan bound. Its resolution is `reduce-snapshot-set-or-retry-after-scope-review`.
  Releasing scope does not remove an item's historical snapshot from this count.
- `path-scope-unavailable` refuses unsafe, malformed or oversized stored
  declarations. Its resolution is `review-stored-scope-declarations`.

Refusals do not return holder hashes, capabilities, grants, checkpoint prose or
unrelated item fields. Stored identities and paths are validated before they
enter conflict output. An incomplete assessment never admits a writer.
Resolution codes describe an end state for ownership review. They do not
instruct callers to release another holder automatically.

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
qualification remain outside this slice. Existing imported duplicates can
continue to exist until explicitly reconciled. No schema, migrations, runtime
dependencies, provider changes or verification tooling changes are introduced.

## Verification ownership

Regression tests exercise the real authenticated HTTP handler and SQLite store
in `tests/test_worklore_scope.py`. Separate SQLite connections contend on expansion
and acquisition. The focused gate includes ownership, HTTP, client, ownership CLI,
Dot and module-size tests. Root owns independent review, the full gate, CI,
publication and merge.
