# Operation readiness

`brigade roster ready` inspects a requested worker operation. `brigade roster
doctor` retains its existing configuration checks and exit behavior. A valid
roster can still have no eligible direct worker.

```console
brigade roster ready --worker builder --json
brigade roster ready --worker builder --worker reviewer --concurrency 2 --route brigade --isolation worktree
brigade roster ready --worker researcher --capability research.plan --route t3 --json
```

`--worker` is required and repeatable. Names must be distinct, present in the
effective roster, and different from its orchestrator. Concurrency defaults to
1 and cannot exceed the selected worker count or `limits.max_workers`. Route
defaults to `brigade`, isolation to `shared`. Capabilities use the roster's
existing dotted capability contract and `Agent.supports` semantics.

Use `--target` to select the workspace and `--roster` for an explicit file.
The report names the effective source (`explicit`, `workspace`,
`worktree-parent`, or `user`) and path. Resolution follows the same precedence
as other roster commands. It never scaffolds a roster over a fallback.

## Results

| Exit | Status | Meaning |
| --- | --- | --- |
| 0 | `ready` | All required evidence passed for this exact operation and remains fresh. |
| 2 | `blocked` | Invalid input, an incompatible selection, a fresh failure, denial, or unsupported requirement. |
| 3 | `unverified` | Required evidence is missing, degraded, stale, or scoped to another operation. |

A fresh refusal takes precedence over missing observations. An unsupported
requirement is a blocker. Missing T3 support evidence is unverified, and a
policy denial remains a denial. The diagnostic applies no automatic fallback.
A Brigade fallback needs an explicit route request and its own admission.

The versioned JSON schema is `brigade.roster_readiness.v1`. `evidence` reports
executable discovery, authentication/entitlement, transport, version gates,
model reachability, health isolation, requested operation isolation, route
support, admission, and capacity separately. Every item has a status, reason,
freshness, and corrective action when needed. Timed observations include their
Unix timestamp, age in seconds, and TTL. Nonfinite timestamps are unknown and
render as JSON null.

`selected_seats` includes the declared role and a separate selection role
(`worker`, `orchestrator`, or `missing`). `structural_worker_count` counts all
non-orchestrator roster seats. `usable_worker_count` counts selected workers
whose seat-specific checks passed. That count does not imply operation
admission. `configured_limit` is the roster maximum. `admitted_capacity` is
null until fresh capacity evidence is supplied. `limiting_reason` identifies
the first blocker, or the first unknown when there is no blocker.

## Inspection scope and current limits

Default inspection never sends a model inference request, reserves a lease,
creates a worker task, creates a worktree, changes persistent configuration,
or writes a health receipt. It uses the shared `SeatHealthProbe` only for
selected Brigade workers, excludes their fallback chains, disables model
smoke and executable version collection, and retains the shared 30-second
per-seat and 35-second overall health bounds. Prompt-free provider status
checks and transport initialization may still run. The shared probe's
in-process cache may be reused when an embedding caller supplies a probe.

For worktree requests, a non-Git workspace is blocked before health probing.
Git eligibility alone does not establish writable disk capacity or permission
to create a worktree. Worktree isolation remains unverified without scoped
isolation evidence. Writable ACPX workers with shared isolation are blocked.
The CLI has no read-only operation flag, so it does not claim readiness for a
read-only ACPX exception.

**The CLI currently has no authoritative live admission, capacity, or T3
capability/governance observation producer.** It therefore cannot return ready
for a live operation. A structurally eligible request normally returns
unverified, or blocked if selected-seat health supplies a fresh failure.
T3 requests skip local provider probes because those do not establish T3
execution health. No claim of T3 integration with Brigade budgets follows
from this command. Active inference probing is not offered by this subcommand.

## Integration interface

`brigade.roster_readiness` exposes these Python interfaces for an admitted
execution integration:

```python
ReadinessRequest(workers: tuple[str, ...], concurrency=1,
                 route="brigade", isolation="shared", capability=None)
Observation(status, observed_at, scope, detail="", capacity=None)
operation_scope(roster, request, workspace) -> str
evaluate_readiness(roster, request, *, workspace, health=None,
                   observations=None, now=None) -> dict
inspect_readiness(roster, request, *, workspace, health_probe=None) -> dict
```

`health` maps selected seat names to existing `SeatHealthResult` instances.
Their fingerprints must match the current seat declaration. Health uses the
shared cache TTLs: 300 seconds for healthy results and 30 seconds otherwise.
Cached results retain their original age. Missing checks remain unknown.
The evaluator expects versionless fingerprints, matching the inspector's
`collect_executable_version=False` setting.

`observations` maps `route-support`, `isolation`, `admission`, and `capacity`
to `Observation` instances. Their status is `passed`, `failed`, `denied`,
`unsupported`, or `unknown`. An observation must have the exact scope returned
by `operation_scope` and be no more than 30 seconds old. The scope binds the
workspace, request, selected seats, model/transport fingerprints, declared
capabilities, orchestrator, configured limit, and model allow patterns.
Changed concurrency, route, isolation, or capability invalidates earlier
operation evidence. A passed capacity observation also needs a nonnegative
integer `capacity`, representing currently admitted available workers.

The caller owns the authority and provenance of injected observations. This
interface evaluates supplied evidence and does not grant admission, acquire a
lease, consume budget, or reserve the observed capacity. An eventual dispatch
must still enforce all of its current gates.
