# Brigade-Owned Native Child Qualification Implementation Plan

> **For agentic workers:** Use native T3 Sol workers for implementation tasks and Astra for consequential review. On 2026-10-30 the user authorized the governed launch slice and remaining readiness, batches, delivery and qualification work. First freeze the actual executable interfaces below. Native execution remains disabled until the implemented path is qualified. No upstream enforcement dependency is permitted.

**Goal:** Qualify one Brigade-governed native child using only shipped T3 interfaces.

**Architecture:** Fleet Hub is the eventual control plane. Brigade's trusted caller enforces its authority, claim, budget, custody and recovery rules. T3 retains execution and native lifecycle. Unknown acceptance holds the run instead of triggering a new launch.

**Tech Stack:** Existing zero-runtime-dependency Python Brigade and native T3 tools. No T3 source changes, new runtime dependency, service or datastore.

**Spec:** [Brigade-owned governance and recovery](../../phase-2046-governed-native-child-contract.md).

## Global Constraints

- Native execution remains disabled.
- No upstream T3 changes or enforcement are required by this plan.
- Shared admission/reservation backend edits still require explicit user scope assignment.
- Budget admission precedes local launch claim.
- A replayed local claim never grants a new invocation.
- Direct native calls outside the Brigade-owned path are outside its guarantee.
- Missing recovery evidence holds unknown without resubmission, refund or key rotation.
- OpenClaw retirement remains a separate migration milestone.

## Review Focus

- Lost acceptance and provider-session rotation must hold unknown when exact recovery is unavailable.
- Source verification must not be described as a native provider-start fence.
- Claim-only crash history must hold the run before another admission.
- Existing remote-only Hub delegation must not silently authorize a local native child.
- Terminal turns with descendants, questions or approvals must retain unresolved obligations.

---

## Decisions already settled

- [x] User reviewed and approved #2046 root integration-contract accountability and qualification direction.
- [x] User explicitly requires everything Brigade-specific on our side, using T3's shipped capabilities despite rapid upstream change.
- [x] Withdraw upstream enforcement/maintainer acceptance as a release gate. The upstream issue draft is withdrawn and must not be posted.
- [x] Keep one same-environment isolated read-only child as the first proof, with actual source evidence and conservative recovery.
- [x] User explicitly included [#2166 advisory placement](https://github.com/escoffier-labs/brigade/issues/2166). Reuse its owner-supplied candidate evaluation and exact environment/project/repository recommendation before readiness. Recommendation is not admission, and target changes invalidate prior readiness. Do not add a second chooser or edit its in-flight session-count files.

Earlier Sol maps and Astra consultation inspected Brigade `a0227f03e8ef6c9106fc022e562f5cded8fcd178` and T3 `812c0a71c785c69c34a0111c3ce6976f64cd5bfe`. Their source observations remain evidence. Recommendations requiring upstream enforcement are superseded by the user's correction. Those inspection pins do not qualify the current installed build.

## Task 1: Freeze the real Brigade-owned call boundary

**Current files:** This plan and `docs/phase-2046-governed-native-child-contract.md`. No production edit is released by this planning correction.

**Consumes:** Shipped native delegation/status/result behavior and existing Brigade intent, task journal, budget and authority seams.

**Produces:** A concrete executable brief for the actual app-hosted caller. It must name real signatures and exact changed hunks before a worker starts. Do not invent a standalone CLI-to-native bridge.

- [ ] Verify the callable route from the app-hosted parent to existing native delegation and back into Brigade custody. Prefer current tool orchestration over a new adapter layer.
- [ ] Choose the smallest caller change that connects preflight, budget, one-use claim, durable dispatch obligation, one native invocation and exact acceptance recording. A journal-only helper without a real caller is insufficient.
- [ ] Record exact authority/admission mapping. If shared Hub source edits are necessary, obtain the existing bounded scope release and coordinate its hunks. Native discovery is not admission.
- [ ] Freeze interfaces and compatible journal facts using the existing assets and task ledger. Define run-wide hold for orphan claims and ambiguous invocation without requiring native lookup or idempotence beyond observed support.
- [ ] Record current claims/dirty files and Brigade code-graph impact before any non-trivial source edit.
- [ ] Write the exact failing tests, implementation signatures and focused selectors in the executable brief. Preserve the chosen Sol execution route.

Candidate existing surfaces, to narrow to actual changed hunks:

| Responsibility | Existing Brigade surface |
| --- | --- |
| App-hosted dispatch composition | `src/brigade/run_transport.py`, `src/brigade/aboyeur/orchestrator.py` or the verified current caller |
| Durable task/acceptance custody | `src/brigade/run_tasks.py`, `src/brigade/run_task_journal.py` |
| Budget reconstruction and orphan hold | `src/brigade/run_budget.py` |
| Authority mapping if needed and released | Existing `fleet_policy_delegation.py`, `fleet_hub_policy_api.py`, `fleet_hub_model_roster.py`, `fleet_model_admission.py` under `src/brigade/` |

There are no upstream T3 write paths or maintainer acceptance tasks in this plan.

## Task 2: Implement one real governed path

**Gate:** Task 1 has produced actual callable interfaces, exact released paths and a reviewed executable brief. This checklist does not substitute for that brief.

- [ ] Observe intended failing tests and implement only the actual caller's Brigade-owned gates and durable correlation.
- [ ] Assert zero native invocations on preflight refusal and at most one invocation from parallel or replayed local claims. Conflicting immutable input must refuse.
- [ ] Inject crashes around reserve, claim, requested dispatch, native invocation and acceptance recording. Restart keeps one obligation, or holds unknown without another invocation.
- [ ] Prove matched claim/requested accounting counts once and unmatched claims hold the run before any fresh admission.
- [ ] Prove retained result readback and exact acknowledgement, including duplicate wakes and unresolved descendants/approvals.
- [ ] Pass relevant focused verification through Brigade and review the implementation independently. Product source remains zero-runtime-dependency Python.

## Task 3: Qualify one authorized read-only child

- [ ] Refresh shipped, exposed and installed native capability evidence. Adjust our integration when native capabilities change. Do not wait for upstream requests or install another watcher.
- [ ] Verify current parent source before invocation and actual child checkout before task work through the trusted child workflow. Mismatch rejects work/result acceptance and remains budget-accounted even if the provider already started.
- [ ] Run one expressly authorized bounded read-only task through the actual caller, with exact acceptance/result correlation and retained readback.
- [ ] Exercise ambiguous reply/session-loss recovery using supported evidence. A documented occupied hold is an acceptable safe outcome, not proof of automatic recovery.
- [ ] Complete the full PR gate, current-head code/security reviews and protected merge for the actual code slice. Report fixtures, installed checks and live proof separately.

**Terminal state:** Our governed path validates authority, records and accounts one dispatch, invokes native delegation once, retains the exact result and refuses unsafe replay. Direct-call bypass prevention, atomic native provider-start enforcement, automatic exactly-once recovery, remote placement and automatic settlement are outside this first proof.

## Planning verification

Check document links, source hashes, explicit no-upstream direction, withdrawn issue status, prose and handoff structure through Brigade audit receipts. Do not run a product suite for reversible planning edits or claim these checks qualify live execution.
