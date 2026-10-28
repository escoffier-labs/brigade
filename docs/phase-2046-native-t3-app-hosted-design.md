# Epic 2046: app-hosted native T3 integration

Status: user-approved design, 2026-10-28. Astra builder review found no blocking V0 planning questions.

## Decision and approval scope

The user selected app-hosted T3 execution as the first integration target, with
Brigade retaining governance and durable state. This document proposes the
architecture and a bounded first implementation slice. User approval authorizes
planning that first slice. It does not activate native execution or approve
unsettled backend, T3 transport, ownership or batch contracts.

The first slice is a pure contract implementation with fixtures. It validates
target qualification, immutable intent and native observations using supplied
data. Production dispatch remains unwired. Subsequent execution requires the
dependencies and separate acceptance criteria below.

Source baseline: `2c565ad0310caee756a8a85e0966a0466837bef7`. The current checkout
is older, so source review used `git show` at that pin. Implementation must
refresh affected paths against its actual base and coordinate overlapping work.
The two accompanying local reports are `native-adapter-design-2c565ad.md` and
`readiness-producer-native-2c565ad.md` under
`docs/plans/epic-2046/t3-native-integration-coordination/`. They are source
reviews, not live execution qualification.

## Intent

An operator should be able to conduct governed Brigade work from a T3 session
using native delegation, linked environments, worktree preparation and completion
wakes. Brigade should retain the same accountable work owner, approved scope,
policy decisions, budget and durable result consumption across that execution.

The T3 parent session hosts the controller. It invokes native tools through the
app's existing authenticated session and drives Brigade's explicit state
transitions through supported local entry points. This is a logical component,
not a proposed server, daemon, scheduler or second thread manager. The first
slice does not claim those entry points or their enforcement already exist.
An agent prompt or skill instruction cannot enforce target admission.

Brigade remains a zero-runtime-dependency Python CLI. No MCP client package,
private app database access, undocumented HTTP endpoint, provider credential
copying or standalone native-launch bridge is added by this design.

## Native T3 alignment priority

Prefer supported native T3 discovery, delegation, app-owned worktrees, task
control and completion notifications. Before each implementation slice, compare
Brigade-owned execution machinery with the current native tool catalog and
upstream contracts. Record the inspected revision, available capabilities,
overlap and adoption decision. Reuse native behavior where its verified contract
meets the requirement. Retain additional Brigade execution machinery only with
a documented governance, durability or compatibility need.

Brigade retains governance, durable ownership and claims, budgets, verification
and provenance. Native presence, discovery and lifecycle observations establish
no admission, reservation or launch authority. Missing native guarantees remain
explicit unknowns and activation dependencies.

Extend existing update or maintenance checks for any future daily capability,
overlap and adoption report, subject to the existing research owner's concrete
recommendation and agreed scope. This decision installs no watcher, service,
schedule or activation path.

## Approaches

| Approach | What it enables | Cost and constraint | Decision |
| --- | --- | --- | --- |
| App-hosted controller with Brigade state transitions | Uses the native delegation and wake surface already available in T3 sessions | Still needs enforced target governance, acceptance recovery and replay guarantees | User selected this first |
| Standalone Brigade native transport through a T3-owned bridge | Could eventually launch from an ordinary CLI process | No supported callable bridge, origin authority or standalone wake contract was established by the reviewed source | Deferred |

The legacy `t3-fleet` client remains a separate supported path. Its subprocess
interface does not establish native MCP support. There is no automatic fallback
between native, legacy fleet and local execution.

## Authority and ownership

| Component | Owns | Cannot establish |
| --- | --- | --- |
| Worklore | Durable work ownership, generation and declared checkpoint path admission | Physical writer exclusion, provider liveness or successful verification |
| Brigade run/task/budget components | Task identity, launch claim, dispatch accounting, journal replay and exact result acknowledgement | Native acceptance, target containment or a T3 wake guarantee |
| Brigade policy/admission integration | Authoritative route, reservation, admission and launch-proof decisions under its existing contracts | Authority from a model catalog, reachable link or prompt |
| Native T3 app | Child task creation, selected environment/project, provider execution, app-owned worktree, cancellation and app wakes | A Brigade reservation or durable Worklore ownership merely by creating a task |
| Hub admin preview | Read-only human presentation of approved projections | An ownership backend, scheduler, admission or reservation logic |

