# Native T3 V0 Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement pure native T3 target/intent validation and lifecycle observation classification, without enabling execution.

**Architecture:** Two new leaf modules consume supplied data and produce immutable internal values or bounded diagnostics. Neither module acquires evidence, persists state, calls a provider, grants launch, marks a result consumed or changes existing transport behavior. Their independent tests permit parallel Codex implementation after parent preflight, followed by integration and Astra review.

**Tech Stack:** Python >=3.10, standard library, existing Brigade reference validators and pytest. No new runtime or development dependency.

**Spec:** [Approved app-hosted design](phase-2046-native-t3-app-hosted-design.md), user approved 2026-10-28. Source review baseline `2c565ad0310caee756a8a85e0966a0466837bef7`. Refresh source assumptions before implementation.

## Global Constraints

- "Production dispatch remains unwired."
- "V0 exposes no submit capability or launch grant."
- "No new external dependency or installation."
- "V0 neither changes that public scope nor weakens those checks."
- "Owner discovery and Hub projection are later coordinated slices, not V0 output."
- "An agent prompt or skill instruction cannot enforce target admission."
- Preserve `brigade`, `.brigade`, `--chef`, persisted/wire chef fields and existing rosters.
- Do not modify CLI/help, roster, readiness, transport, task event schemas, journal, budget, ownership backend, Hub or skills. Correlation types are internal and not a public or persisted schema.
- Exact integers exclude booleans. Evidence time is supplied and retained, never refreshed by normalization. All fixtures use fake identities and timestamps.
- No worker helpers or PR watches. Parent owns integration, verification, reviews and any eventual PR through merge. Existing terminal-state and activation holds remain.

## Review Focus

- Mutable option containers or duplicate keys could alter an accepted intent: freeze/copy and reject duplicates in Task 1.
- `True`, `1`, `NaN`, infinity and future timestamps could evade exact identity/freshness checks: Task 1 pins type and finite-time behavior.
- A canonical model could be substituted for a different launch model, or local discovery reused for a remote target: Task 1 rejects mismatches and retains mapping uncertainty.
- A failed/canceled/completed native turn can still own live descendants: Task 2 requires correlated containment and complete terminal identity for a terminal candidate.
- Unknown fields, raw error strings or forged result handles could leak private data or become authority: both lanes return fixed reason codes, never raw payloads, and Task 3 verifies no grant/consumption or production wiring exists.

## File structure and interfaces

| Files | Owner and responsibility |
| --- | --- |
| `src/brigade/native_t3_contract.py`, `tests/test_native_t3_contract.py` | Lane A: supplied target/discovery/binding evidence, immutable intent and safe qualification diagnostics |
| `src/brigade/native_t3_observations.py`, `tests/test_native_t3_observations.py` | Lane B: originating native task reference, snapshot classification and safe lifecycle diagnostics |
| `docs/phase-2046-native-t3-app-hosted-design.md` | Approved design, parent only |
| `docs/phase-2046-native-t3-v0-plan.md` | Reviewed plan promotion, parent only after plan review |

The two source modules do not import each other. Reuse `run_tasks.TaskHandle` and pure validators from `run_task_journal` where applicable. Imports are not authority checks. Do not invent a shared framework or edit existing modules to accommodate V0.

Private implementation choices fixed by this plan: use frozen dataclasses, immutable tuples, fixed reason tokens and ordinary equality after validation. No new request/batch digest algorithm. Reference strings use existing 512-character provider-reference/token bounds where applicable. Option keys/model names are bounded nonempty safe strings. Do not truncate accepted identity. Reject over-bound or control-containing input with a fixed `ValueError` message. Returned diagnostics contain only fixed status/reason tokens and validated timestamps, never supplied reference values or option payloads.

### Task 0: Parent preflight and execution base

**Files:** approved design and plan documents only. No product edits.

