# T3 lifecycle reporting

Upgrade the Fleet Hub before enabling `brigade fleet events report` in
t3-fleet. Older hubs cannot project unknown observations or delayed source
timestamps correctly. The hub's schema 32 adds a nullable `events.context`
column and preserves event history. Startup also repairs a missing registered
additive column when the database already reports schema 32.

T3 producers refresh nonterminal observations at least every 300 seconds.
The hub keeps running and paused observations fresh for 900 seconds from the
source timestamp. Older observations become stale. Context is bounded to
4 KiB and accepts only correlation metadata. Each string except `issue_url`
must be printable, at most 128 characters, contain no slash, backslash or
percent sign, avoid a leading `op:`, and pass credential checks. `issue_url`
must be a canonical HTTPS GitHub issue or pull URL, at most 512 characters.

The CLI posts its own event first, independently of any existing spool
backlog. Exit 0 means delivered or durably queued after transport failure.
A permanent hub rejection returns `rejected` and exit 2 without spooling
that event. Exit 1 means delivery failed and the event could not be persisted.
Shared spool poison-batch isolation is tracked in [#1735](https://github.com/escoffier-labs/brigade/issues/1735).

## Rollback

Stop t3-fleet reporting and the hub before rolling back. Back up the database.
An older Brigade binary refuses schema 32 with `FleetHubUnsupportedSchema`.
For a database whose only schema-32 change is `events.context`, set
`PRAGMA user_version=31` with SQLite before starting the older binary.
This is safe because the added column is nullable and older writers use
named-column INSERTs. Keep the column and its history. If other schema-32
migrations have also run, check their rollback requirements before changing
the version marker. Upgrade the hub again before resuming T3 reporting.