Epic 2046 owns this adapter/readiness design. The rebrand lane retains CLI/core
docs, neutral defaults, skills acceptance and its existing tests. Preserve
`brigade`, `.brigade`, `--chef`, persisted/wire chef fields and existing rosters.
Coordinate overlapping roster/help/tests before edits. E2EE stays separate.

The Hub preview owner is a verified coordination contact, not an assumed owner
of every backend authority. Backend/projection changes require agreement with
the accountable backend owner and preview owner where their surfaces overlap.
This design requests no changes from either lane.

## Proposed identity model

Use one bounded correlation record with separate namespaces. The rows below
define semantic requirements, not approved JSON keys or a public wire schema.
The first slice can represent these requirements internally without changing
existing closed event payloads.

| Group | Required correlation | Meaning |
| --- | --- | --- |
| Work/source | Existing Worklore work ID and its authenticated authority scope, canonical repository and source reference | Joins to existing work, never creates an owner from a thread title or issue number |
| Durable holder | Owner node, ownership revision and generation from Worklore | Public holder identity is a node reference, never the secret holder nonce, hash or lease fencing token |
| Brigade task | Run ID, request ID, task ID, task spec digest, prepared attempt ID, selected seat and dispatch ordinal | Repeated assignments to the same seat remain distinct |
| Native origin | Origin environment and parent thread | Defines the scope in which the delegated task can be read or canceled |
| Native target | Environment, project, provider instance, driver, explicit model/options, modes, isolation and immutable source revision | Binds the intended provider and prepared workspace without inherited guesses |
| Native acceptance | Native client request key, task ID, child node/thread/run and remote child pair when supplied | An originating task ID and a remote environment/thread pair serve different operations |
| Native terminal | Terminal run and result-transfer identity, status and containment evidence | A finished turn can still own descendants or pending work |
| Brigade authority | Policy revision/digest, delegation/decision, admission, reservation, launch proof and budget dispatch reference as applicable | These references retain distinct roles and original expiry |
| Consumption | Brigade terminal result ID, artifact classification/digest and acknowledgement | Native delivery acknowledgement is separate from Brigade consumption |

Prelaunch intent contains the exact selected target and approved immutable
payload/context digest. Acceptance fields are absent before acceptance. Missing
required intent fields refuse qualification. Native acceptance and authority
references may remain unknown in diagnostic records, but cannot be fabricated
or reused as permission to launch. A source reference or digest alone does not
retain the payload needed for restart.

V0 records no real holder capability and does not acquire Worklore ownership.
Later writes must revalidate the real holder's current generation and revision.
A generation change fences further metadata mutations, but cannot prove a
previous provider process stopped. Cancellation and containment still need
independent evidence.

### Read-only owner discovery and Hub presentation

The eventual projection should support bounded lookup by canonical repository,
work ID or verified source issue reference. It must report scope, observation
times, revisions, truncation and source failures. Exact lookup/pagination and
storage are backend contracts deferred from V0. Existing separate GETs do not
produce an atomic combined snapshot.

`unregistered` means no correlation was found in the stated inspected scope.
`unknown` means the lookup or its completeness/authority cannot establish that
answer. Neither means unowned. `unowned` requires an authoritative Worklore
ownership result. Issue discovery must preserve multiple matching work items
and conflicts rather than select an owner heuristically.

An expired native presence observation can mark presence stale while retaining
the last observed holder and generation with their source time. Explicit release
or a new generation supersedes current ownership while preserving history.
Agent identifiers belong behind the Hub's existing Admin/Agent toggle. The
toggle is a presentation choice and does not replace backend access controls.
Owner discovery and Hub projection are later coordinated slices, not V0 output.

## App-hosted execution boundary

The existing `RemoteTransport` placement branches before the origin model lease
and provider invocation. Reuse that worker boundary when execution is qualified.
Do not reuse its current synchronous result shape unchanged: it lacks assignment
identity and treats any returned result as a finished attempt.

