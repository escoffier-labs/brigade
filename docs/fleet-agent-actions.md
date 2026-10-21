# Alert repair actions

An enrolled broker can dispatch a repair prompt from a concrete Hub alert to a
registered T3 Code project, OpenClaw agent, or durable inbox principal. Hub stores
requests and receipts. Provider credentials stay on the broker node.

Dashboard viewing access remains read-only. An administrator must grant one exact
trusted Tailscale login access to specific destination and subject pairs. The
browser then explicitly enables a separate 15-minute operator session. Activation
and submission require a canonical same-origin POST, CSRF proof, and the trusted
login header from the configured loopback proxy. Browser requests never fall back
to an admin or node bearer. Operator cookies are HttpOnly and SameSite=Strict,
with Secure on HTTPS. Hub stores credential and CSRF digests.

## Grants

Use the existing configured admin credential for grant commands. Scope files and
the node manifest must be private regular files owned by the invoking user.
These examples use fictional identities.

An operator scope file contains destination and subject pairs:

```json
{
  "scopes": [
    {
      "target_id": "fixture-repair",
      "subject": {"kind": "telemetry", "node_id": "fixture-node"}
    }
  ]
}
```

A broker scope file contains its already-enrolled node and permitted targets and
subjects:

```json
{
  "node_id": "fixture-node",
  "target_ids": ["fixture-repair"],
  "subjects": [{"kind": "telemetry", "node_id": "fixture-node"}]
}
```

```text
brigade fleet actions grant-operator --login operator@example.test --scope-file operator-scope.json
brigade fleet actions grant-broker --scope-file broker-scope.json
brigade fleet actions revoke-operator GRANT_ID
brigade fleet actions revoke-broker NODE_ID
```

Run selectors may name an exact enrolled `node_id` and an optional exact
`repo_identity` instead of an individual `run_id`. This admits future runs on
that node. T3 dispatch requires a nonempty source repository identity
matching the target repository. Telemetry scopes require an exact node and work
scopes require an exact work ID. A request always resolves one concrete source on
Hub. Display labels and aggregate alerts grant no authority.

Replacing an operator grant rotates its identity. Old sessions and queued requests
cannot regain access through a later grant. Revoking the broker's node enrollment
also blocks dispatch. Removing a target from a sync withdraws its registration.
Hub rechecks scopes and destination registration immediately before start.
`revoke-broker` removes action authority, hides its targets, and expires unstarted
actions with ledger events. It preserves the node's enrollment and telemetry.

## Node manifest

The protected manifest contains deployment-specific paths and bindings. Only its
public projection and binding digest reach Hub. No credential belongs in it.

```json
{
  "schema": "brigade.fleet_action_manifest.v1",
  "journal_dir": "/private/fixture-actions",
  "targets": [
    {
      "public": {
        "target_id": "fixture-repair",
        "family": "openclaw",
        "agent_id": "fixture-agent",
        "label": "Fixture repair agent",
        "subjects": [{"kind": "telemetry", "node_id": "fixture-node"}],
        "models": [],
        "capability": "execution"
      },
      "binding": {"agent_id": "fixture-agent"}
    }
  ]
}
```

Supported families are `t3`, `openclaw`, `grokbot`, and `dot`. Inbox targets use
`capability: "inbox"`, an empty binding, and an exact active `principal` in their
public record. OpenClaw requires an explicit agent ID and exposes no model
override. An optional exact `session_key` must match in both public and protected
bindings and use `agent:<agent_id>:clawchat:<uuid>` syntax. Its full value is part
of the immutable native destination. T3 requires binding fields `host`, `repository`, `repo_identity`, and a
40-character `source_revision`. Its public `models` entries have `id` and `label`.
The IDs name seats in Hub's current model roster. The picker uses that roster's
enabled state, native T3 bindings, retirement checks, and model labels. Activated
Fleet policy also restricts repository eligibility. Listing a model does not admit
or reserve it. A saved label does not admit a seat. An optional T3 binding
`override_reason` records an explicit operator selection where routing requires it.
The protected T3 binding may specify `runtime_mode`: `approval-required`,
`auto-accept-edits`, `auto`, or `full-access`. The generic adapter default is
`approval-required`.

