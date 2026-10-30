# Governed native child: Brigade-owned governance and recovery

Status: user-approved integration direction, corrected by explicit user instruction on 2026-10-29. All Brigade-specific governance and recovery belong on our side. Native execution remains disabled.

## Direction and responsibility

Fleet Hub is the intended long-term control plane. Use T3's shipped execution, worktrees, task lifecycle and notifications. Brigade owns policy, admission, claims, budgets, verification and provenance for its governed dispatch path. OpenClaw memory, credentials and recurring jobs require separate migration and rollback plans.

The #2046 root owns the integration contract, recovery decisions and end-to-end qualification. Bounded native Sol workers implement released pieces, with Astra for consequential review. Shared admission/reservation backend edits still require explicit user scope assignment. Existing Hub UI/advisory placement and Worklore ownership lanes retain their boundaries.

No upstream T3 changes or enforcement are required by this plan. Do not file an upstream request, await maintainer acceptance, fork T3 or patch its server to satisfy Brigade governance. Adapt our design to supported native capabilities. The earlier upstream-enforcement prerequisite is withdrawn.

## Supported boundary and limits

The trusted integration boundary is the Brigade-owned caller and its durable journal. It validates authority and immutable intent, charges budget, consumes one launch claim and decides whether to invoke the existing native tool. T3 receives supported native inputs and retains execution custody. No Brigade proof is presumed to be consumed by T3.

This guarantee covers calls through the Brigade-owned path. Direct native calls outside that path are not retrospectively governed by Brigade. The first slice does not promise a T3-wide policy fence, atomic authorization-to-provider-start, a hard read-only sandbox or universal cross-session request lookup. Missing capabilities narrow our behavior rather than creating an upstream dependency.

For the first proof, use one same-environment child with T3-managed worktree isolation and one bounded read-only source-check task. Read-only is the authorized task scope, not a claim that a native runtime mode enforces it. Parent-side checks validate the expected source immediately before dispatch. The trusted child workflow checks the actual checkout before task work and supplies source evidence with its result. A source mismatch refuses task work/result acceptance and remains charged. T3 may already have started the provider by that point. Do not claim that a source precheck closes that race.

## Inspected baseline

Brigade source: `a0227f03e8ef6c9106fc022e562f5cded8fcd178`. T3 source: `812c0a71c785c69c34a0111c3ce6976f64cd5bfe`. T3 source retrieval completed at `2026-10-29T23:51:51.507838+00:00`. These are inspection pins, not current installed-build qualification.

| Existing seam | What our integration can reuse | Limit to preserve |
| --- | --- | --- |
| `native_t3_contract.py` | Immutable supplied target/work/authority correlation | Declarations are not authenticated launch grants. |
| `run_tasks.py:449`, `run_task_journal.py` | Exact CAS launch claim, owner locks, durability and replay | A bare claim proves neither budget nor admission. Repeated reads never grant another invocation. |
| `run_budget.py:1701` | Reserve before claim and record the requested dispatch | Pending reservation is process-local before durable dispatch accounting. Orphan claims need a run-wide hold. |
| Existing Hub policy, reservation and admission modules | Brigade-side issuer, loaded policy, current authority and capacity | Native mapping/client support must be implemented on our side where needed. Existing remote-only `t3-fleet` delegation grants no local native authority. |
| Native local worktree delegation | T3 creates an isolated checkout from current parent HEAD | No expected-SHA input in the inspected schema. Verify actual source in our workflow and narrow acceptance claims. |
| Native task handle, terminal run and result transfer | Durable correlation, notifications and exact result readback | Delivery marking is separate from Brigade acknowledgement. A terminal turn does not establish descendant containment. |
| Native request key and receipt replay | Supported same-context deduplication | No assumed cross-session idempotence or authoritative absence. Unknown remains held. |