The proposed boundary accepts one previously bound assignment and returns one
of four semantic outcome families: refused, awaiting/reconciling, active, or
terminal candidate. A terminal candidate is consumable only after correlated
containment and result persistence checks. Names and internal representation
are builder choices in V0. Every outcome retains the original identity and a
bounded reason. Missing or contradictory evidence retains uncertainty and
cannot authorize launch, refund, fallback or final acknowledgement.

The controller invokes `delegate_task`, `task_status` after a completion wake,
and `task_cancel` for cancellation, through supported T3 tools. It follows the
tool's returned `next`. It does not poll running tasks or create top-level
threads to substitute for delegation. A stopped parent session may need a
supported resume/delivery route. No such standalone subscription is assumed.

### Future launch and recovery order

1. Establish current durable work ownership, approved path scope and complete
   batch-to-assignment binding. Retain the full immutable execution input and
   target context in the existing Brigade durability domain under an approved
   versioned extension. No parallel task database.
2. Obtain fresh target-scoped qualification, admission and capacity. Normal
   dispatch still obtains or adopts its required reservation and proof. A
   readiness observation owns no capacity.
3. Under active run ownership and the budget coordinator, reserve the exact
   dispatch candidate. Claim that prepared task once, then persist the requested
   dispatch fact for the same candidate. Compose the existing budget callback
   with `reclaim_pending=False`. Uncertain recorder/claim outcomes retain budget.
4. Revalidate cancellation, ownership and expiring authorization at the real
   launch boundary. Target-side enforcement must bind current governance to
   native acceptance before the provider starts. Carrying IDs in a prompt is
   insufficient. V0 has no launch operation.
5. Submit once using the retained native request key, only from a fresh durable
   launch grant after the preceding barriers. Persist returned native acceptance
   before awaiting completion or releasing launch ownership.
6. Crash after claim, lost submit reply or missing saved handle enters
   reconciliation with the same immutable intent. `already-claimed` cannot
   regrant launch. A lookup miss is not proof of nonacceptance without an
   authoritative T3 contract defining that guarantee. Generic provider retry,
   a new request key and another budget reservation are prohibited recovery.
7. On a completion wake, reconcile the originating task. A question, approval,
   wait timeout, cancellation request, completed turn with descendants, or
   uncertain containment remains nonterminal. Model/runtime selection grants
   no authority to approve a user permission request.
8. Persist the correlated terminal observation and artifact classification,
   then let the actual Brigade consumer acknowledge its exact result ID.
   Duplicate wakes and acknowledgements must be idempotent. Native result reads
   can mark delivery, so replay after that mark must be established before
   activation. A reported artifact is not a verified artifact or a test receipt.

Native request-key deduplication scope, conflicting reuse, retention, lookup by
request key and result replay remain T3 acceptance contracts. V0 preserves these
unknowns. It does not manufacture exactly-once execution from a local claim.

## Readiness

The pure qualifier consumes supplied native discovery, link/target metadata and
Hub-verified binding projections. It performs no network request, probe,
reservation, admission, worktree creation, task creation or cache refresh.

Validate the exact consumer/seat and selected native context. Preserve original
source observation and expiry rather than stamping cached data as fresh.
Disabled providers, unknown options, identity mismatch and authoritative refusal
must not trigger fallback. Partial declaration evidence remains separate from
the existing route-support, isolation, admission and capacity observations.

All four native readiness facts remain unknown unless their exact authoritative
operation-specific requirements are satisfied. Current discovery alone cannot
pass them. The current readiness scope omits native target identity, and its
health requirements are not a native health contract. V0 neither changes that
public scope nor weakens those checks. It returns internal qualification and
missing-contract reasons without marking the CLI ready.

## First implementation slice: V0

V0 delivers internal pure value types/validators and fake-boundary tests for:

- Exact selected-target qualification from supplied declarations and verified
  binding material, preserving provenance, original freshness and refusal.
- Immutable intent comparison across work/holder/task/target/revision/options,
  with acceptance references separated from prelaunch inputs.
- Native lifecycle observation classification, including active descendants,
  pending questions/approvals, uncertain acceptance and non-quiescent failures.