- [ ] Read the approved spec and this plan. Confirm the user has reviewed this plan before invoking an implementation skill.
- [ ] Inspect dirty files, current app workspace/branch, supported fleet claims and durable Worklore ownership through supported reads. An empty execution lease list does not establish no durable owner. Do not take over another owner or print a holder secret.
- [ ] Prepare an isolated, app-bound implementation branch from a verified current main through supported native workspace tools if the old merged branch is unsuitable. Do not reset this checkout, create shell child worktrees or copy ignored artifacts as source truth. Retain absolute access to the approved documents until their tracked promotion reaches the implementation checkout.
- [ ] Refresh the four new paths and their intended existing dependencies against the implementation base. Stop for overlapping changes. Run `brigade code` help, sync/doctor if stale, then `affected`/`impact` on the exact new paths and imported existing paths using the installed syntax. Save the actual source/test impact set. New unindexed paths and graph errors are missing coverage, not zero impact.
- [ ] Check the live roster and eligible Brigade worker route. Substantial implementation must use admitted worker seats. If the route refuses a seat or provider, report the actual refusal and preserve the hold. Do not change a roster or silently replace the route. Retain any required ownership claim privately and renew it while writing.
- [ ] Preserve the user's Codex-only implementation preference: Sol for these harder bounded slices, Astra for review. Use live callable selection and explicit model/settings. The prior successful app delegates do not prove current quota or Brigade admission.
- [ ] Promote the reviewed plan to `docs/phase-2046-native-t3-v0-plan.md`. Keep source reports as local supporting evidence. Document any source-base delta before dispatch.

### Task 1: Lane A, target qualification and immutable intent

**Files:** create only `src/brigade/native_t3_contract.py` and `tests/test_native_t3_contract.py`.

**Interfaces:**

- `NativeTarget`: origin environment/thread, target environment/project, provider instance, driver, explicit launch model, sorted unique option pairs, runtime/interaction mode, isolation and source revision. Identity/mode fields are strings. Options are `tuple[tuple[str, str | bool], ...]`. Revision is exactly 40 lowercase hexadecimal characters. Valid isolation is `shared` or `worktree`.
- `WorkBinding`: authority reference, work ID, canonical repository, source reference and owner node as strings, plus `ownership_revision: int` and `generation: int`. Revision is nonnegative and generation is positive. It contains no holder nonce, hash or execution lease token.
- `ModelDeclaration(model: str, options: tuple[tuple[str, tuple[str | bool, ...]], ...])`: each option key occurs once and declares its exact allowed values. Boolean and string values remain distinct.
- `DiscoveryEvidence`: explicit environment/project/provider-instance/driver context as strings, original `observed_at: float`, `expires_at: float`, `child_enabled: bool`, `runtime_modes: tuple[str, ...]` and `models: tuple[ModelDeclaration, ...]`. These are normalized supplied facts, not a new native API envelope. Missing context cannot inherit from the current session. An expiry is a supplied evidence bound, not a claim that native discovery publishes one. Runtime mode must be explicitly declared. Interaction modes are `default` or `plan`. Shared/worktree API declaration does not establish target isolation eligibility.
- `BindingEvidence`: `consumer`, `seat`, `canonical_model`, `launch_model`, `roster_digest` and `policy_digest` are strings. `roster_revision` and `policy_version` are nonnegative exact integers. `observed_at` and `expires_at` are finite floats. `refusal` is absent or a fixed recognized policy refusal token, never arbitrary error prose. Reuse the existing policy refusal vocabulary without modifying its module. No target authority mapping is invented. A supplied projection is not authenticated by this pure code.
- `Qualification(status: Literal['declared', 'unknown', 'refused'], reasons: tuple[str, ...], observed_at: float | None, expires_at: float | None)`. `declared` describes matched declarations only. All four operation-readiness facts remain unknown and no authoritative readiness object is emitted.
- `qualify_target(target: NativeTarget, *, consumer: str, seat: str, discovery: DiscoveryEvidence | None, binding: BindingEvidence | None, now: float) -> Qualification`. Match the explicit consumer and seat as well as the declared launch model.
- `AuthorityReference(kind: str, reference: str, expires_at: float | None)`, limited to policy, decision, delegation, admission, reservation, launch-proof and budget-dispatch reference kinds. These are correlation only, never validation or permission.
- `NativeIntent(work: WorkBinding, handle: TaskHandle, dispatch_attempt: int, target: NativeTarget, payload_context_digest: str, batch_binding_ref: str, native_client_request_key: str, authority_refs: tuple[AuthorityReference, ...])`. Require a prepared attempt and positive exact dispatch ordinal. Authority references may be absent, but duplicates or unknown kinds are refused. No acceptance fields live here.
- `compare_intents(original: NativeIntent, candidate: NativeIntent) -> Literal['identical', 'conflict']`. Compare all validated fields, including reference expiry. Do not generate a new client key or infer batch acceptance.
- `qualification_diagnostic(result: Qualification) -> dict[str, object]`: only fixed status/reason tokens and validated timestamps. Never `repr` or echo caller content.