Source evidence: [native delegate schema](https://github.com/pingdotgg/t3code/blob/812c0a71c785c69c34a0111c3ce6976f64cd5bfe/packages/contracts/src/orchestratorMcp.ts#L168), [local worktree preparation](https://github.com/pingdotgg/t3code/blob/812c0a71c785c69c34a0111c3ce6976f64cd5bfe/apps/server/src/mcp/OrchestratorMcpService.ts#L2240), [request namespace](https://github.com/pingdotgg/t3code/blob/812c0a71c785c69c34a0111c3ce6976f64cd5bfe/apps/server/src/mcp/OrchestratorMcpService.ts#L642), [remote isolation and ordering](https://github.com/pingdotgg/t3code/blob/812c0a71c785c69c34a0111c3ce6976f64cd5bfe/apps/server/src/peer/RemoteDelegation.ts#L213).

## Brigade-owned dispatch sequence

1. Validate current authorized principal, work/source identity, holder/generation, exact task/attempt and expected journal version. Freeze repository/source SHA, payload digest, target environment/project, provider instance/model/options, modes and isolation. Correlate policy/admission/reservation and budget dispatch. Descriptor membership alone is not a full execution fingerprint.
2. Validate current Brigade authority and adopt one existing reservation through an explicitly supported Brigade mapping. Discovery, presence, placement advice and model planning never grant admission. Refusal at this stage produces zero native invocations from our caller.
3. Reserve budget, consume the exact one-use launch claim and durably record the dispatch obligation before the native call. Preserve input custody in existing journal/assets. A replayed claim does not authorize another call.
4. Invoke the shipped native delegation interface once through the actual app-hosted caller. Do not invent a CLI-to-MCP bridge or add unsupported inputs. Persist the returned exact native acceptance before ordinary result handling. Follow native completion notifications and tool instructions.
5. Check actual child source through the trusted workflow and correlate its evidence with the immutable intent. Source mismatch fails qualification and result acceptance. Account for provider work already incurred.
6. Retain and read back the exact reviewed result before Brigade acknowledgement. Establish descendant containment and resolve obligations before releasing capacity. Automatic settlement remains outside this slice.

If authority changes after invocation, our caller prevents further dispatches, records the uncertainty and uses supported cancellation where appropriate. A cancellation request does not establish containment. No native queued-start revocation fence is asserted.

## Conservative recovery

Before any new admission, reconstruct obligations from the existing journal. An unmatched durable launch claim holds the whole run. A matched claim and requested-dispatch fact consume one dispatch under the existing seat/dispatch-attempt binding. Do not introduce another accounting store or automatic refund.

After a lost reply, interrupted correlation write, process restart or provider-session change, use retained exact native acceptance evidence when available. Never infer a child from its title or presence. If supported evidence cannot identify acceptance or containment, preserve `unknown`, budget occupancy and reservation obligations. Do not resubmit, rotate the key, create another reservation or use a fallback transport.

This trades automatic progress for conservative accounting. Restart recovery can legitimately end in an explicit hold requiring an operator decision. It does not require an upstream lookup API or promise automatic exactly-once native execution.

## Qualification cases

| Case | Required evidence from our path |
| --- | --- |
| Missing, stale, revoked or mismatched authority before invocation | Zero native tool calls. No grant from declarations, discovery or placement. |
| Conflicting task/source/payload/target/model/options/modes | Local conflict/refusal before invocation when known. Returned evidence mismatch refuses result acceptance. |
| Parallel callers and replayed local claim | At most one native invocation from the one granted operation. One adopted reservation and one dispatch obligation. |
| Crash after reserve, claim or requested persistence | Conservative reconstruction. An orphan claim holds the run before new admission. |
| Lost acceptance reply or correlation write | Same exact native handle is recovered from supported evidence, or unknown remains occupied without another invocation. |
| Provider-session change | No assumption that a fresh session can replay the old request key. Hold when exact recovery evidence is unavailable. |
| Parent HEAD/source changes | Parent refuses a known pre-call mismatch. Child workflow rejects task work/result acceptance on actual-checkout mismatch. Provider-start prevention is not claimed. |
| Revocation after dispatch, timeout or disconnect | No further governed dispatches or premature capacity release. Cancellation and containment are recorded separately. |
| Terminal delivery, descendants, approvals and duplicate wakes | Exact retained result readback and idempotent acknowledgement. Unresolved execution prevents closeout. |

One accepted nested child has already been manually settled with retained result readback. That does not qualify launch admission, independent sidebar behavior or automatic settlement. Fixture success, installed-build checks and the one authorized live read-only proof must be reported separately.

## Next implementation boundary

Record this user-directed correction without another upstream approval loop. Freeze a concrete Brigade-only caller/journal/budget implementation plan against the shipped native tools. Validate the actual app-hosted call route before introducing any helper. Exact shared-backend edits retain their existing scope/ownership coordination requirement. If the implementation would need unsupported native behavior, reduce its guarantee or hold that operation instead of requesting upstream enforcement.

No new scheduler, thread manager, task database, watcher, service, legacy fallback or settlement sweep. Reassess shipped, exposed and installed T3 capabilities before implementation and through existing maintenance, without installing a new watcher. OpenClaw retirement remains separate.

The user explicitly included [#2166 advisory placement](https://github.com/escoffier-labs/brigade/issues/2166) on 2026-10-30. Consume its exact repository/node/environment/project recommendation with telemetry timestamp and expiry before operation-scoped readiness. A changed target invalidates the prior readiness result. Placement remains non-reserving and cannot grant admission. Reuse the existing Hub candidate evaluator and coordinate its owner rather than duplicating selection or session-count work.

Foundation: [app-hosted design](phase-2046-native-t3-app-hosted-design.md). Preparation: [Brigade-owned qualification plan](superpowers/plans/2026-10-29-governed-native-child-qualification.md).
