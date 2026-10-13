# Managed Claude review history

`brigade.claude_review_history` stores sanitized GitHub review observations in a
caller-owned SQLite connection. It extends the offline collector in
[`claude_review_import.py`](../src/brigade/claude_review_import.py). The caller
supplies the GET transport, trusted repository and app identity, exact PR head,
and observation time.

## API

```python
from brigade.claude_review_history import (
    ensure_schema,
    ingest_managed_review,
    read_managed_review_history,
)

subject = dict(
    owner="example",
    repo="project",
    pr_number=7,
    expected_app_id=123,
    expected_app_slug="example-review",
)
observation = ingest_managed_review(
    conn,
    transport,
    **subject,
    current_head="a" * 40,
    observed_at="2026-01-03T00:00:00Z",
)
conn.commit()
history = read_managed_review_history(
    conn, **subject, current_head="a" * 40, limit=20
)
```

`ensure_schema(conn)` creates `brigade_managed_review_history` and its subject
index. It leaves other tables, `user_version`, connection settings and pending
caller changes intact. Ingestion opens a transaction before schema creation
when the connection is idle and leaves commit or rollback to the caller.
Caller rollback also removes schema created by ingestion. Existing transactions
remain open. A savepoint protects the append and quota checks without committing
unrelated changes. Normal append rejection rolls back only the savepoint and
preserves pending caller changes. The caller must still commit or roll back
that transaction, including one opened by ingestion. SQLite errors that
automatically abort the whole transaction, such as a trigger's `RAISE(ROLLBACK)`,
also roll back pending caller changes and remove the savepoint. Ingestion skips
savepoint cleanup when SQLite has ended the transaction and propagates the
original error.

`ingest_managed_review` calls `collect_managed_review` itself. It accepts the
collector's optional `Limits` instance, never an arbitrary snapshot. Only the
collector's sanitized JSON enters storage. Its result contains `observation_id`
and `duplicate`. The observation ID is a SHA-256 digest of canonical JSON,
including normalized observation time. Exact replay keeps the existing row.
Different timestamps or sanitized revisions produce separate snapshots.
Repository identity is case-folded for collection, storage and reads. Source
URL validation compares the trusted owner/repository path prefix without case
sensitivity and preserves the original validated URL. Resource paths, PR and
object IDs, fragments and the `github.com` host retain exact matching. App ID
and slug are both part of the subject.

Writes refuse snapshots above 256 KiB, a 201st observation for a subject, or a
20,001st observation globally. Replay remains allowed at the quotas. Quota
probes read bounded indexed rows under SQLite's writer lock. No write prunes
old observations. The caller controls database retention and lock contention.

## JSON and coverage

The read result has `history`, `history_truncated`, `checks`, `annotations`,
`reviews`, `comments`, and `latest_check_enumeration_complete`, together with
repository, PR, app and caller-head metadata. Each snapshot includes its
`observation_id`, original collection head, normalized observation time,
sanitized objects, source completeness, usage and collection limits. Object
coverage and current finding flags are rebound to the read caller's head in
both snapshots and the projection. Stored severity counts remain available
when a historical flag becomes false.

Reads never create schema or change the caller's transaction state. A bounded
`sqlite_master` probe checks whether the history table exists. When it is
absent, reads return the same empty history and projection as an unknown
subject, including on a read-only connection. Both completeness and truncation
flags are false. The reader uses its own tuple cursor and preserves caller row
and text factories, existing cursors, transaction state and database encoding.
For fetched observation IDs and payloads, `bytes`, `bytearray` and `memoryview`
are decoded as UTF-8, while `str` values remain unchanged. SQLite supplies UTF-8
bytes to text factories when fetching TEXT, including from UTF-16 databases.
Arbitrary global TEXT converters remain unsupported.