- [ ] **Step 1: Add failing public-behavior tests.** Test fake selected target `env-target`/`project-test`, provider `codex-test`, model `launch-model` and option `reasoningEffort=high`. Matching supplied declarations may return `declared`, but never `ready` or launch authority. Absent evidence returns `unknown`. Fresh disabled/denied evidence refuses. Retain original observation/expiry. Test expiry, time travel, `NaN`, infinity, stale evidence older than the existing 30-second operation TTL and a different environment/project/provider/driver.
- [ ] **Step 2: Add immutable-intent cases.** Build a valid prepared `TaskHandle` using the existing types and fake digests. Exact replay is `identical`. Parametrize changed work, holder generation/revision, run/request/task/spec/attempt, dispatch ordinal, target, source revision, model/options, payload digest, batch reference, client key and authority reference as `conflict`. A repeated seat with a different task remains distinct. Mutating caller-owned option input must not change an existing value. Duplicate options and exact-integer boolean substitutes refuse. A Hub canonical model matching the target is insufficient when its explicit launch model differs.
- [ ] **Step 3: Observe RED through Brigade.** Run the lane's new selector via the focused command below. Expected initial failure is missing `brigade.native_t3_contract` or the newly specified interface. An environment, lint or unrelated failure does not establish intended RED.
- [ ] **Step 4: Implement the specified pure interfaces.** Copy/canonicalize containers at construction, use finite supplied times and retain original evidence. Fresh authoritative refusal takes precedence over missing declarations. Stale refusal remains historical and cannot create fresh denial. Mapping to authoritative native governance stays unknown. Opaque batch reference is compared, never interpreted or hashed into acceptance.
- [ ] **Step 5: Pin safe diagnostics.** Give extra caller mappings secret-shaped sentinel values and arbitrary raw errors. Assert none appear in diagnostic output or exception messages. Unknown raw material is neither retained in public values nor stringified. Oversized references refuse without truncation.
- [ ] **Step 6: Observe GREEN through Brigade.** Run `./scripts/verify-focused tests/test_native_t3_contract.py` through the wrapper. Expected exit 0 with all lint/format/sync/snapshot/type checks and this selector passing. Record actual counts and receipt, including any attribution warning.
- [ ] **Step 7: Commit locally.** Conventional message `feat: add native T3 intent qualification contracts`, with the required Codex co-author trailer. Return commit, exact receipt and concerns. No push, PR, additional file changes or helpers.

### Task 2: Lane B, native lifecycle observation classification

**Files:** create only `src/brigade/native_t3_observations.py` and `tests/test_native_t3_observations.py`. May run in parallel with Task 1 after Task 0 because no shared file or type is required.

**Interfaces:**

