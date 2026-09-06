# Fleet remote worker transport (source side)

`brigade run` can now execute a worker seat on the machine the Fleet routing
authority selected, instead of always running it locally. The execution adapter
is the local `t3-fleet` client. The policy consumer stays `brigade-run`.

Source module: `src/brigade/fleet_t3_transport.py`. Tests:
`tests/test_fleet_t3_transport.py`.

## Where the route is resolved

`run_transport.dispatch` takes a `remote_transport` callable. Inside `invoke()`
the route is resolved **before** the existing `with model_lease(selected_agent)`
context is entered:

1. seat preflight (unchanged)
2. `on_dispatch_requested` run-budget accounting (unchanged)
3. **route resolution** - remote or blocked returns here
4. `with model_lease(...)` -> `_invoke_external` (unchanged local path)

So a seat that policy placed on another machine never takes a local model lease
and never reaches a provider. `planning.dispatch` passes the transport straight
through; `planning._run_orchestrator` does not receive it, so the orchestrator
keeps its own local capacity and can never recurse into a T3 delegate.

The transport is only wired when `fleet_t3_transport.build_source_transport`
finds an enrolled snapshot *and* `routing.enabled` true in the authoritative
policy document. A standalone checkout, an unenrolled fleet, a routing-disabled
policy, or an unreadable policy document all keep every seat on the existing
local path.

## Origin identity

Routing resolves this machine's identity automatically. `fleet_client.resolve_node_id()`
gives the authenticated node id; the policy document's `machines` table maps it
to exactly one machine name (the Hub enforces that uniqueness at parse time).
The operator never restates their machine, no hostname is guessed, and an
unmapped node is a `origin-identity-unresolved` rejection rather than a literal
`"local"` the Hub cannot authenticate.

`fleet_session_bootstrap.default_origin()` applies the same rule to the session
launch helpers, which previously defaulted to `"local"` and discarded the
`resolve_node_id()` return. A checkout with no identity keeps `"local"`. An
explicit caller-supplied origin remains a separate, receipted override.

## Proof order for a delegated task

```
immutable source revision   (clean 40-char HEAD, else remote-context-unavailable)
  -> delegation create      (adopts the existing decision + reservation; no second route)
  -> doctor + capacity      (ineligible or at-capacity target refuses; no fallback)
  -> submit --delegation-id (prompt in a 0600 file, never on argv)
  -> persist qualified request id  (before any poll)
  -> bounded wait
```

The delegation is created from the routing decision id, the immutable revision,
and a generated parent request identity (`brigade-run:<run-id>:<seat>`). It is
stable per run and seat, so a same-seat retry adopts the *same* delegation
instead of creating a second reservation. `--delegation-id` is the only adoption
flag: no consumer or adapter override, no peer-qualified request id override, no
guessed host or repository alias. T3 resolves `repo_identity` to a configured
`policy_identity` alias itself and refuses adoption when enrollment is off.

Seat eligibility is checked against the exact `bindings.t3_fleet` /
`bindings.native` launch binding via `fleet_model_roster.adapter_plan_binding`.
A Brigade CLI-only seat is **not** automatically T3-launchable; a missing
binding is a `t3-binding-missing` routing rejection, not a reason to guess.

## Ownership

The source owns the orchestration receipt and a read-only model plan
projection. It never leases a model seat, calls a provider, renews a
reservation, or releases one for a remote branch. Reservation release is
target-only and the Hub enforces that, so a blocked source route leaves the
reservation to its own TTL rather than attempting an unauthorized release. The
target performs prepare -> context application -> ack -> launch proof ->
provider execution and renews its own reservation as the execution claim.

## Result states and handoff

`result.state` is preserved exactly. `succeeded`, `failed`, `interrupted`,
`needs_attention`, `blocked_offline`, `unknown`, and a wait timeout (still
`running`) stay distinct; a wait timeout is neither terminal success nor a safe
release. Non-success maps to `failure_kind="t3-<state>"`.

A terminal delegated writable execution that kept a target worktree may carry
`disposition: "pending-handoff"` and the frozen `handoff` fields
`{target_machine, repository, branch, source_revision, request_id, worktree_ref}`.
`worktree_ref` is an opaque target-owned request reference, never a filesystem
path and never a claim that the source checkout changed. Running and unknown
states never claim a completed handoff, and a pre-launch failure omits it
entirely. These facts ride on the new `WorkerResult.remote` field, so an empty
local diff is never read as locally implemented.

An unknown outcome (or a `unknown_state` error carrying a request id) is
recovered with `t3-fleet recover` against the retained qualified handle. The
original prompt is never resubmitted as a guess.

## Deployment prerequisites

- **`t3-fleet submit --delegation-id` must ship.** The client installed at the
  time of writing exposes `submit --request-id/--host/--repository/--source-revision/--title/--prompt-file/--seat/--runtime-mode/--interaction-mode/--run-setup-script/--allow-queue`
  and no `--delegation-id`. Brigade classifies that rejection as
  `t3-delegation-unsupported` and refuses the route; it does not fall back.
- **Hub delegation-aware plan/admit.** `fleet_model_admission.plan_model` and
  `admit_model` currently fail closed with `delegation-unavailable` when a
  `delegation_id` is supplied. Target-side launch needs those paths completed;
  the source side does not depend on them.
- **Routing must be enabled in the authoritative policy** and both machines
  enrolled with `node_id` mappings, or no seat is ever delegated.