- Bounded public diagnostics that omit holder secrets, credentials, prompts,
  raw native responses and unrelated private fields.

Builders may choose private type/function names and split a small adapter module
from focused tests. They must not add speculative public CLI flags, event keys,
transport commands, Hub endpoints or target-side authority. No production caller
is wired to a submit function. V0 exposes no submit capability or launch grant.
Legacy behavior stays unchanged. No new external dependency or installation.

Whole-batch binding remains an activation prerequisite. V0 accepts only an
opaque validated binding reference from the future batch component and never
uses that reference as proof that a real batch has been accepted. Ordering,
duplicate-member semantics, task/request namespace and partial-batch recovery
need their own parent-owned contract before execution. V0 does not decide them
through hashing or incidental fixture ordering.

## Verification and acceptance

V0 tests must reject altered target/environment/project/provider/options,
changed source revision or holder generation, replay with changed immutable
input, stale/future evidence, mismatched discovery context and unknown required
identity. Include repeated-seat assignments with distinct task identities,
disabled provider declarations and canonical-versus-launch model differences.

Lifecycle cases include lost acceptance reply, no authoritative request lookup,
duplicate terminal observation, contradictory identity, pending child work,
cancellation requested without containment, failed turn with active descendants,
and missing terminal result identity. None can produce a fresh launch grant or
consumable result from incomplete evidence. Pure tests establish classification
only, not real durability or target enforcement.

Before source/test edits, record exact graph impact and affected tests against
the implementation base, inspect live claims and Worklore ownership, and obtain
an eligible governed worker route. Follow the focused Brigade gate with actual
selectors chosen from the impact set. Full verification belongs to the eventual
PR. Ad hoc receipts remain audit-only. This design phase runs no product tests.

Live activation additionally requires target-side governance tests, real native
acceptance/recovery and identity proof, worktree/revision verification, joint
capacity and health contracts, an approved whole-batch binding, and process
restart tests across claim/submit/acceptance/delivery/result acknowledgement.
Keep execution disabled when any required contract or evidence is absent.

## Dependencies and later slices

| Dependency | Accountable coordination | Required before |
| --- | --- | --- |
| T3 session enforcement, request reconciliation, replay and restart wakes | T3 integration/API owner, identity still to be confirmed | Live execution |
| Non-reserving operation readiness and target authority mapping | Existing Brigade backend/admission owner with T3 owner | Ready status and live execution |
| Durable native correlation storage and versioned closed-event compatibility | Epic 2046 lifecycle owner with relevant backend owner | Persisted native execution |
| Whole-batch binding semantics | Epic 2046 parent/batch lane | First member launch |
| Work/source discovery and correlated owner projection | Existing ownership backend owner plus Hub preview owner for rendering | New owner lookup/display |
| CLI/docs/help/roster overlap | Rebrand owner | Any change to those surfaces |

V0 can be planned without settling those later contracts because it has no
execution or projection path and does not issue authoritative readiness. Adding
persistence, network acquisition, dispatch wiring or a new public field exceeds
V0 and requires a further reviewed design.

## Evidence and limits

Pinned anchors: `src/brigade/run_transport.py:459-467,1045-1109`,
`aboyeur/orchestrator.py:92-118`, `run_tasks.py:23-108,313-432`,
`run_budget.py:1701-1781`, `roster_readiness.py:28-106,130-144,315-480`,
`fleet_model_admission.py:1141-1338`, `fleet_policy_delegation.py:135-264`,
`fleet_owners.py:1-9`, `docs/worklore-ownership.md`, and
`docs/phase-worklore-path-scope-admission.md`.

Native tool declarations come from `native-tool-snapshot-2c565ad.json` and live
catalog schemas read in this session. The prior ownership lane's 11 passing
cases are attributed evidence and were not rerun here. Fleet claim listings
describe execution leases, not complete durable ownership. No new service,
provider execution, admission, claim acquisition or authority mutation occurred
during design preparation. This document promotes the approved local draft to
the repository phase-document location. The source reports and native snapshot
remain local review artifacts under `docs/plans/epic-2046/t3-native-integration-coordination/`.