Reads fetch at most `limit + 1` rows for the exact subject. The default limit is
20, and valid limits are integers from 1 through 100. An extra row sets
`history_truncated`. A truncated read cannot establish current known or zero
findings or round supersession. Every projected and rebound historical check
has `supersession_disposition: unknown` and `superseded_by: null` in that case.
Loaded findings and reviewed-head coverage remain available. An omitted
observation may contain a round whose start time falls between or after loaded
rounds. Observation order uses fixed microsecond UTC keys, including when
whole-second and fractional timestamps are mixed. Completion time, insertion
order and provider numeric IDs do not establish observation order.

The projection selects the latest loaded revision per `identity_key` and
marks tied conflicting revisions with
`history_disposition: conflicting-observations`. A check run's head is immutable.
Different known heads claimed for the same check ID across any loaded observations
leave that check's projected reviewed head null and coverage unknown. Its parent
head also becomes unknown on projected annotations. Rebound historical checks
and annotations retain their raw head claims with unknown coverage. Snapshots
retain all loaded revisions. Tied contradictory latest snapshots for the requested head disable
current finding claims. The latest observation for that head must have a
complete check enumeration. A later denied or truncated enumeration
invalidates an earlier valid zero. A delayed observation of an old head keeps
its findings stale after a push.

With complete history, same-head checks are ordered by valid `started_at`
across loaded snapshots.
Strictly later rounds supersede earlier checks, including when the latest
fetch omits the later round. Missing or tied start times retain uncertainty.
Different valid start times claimed for one check ID, at tied or different
observation times, also mark its projection `conflicting-observations`.
An uncertain check has unknown supersession, null `superseded_by`, and false
current finding flags, even when one selected revision appears to order it safely.
Checks with no known head claim never establish supersession. If the newest
revision has no head, older known head claims still affect peers on those heads.
A headless run also remains an uncertain rival on each commit-specific listing
head where its missing or malformed reviewed head was observed. That listing
context cannot supply a reviewed head, current coverage or positive supersession.
If a later known head disagrees with any such listing head, the run remains
uncertain for ordering on every known and headless listing head. Its valid
reviewed-head claim and coverage remain unchanged. Recovery at the same listing
head can still restore otherwise certain ordering metadata.
A contradicted check also retains every claimed head for peer ordering.
Affected peers have unknown supersession and false current finding flags unless
every possible rival start is strictly earlier than the peer's start. Missing
rival start evidence in any loaded observation blocks ordering against an
uncertain rival, independently of the digest-selected revision. A unique latest
valid start can recover an otherwise certain run's metadata after an older missing
start. A known later check can remain current when all uncertain rival starts are
earlier. These rules also
apply when the latest enumeration omits the uncertain run, and to supersession
metadata rebound into historical snapshots. Digest selection cannot establish
ordering or raise current finding confidence. Historical missing heads followed
by a known head on the same ID may inherit that run's global supersession while
retaining unknown coverage and false historical flags.
Omission of a known latest round disables current known and zero flags. An
older omitted round does not prevent a valid later enumerated round from
being current. `superseded_by` names the uniquely observed next round when
available. `latest_check_enumeration_complete` describes the latest fetch's
enumeration and history bounds. Individual check flags also require valid
severity, execution, ordering and revision evidence.

Annotations keep the collector's location-only identity limitations. If an
ordinal's location changes across snapshots, the projection adds
`location_disposition: ordinal-location-revised` independently of representation
ambiguity. It retains `ambiguity: possible-duplicate-representation` when the
collector found a possible duplicate. For annotations whose collector ambiguity
is only `no-universal-immutable-id`, the projection also retains the existing
`ambiguity: ordinal-location-revised` label. Snapshots preserve both locations
and their original collector ambiguity.
This locator cannot establish that two representations describe one finding.
Reviews and inline comments retain `provider_identity: unverified`, and
comment thread state remains `thread_disposition: not-collected`.

## Remaining scope

This API does not install a live transport, fetch credentials, trigger reviews,
mutate providers or resolve threads. `finding_totals` stays null. It makes no
approval or merge-gate assertion. Verified review and inline app identity,
GraphQL thread disposition, and Hub, Worklore, JSON API and FleetHub integration
remain separate #1601 acceptance work.