A target ID permanently identifies its owning node and native destination,
including the T3 repository identity, OpenClaw agent ID, or inbox principal.
Withdrawal, expiry, and revocation never allow rebinding that ID. A destination
change requires a new ID and grant. Catalog labels, selectors, model labels, and
source SHA can refresh without invalidating unrelated queued requests. Execution
uses the source SHA frozen in each request.

An optional `executables` object maps `t3`, `openclaw`, `brigade`, `systemd-run`,
and `systemctl` to installed executable paths. This is deployment configuration,
never browser input. There is no shell command field or installer fallback.

```text
brigade fleet actions targets sync --file manifest.json
brigade fleet actions tick --manifest manifest.json --budget-seconds 15 --max-actions 1
brigade fleet actions show ACTION_ID --json
```

The existing telemetry invocation may opt into `tick` after publishing. Host
wiring must contain action failures so telemetry still finishes. Tick validates
executor availability and schedules at most one new action per tick. It can also
reconcile one existing job without blocking later claims. Long
turns run in that job, outside telemetry. No persistent service or timer is added.
The internal job command is `fleet actions execute ACTION_ID --generation N
--manifest FILE`. Registrations expire after 300 seconds.

## Retry and result contracts

Hub keys submissions by operator grant and client ID. Exact retries return the
same action. Changed content under that key conflicts. Evidence is resolved on
Hub and its revision is checked at submission and before start. A source can be a
run node plus run ID, a Worklore ID with its server version, or enrolled telemetry.
Aggregate exclusions and coverage labels are never executable subjects.

Claim generations fence old jobs. Hub durably records start before a destination
can accept work. A crash after that fence without a durable external handle parks
the action as unknown. It never submits a replacement turn. The protected node
journal saves each observation's receipt and event ID before reporting it. A lost
Hub response retries that exact receipt and event ID. Deterministic report
refusals retain an audit record. Authorization and fence refusals hold the job. T3 recovery uses the exact public
request handle, using `status` for known states and `recover` for unknown states.
Automatic recovery of any started action stops after 24 hours from the Hub's
original `started_at`, including inbox lookup without a destination handle. The
cutoff is fixed across observations and process restarts. At the cutoff the local
journal parks the action without changing its Hub state or canceling the provider
work; delayed execution jobs also check the cutoff before provider queries.
Terminal Hub states close normally unless a durable report receipt is pending.
That receipt remains available for exact event-ID replay, including after the
cutoff, without provider lookup or redispatch. A malformed start timestamp also
parks automatic recovery because its age cannot be established safely. Manual
observation remains available through the existing action show path.
OpenClaw uses the exact selected bot session key when registered, or an
action-specific session key for a generic agent. Both use Gateway mode without
`--deliver`. The session key provides correlation. Reusing it can create another
turn.

Inbox messages use the action ID as their stable client ID and thread. Initial
dispatch sends exactly one message online without reading or writing the local
spool. A transport failure leaves acceptance unknown and does not queue or retry.
Reconciliation only reads sent-message status for that stable ID. It never sends
again or flushes the credential's spool.
Without a matching receipt, execution stays unknown. Delivery and acknowledgement never imply execution.
A finished destination execution still displays verification pending. Only fresh
Hub telemetry evidence for the original subject may clear a telemetry condition.
Work evidence attaches only structured ID, version, state, and time. Hidden titles
and blockers are excluded. Fresh `completed` state can clear that exact work
condition. Run verification requires fresh `run.completed` or `external.completed`
evidence with exact zero exit status for the original enrolled node and exact run
ID, with event and receipt timestamps after action creation. Success from another
run leaves verification pending, including when it uses the same node and
repository. An immutable failed run stays pending without new evidence for that
exact subject. Destination execution and output cannot verify the original issue.

Telemetry revisions fingerprint status, credential state, and computed staleness.
New observations alone preserve the review. A changed condition requires renewed
review before start. Blocking OpenClaw turns retain a bounded lease longer than the
provider timeout and shorter than the transient job limit. T3 submit has a
180-second timeout and a 240-second started lease. Execution receipts require an
immutable destination reference, except an initial T3 pre-launch refusal with no
prior handle and a closed rejection code: `invalid_config`, `invalid_request`,
`policy_rejected`, or `target_busy`. Other no-handle outcomes remain unknown.
Optional conversation references appear separately from request handles.
