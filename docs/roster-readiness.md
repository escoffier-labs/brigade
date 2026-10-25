# Operation readiness

`brigade roster ready` inspects a requested worker operation. `brigade roster
doctor` retains its existing configuration checks and exit behavior. A valid
roster can still have no eligible direct worker.

```console
brigade roster ready --worker builder --json
brigade roster ready --worker builder --worker reviewer --concurrency 2 --route brigade --isolation worktree
brigade roster ready --worker researcher --capability research.plan --route t3 --json
```

`--worker` is required and repeatable. It names a selected seat pool, not a
task count. Duplicate names are removed in encounter order for selection,
display, and health inspection. Operation scope sorts the distinct names, so
reordering the same pool preserves its scope. Each selected seat must be a worker
rather than the effective orchestrator. Concurrency defaults to 1 and must be
a positive integer. A single seat can supply concurrency 3 when
`limits.max_workers` and fresh admitted capacity both permit 3 assignments.
Repeating `--worker builder` does not request another task. Route defaults to
`brigade`, isolation to `shared`. Capabilities use the roster's existing dotted
capability contract and `Agent.supports` semantics.

Use `--target` to select the workspace and `--roster` for an explicit file.
The report names the effective source (`explicit`, `workspace`,
`worktree-parent`, or `user`) and path. Resolution follows the same precedence
as other roster commands. It never scaffolds a roster over a fallback.
Before classifying seats it applies `run_preference.load_cached()` through
`apply_to_roster`, matching the cached role overlay used by `run`. It never
refreshes that cache, so a newer Hub preference remains outside this report.

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
render as JSON null. Malformed operation observations are unknown with reason
`observation-malformed`. Invalid health values are unknown with reason
`health-unobserved`. Valid refusals in other evidence retain precedence.

`selected_seats` includes the declared role and a separate selection role
(`worker`, `orchestrator`, `missing`, or `unresolved`). `structural_worker_count` counts local
non-orchestrator roster seats. `usable_worker_count` counts selected workers
whose seat-specific checks passed. That count does not imply operation
admission. `configured_limit` is the roster maximum. `admitted_capacity` is
null until fresh capacity evidence is supplied. `limiting_reason` identifies
the first blocker, or the first unknown when there is no blocker.

## Inspection scope and current limits

Discovery is limited to the resolved local roster and cached preference.
`roster.discovery` is `local-cache-only`. `roster.local_authoritative` is true
when no Hub path is configured. In that case a missing selected seat is blocked
as `worker-missing`. When a Hub URL is configured through the existing fleet
settings, the local roster cannot prove a seat is absent globally. A missing
local seat has selection role `unresolved`, reason
`worker-discovery-unverified`, and status `unverified` unless another known
blocker applies. The diagnostic does not query the Hub or synthesize seats.
`run` can perform that discovery later through its own admission path. Local
counts do not count Hub-only seats.

Default inspection never sends a model inference request, reserves a lease,
creates a worker task, creates a worktree, changes persistent configuration,
or writes a health receipt. It uses the shared `SeatHealthProbe` only for
selected Brigade workers, excludes their fallback chains, disables model
smoke and executable version collection, and retains the shared 30-second
per-seat and 35-second overall health bounds. Prompt-free provider status
checks and transport initialization may still run. The shared probe's
in-process cache may be reused when an embedding caller supplies a probe.

The generic CLI dispatcher records nonzero exits in the user-local
`brigade/last-error.json` under the platform data directory. Both blocked
(exit 2) and unverified (exit 3) readiness checks can replace the previous
error record used by `brigade bug report`, including routine checks with
missing live producers. `roster doctor` uses the same dispatcher behavior.

For worktree requests, a non-Git workspace is blocked before health probing.
Git eligibility alone does not establish writable disk capacity or permission
to create a worktree. Worktree isolation remains unverified without scoped
isolation evidence. Writable ACPX workers with shared isolation are blocked.
The operation-level isolation row never passes when a selected seat is
incompatible.
If injected isolation evidence passed, `observed_status` retains that supplied
status alongside the derived failure. The CLI has no read-only operation flag,
so it does not claim readiness for a read-only ACPX exception.

The health `isolation-compatibility` check currently tests read-only
enforcement. Readiness conservatively requires it even for a writable
operation, separately from requested worktree isolation. This can leave a
writable adapter unverified or blocked despite adequate write isolation.
Adapter-aware operation health requirements need an explicit source contract
before this gate can be relaxed. Unknown evidence remains unverified.

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
                   observations=None, now=None,
                   hub_discovery_possible=False) -> dict
inspect_readiness(roster, request, *, workspace, health_probe=None,
                  hub_discovery_possible=False) -> dict
```

`health` maps selected seat names to existing `SeatHealthResult` instances.
Their fingerprints must match the current seat declaration. Health uses the
shared cache TTLs: 300 seconds for healthy results and 30 seconds otherwise.
Cached results retain their original age. Their effective `observed_at` is
`finished_wall_at - cache_age_seconds`, so `age_seconds` equals evaluation time
minus that observation time. A future replay timestamp, negative cache age,
or nonfinite time cannot establish fresh health. Isolation health is
recomputed by the shared cache but conservatively uses the retained age here.
Missing or malformed checks remain unknown without discarding valid sibling
checks. Health timestamps and cache ages must be finite `int` or `float`
values, excluding booleans.
The evaluator expects versionless fingerprints, matching the inspector's
`collect_executable_version=False` setting.

Programmatic callers must supply valid request fields. Malformed requests
return blocked `malformed-request` before seat selection, scope hashing, or
provider probes, with null `request` and `scope`. `operation_scope` raises
`ValueError` for a malformed request and canonicalizes the distinct worker pool
for valid requests. Set `hub_discovery_possible=True` only when configured Hub
discovery could supply missing local seats. This flag records uncertainty and
does not authorize network discovery or admit a seat. The Python evaluator
uses the roster supplied by its caller, including any preference overlay.

`observations` maps `route-support`, `isolation`, `admission`, and `capacity`
to `Observation` instances. Their status is `passed`, `failed`, `denied`,
`unsupported`, or `unknown`. An observation must have the exact scope returned
by `operation_scope` and be no more than 30 seconds old. Observation timestamps
must be finite `int` or `float` values, excluding booleans. Status, scope, and
detail must be strings. The scope binds the
workspace, request, selected seats, model/transport fingerprints, declared
capabilities, orchestrator, configured limit, and model allow patterns.
Changed concurrency, route, isolation, or capability invalidates earlier
operation evidence. A passed capacity observation also needs a nonnegative
integer `capacity`, representing currently admitted available workers.

The caller owns the authority and provenance of injected observations. This
interface evaluates supplied evidence and does not grant admission, acquire a
lease, consume budget, or reserve the observed capacity. An eventual dispatch
must still enforce all of its current gates.
