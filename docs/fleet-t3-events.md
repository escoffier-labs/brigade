# T3 lifecycle reporting

Upgrade the Fleet Hub before enabling `brigade fleet events report` in
t3-fleet. Older hubs cannot project unknown observations or delayed source
timestamps correctly. The hub's schema 32 adds a nullable `events.context`
column and preserves event history. Schema 32 also adds `model_policy.t3_model`
for per-seat native T3 launch bindings. Startup repairs either missing additive
column when the database already reports schema 32.

Report through the enrolled local fleet identity:

```bash
brigade fleet events report --run-id run-example --sequence 1 \
  --state run.started --ts 2026-10-17T12:00:00Z \
  --repo repo-example --seat seat-example --json
```

`--harness` defaults to `t3-fleet` and accepts only that value. States are
`run.created`, `run.started`, `run.dispatch.observed`, `run.paused`,
`external.completed`, `external.failed`, `external.canceled`, and
`external.unknown`. `--ts` requires an ISO-8601 UTC observation time.
`--sequence` is an integer from 0 through `2**63 - 1`. Optional metadata includes
`--repo-identity`, `--exit-status`, and either `--context-file` or
`--context-stdin`. The CLI validates the event and computes its digest.

Context requires `schema: "brigade.t3_execution_context.v1"`. Its optional
correlation fields are `request_id`, `thread_id`, `turn_ref`, `controller_id`,
`origin_node`, `target_host`, `work_id`, and `issue_url`. An optional `initiator`
object accepts `kind` (`agent`, `harness`, `human`, or `unknown`) and `label`.
An optional `policy` object accepts `session_id`, `decision_id`, `reservation_id`,
and `delegation_id`. Unknown fields are refused.

T3 producers refresh nonterminal observations at least every 300 seconds.
The hub keeps running and paused observations fresh for 900 seconds from the
source timestamp. Older observations become stale. Context is bounded to
4 KiB and accepts only correlation metadata. Each string except `issue_url`
must be printable, at most 128 characters, contain no slash, backslash or
percent sign, avoid a leading `op:`, and pass credential checks. `issue_url`
must be a canonical HTTPS GitHub issue or pull URL, at most 512 characters.
Nonterminal observations more than 300 seconds in the future project as `external.unknown`.
Unknown and stale observations retain their event history and do not release
claims, ownership, or capacity.

The CLI posts its own event first, independently of any existing spool
backlog. Exit 0 means delivered or durably queued after transport failure.
A permanent hub rejection returns `rejected` and exit 2 without spooling
that event. Exit 1 means delivery failed and the event could not be persisted.
A shared spool batch the hub rejects is bisected, and only the events it rejects individually move to the dead letter described in [fleet-sync.md](fleet-sync.md) ([#1735](https://github.com/escoffier-labs/brigade/issues/1735)).

## Rollback

Stop t3-fleet reporting and the hub before rolling back. Back up the database.
An older Brigade binary refuses schema 32 with `FleetHubUnsupportedSchema`.
For a database whose only schema-32 change is `events.context`, set
`PRAGMA user_version=31` with SQLite before starting the older binary.
This is safe because the added column is nullable and older writers use
named-column INSERTs. Keep the column and its history. If other schema-32
migrations have also run, check their rollback requirements before changing
the version marker. Current startup also adds `model_policy.t3_model`, so this
context-only procedure does not cover a normally upgraded database. Retain the
pre-upgrade backup for rollback. Upgrade the hub again before resuming T3 reporting.