- `NativeTaskReference(origin_environment_id: str, native_client_request_key: str, task_id: str | None = None, remote_environment_id: str | None = None, remote_thread_id: str | None = None)`. The remote pair must be both present or both absent, and requires a known task. A missing task ID preserves uncertain acceptance under the original client key. It cannot adopt an arbitrary snapshot's task ID, regrant launch or infer nonacceptance. A remote thread is never substituted for the originating task ID.
- `NativeObservation(family: Literal['refused', 'awaiting', 'active', 'terminal-candidate'], reason: str, reference: NativeTaskReference, status: str | None, terminal_run_id: str | None, result_transfer_id: str | None, containment: Literal['unknown', 'active', 'quiescent'])`. It carries no grant, refund, fallback, acknowledgement or consumable result ID.
- `classify_native_snapshot(expected: NativeTaskReference, snapshot: Mapping[str, object] | None, *, observed_origin_environment_id: str | None, containment: Literal['unknown', 'active', 'quiescent'] = 'unknown') -> NativeObservation`.
- `observation_diagnostic(result: NativeObservation) -> dict[str, object]`: fixed family/reason tokens only. Native references remain available internally, not reflected in this diagnostic.

`snapshot` is the structured task-status result shape from the pinned native tool declaration, not the MCP content wrapper. Recognize `taskId`, `status`, `workState`, `hasPendingChildRuns`, `hasPendingQuestion`, `pendingApproval`, `waitTimedOut`, `remoteChild`, `latestTerminalStatus`, `latestTerminalRunId`, `latestTerminalResultContextTransferId` and `resultContextTransferId`. Native terminal status values are `completed`, `failed`, `cancelled` and `interrupted`. Terminal candidate requires `workState='result_available'`, both pending booleans explicitly false, `pendingApproval=None`, no wait timeout, matching task/terminal statuses, terminal run and quiescent containment. Use the non-null result-transfer reference from the two named fields, but require equality if both exist. Child run IDs are not substituted for terminal run IDs. Ignore unrelated unknown fields without retaining or rendering them. Require exact types for recognized fields. Missing or inconsistent required evidence becomes `awaiting` with a fixed reason. Wrong scope/task/remote pair yields bounded identity refusal and never a native action. No raw error text is reflected.

- [ ] **Step 1: Add failing classification cases.** `None`, lost acceptance with no task handle, missing or mistyped child/question flags, wrong task/origin/remote pair and malformed recognized fields cannot become a terminal candidate. Unknown extra fields cannot supply authority. Verify originating task ID remains distinct from child thread/run IDs.
- [ ] **Step 2: Add active/wait cases.** Running/queued work with valid identity classifies active. Pending input/approval, wait timeout, `waiting_for_children`, or any pending child remains awaiting. A completed, failed, canceled or interrupted turn with active descendants cannot be terminal. A cancellation request proves no containment.
- [ ] **Step 3: Add terminal correlation cases.** With matching origin/task/remote identity, terminal task status, no pending child/question/approval, matching latest terminal status, original terminal run, original result-transfer identity and explicit quiescent containment, return `terminal-candidate`. Parameterize each missing or contradictory fact to retain awaiting. Default containment is unknown even when pending children is false. Duplicate snapshot returns an equal classification and causes no acknowledgement or persistence.
- [ ] **Step 4: Observe RED through Brigade.** Run the new selector through the focused wrapper. Expected intended failure is missing `brigade.native_t3_observations` or its new interface, not an unrelated gate failure.
- [ ] **Step 5: Implement the specified pure classifier.** Do not treat the latest completed native turn as overall task completion. Do not infer target containment from expiry, failed status, canceled status or a false child flag. The explicit containment parameter is supplied evidence, not an authenticated proof established by this module.
- [ ] **Step 6: Observe GREEN through Brigade.** Run `./scripts/verify-focused tests/test_native_t3_observations.py` through the wrapper. Expected exit 0 with checks and named tests passing. Record exact counts, receipt and limitations.
- [ ] **Step 7: Commit locally.** Conventional message `feat: classify native T3 lifecycle observations`, with the Codex trailer. Return commit, evidence and concerns. No additional files, push, PR or helpers.

### Task 3: Parent integration, review and publication readiness

**Files:** integrate only the two lanes' four files and approved design/plan. No existing runtime modules are modified.

