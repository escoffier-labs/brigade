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
caller changes intact. Ingestion opens a transaction when the connection is
idle and leaves commit or rollback to the caller. Existing transactions remain
open. A savepoint protects the append and quota checks without committing
unrelated changes. A rejected append rolls back its savepoint. If ingestion
opened the transaction, the caller must still commit or roll it back.

`ingest_managed_review` calls `collect_managed_review` itself. It accepts the
collector's optional `Limits` instance, never an arbitrary snapshot. Only the
collector's sanitized JSON enters storage. Its result contains `observation_id`
and `duplicate`. The observation ID is a SHA-256 digest of canonical JSON,
including normalized observation time. Exact replay keeps the existing row.
Different timestamps or sanitized revisions produce separate snapshots.
Repository identity is case-folded for collection, storage and reads. App ID
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

Reads fetch at most `limit + 1` rows for the exact subject. The default limit is
20, and valid limits are integers from 1 through 100. An extra row sets
`history_truncated`. A truncated read cannot establish current known or zero
findings. Observation order uses fixed microsecond UTC keys, including when
whole-second and fractional timestamps are mixed. Completion time, insertion
order and provider numeric IDs do not establish observation order.

The projection selects the latest loaded revision per `identity_key` and
marks tied conflicting revisions with
`history_disposition: conflicting-observations`. Conflicting head revisions
leave the projected reviewed head null and coverage unknown. Snapshots retain
all loaded revisions. Tied contradictory latest snapshots for the requested head disable
current finding claims. The latest observation for that head must have a
complete check enumeration. A later denied or truncated enumeration
invalidates an earlier valid zero. A delayed observation of an old head keeps
its findings stale after a push.

Same-head checks are ordered by valid `started_at` across loaded snapshots.
Strictly later rounds supersede earlier checks, including when the latest
fetch omits the later round. Missing or tied start times retain uncertainty.
Omission of a known latest round disables current known and zero flags. An
older omitted round does not prevent a valid later enumerated round from
being current. `superseded_by` names the uniquely observed next round when
available. `latest_check_enumeration_complete` describes the latest fetch's
enumeration and history bounds. Individual check flags also require valid
severity, execution, ordering and revision evidence.

Annotations keep the collector's location-only identity limitations. If an
ordinal's location changes across snapshots, the projection uses
`ambiguity: ordinal-location-revised` and the snapshots preserve both locations.
This locator cannot establish that two representations describe one finding.
Reviews and inline comments retain `provider_identity: unverified`, and
comment thread state remains `thread_disposition: not-collected`.

## Remaining scope

This API does not install a live transport, fetch credentials, trigger reviews,
mutate providers or resolve threads. `finding_totals` stays null. It makes no
approval or merge-gate assertion. Verified review and inline app identity,
GraphQL thread disposition, and Hub, Worklore, JSON API and FleetHub integration
remain separate #1601 acceptance work.