- [ ] Read each actual terminal result and diff. Verify file ownership, base, immutable inputs and actual RED/GREEN receipts. Resolve small confirmed findings inline. New delegate review rounds use new tasks, never follow-ups to a completed review.
- [ ] Integrate local commits through the approved app-bound branch. Check no production importer or transport hook references the new modules, no submit/launch API exists and no existing closed event schema or readiness scope changed. This is a source check, not a test that merely mirrors filenames.
- [ ] Exercise a compact set of combined scenarios in the existing new test files only if the integrated code exposes an unresolved boundary concern. Do not add a third module or test file for scaffolding. Reuse the already meaningful adversarial cases rather than duplicating them.
- [ ] Run one integrated focused gate with both new selectors plus `tests/test_run_task_launch.py`, `tests/test_run_tasks.py` and `tests/test_roster_readiness.py`. These nearby contracts verify retained launch/readiness behavior. Add any meaningful existing selectors found in actual graph impact and explain additions.
- [ ] Request a fresh exact-head Astra review of V0 against the approved spec and this plan. Review format distinguishes production defects, false authority, privacy/reflection and missing meaningful tests. Confirm configured native code/security review and any required security route for eventual publication without inventing coverage or bypassing a required gate. No duplicate or paid scan.
- [ ] Fix verified issues, rerun only affected focused checks, and obtain current-diff review as required. Do not weaken a test judged wrong without the user's agreement.
- [ ] Before an eventual PR, run the full Brigade gate once with timeout 3600 and no `--no-reuse`. A slot refusal/status 75 does not authorize a larger-timeout retry. Do not call the full gate a substitute for CI-only checks.
- [ ] Follow standing in-house publication authority: content guard, conventional commits/trailer, push without bypass, authored PR link as the very next tool call, configured deliberate review and current-head CI. Watch only if waiting on checks/reviews, then end the turn. Unwatch before handback. Merge only through eligible gates, then verify actual merged state. No deployment or execution activation.
- [ ] Write and lint a durable handoff recording the internal-only contract, evidence and remaining activation dependencies. Notify coordinated owners of a settled display contract only when one actually exists. V0 creates none, so it does not trigger a Hub projection implementation.

## Verification command form

Use the correct checkout's development environment. The existing checkout has `.venv/ci312`, but workers must verify their own environment rather than copy executable paths or assume it exists. With a valid environment:

```bash
PATH="$PWD/.venv/ci312/bin:$PATH" PY="$PWD/.venv/ci312/bin" PYTHONPATH="$PWD/src" \
  .venv/ci312/bin/brigade work verify run --target . \
  --argv-json '["./scripts/verify-focused","tests/test_native_t3_contract.py"]' \
  --capture brigade-work
```

`PY` is the binary directory because the repository scripts invoke `$PY/python`, `$PY/ruff` and `$PY/mypy`. Substitute the actual selectors for lane B/integration. Full PR gate uses `--argv-json '["./scripts/verify"]' --timeout 3600`. Observe completion and read the actual receipt. Ad hoc receipts are audit-only, and an unknown installed skill warning does not establish attributed/scoring evidence. Do not repeat a passing gate without a new change, failure or unresolved concern.

## Self-review

- Spec coverage: target/evidence/intent/privacy in Task 1, lifecycle and terminal uncertainty in Task 2, no-wiring, legacy contracts and review in Task 3. Persistence, acquisition, owner discovery and live activation are expressly excluded.
- Interface consistency: lanes have separate immutable reference types, no cross-import or shared writer. Task 3 adds no new API. Existing `TaskHandle` is read-only input.
- Every failure class in Review Focus has an explicit owning test step. Pure test results do not qualify real durability, native enforcement or readiness.
- The plan does not settle batch hashing/order/multiplicity acceptance, target admission mapping or backend ownership. No fixed point above is a new public/wire contract.
- Worker dispatch, graph impact and execution ownership are real preflight requirements. The plan records no unobserved passing checks and does not authorize silent fallback after refusal.

## Terminal state

The user approved this plan on 2026-10-28. Implementation is authorized subject to the recorded preflight and routing requirements. Preserve the already requested parallel Codex method, with Astra review. Do not ask the user to choose an execution method again. On approval, invoke subagent-driven-development and execute only V0 through the admitted route. Native launch, durable correlation persistence, authoritative readiness, ownership discovery and Hub projection remain held.
