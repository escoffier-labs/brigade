# Durable Descriptor Membership Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retain and reconstruct exact ordered descriptor membership through one bounded checkpoint-paired journal fact, with replay and selective redaction.

**Architecture:** Extend the existing task journal and fold. Preflight both records before publishing a new aggregate pair, retain complete descriptor rows, and reuse existing member lifecycle and archival rewrite machinery. Production native execution stays disconnected.

**Tech Stack:** Existing Python standard library, canonical JSON, SHA-256, pytest and Brigade verification. No new runtime dependency.

**Spec:** [Approved descriptor-membership design](phase-2046-batch-membership-design.md).

Status: implementation plan awaiting user review. The user has already selected parallel Codex workers, with Sol building and Astra providing architectural and final opinions. Preserve that method after plan review.

## Global Constraints

- Retain exactly `task_id`, `seat` and `payload_digest` for each member. No dependencies, execution bytes, target, source revision or public request dictionary enters this API.
- Accept 1 through 8 ordered members. Preserve the 512-character scalar bound, 16,384-byte line bound, 2,048-event run bound and 8 MiB journal bound.
- Event: `task.batch-accepted`, schema v2 only. Payload keys: `descriptor_version`, `request_id`, `members`, `membership_digest`. Descriptor version: integer 1, excluding booleans.
- Exact replay appends nothing and returns ordered current member states. Changed rows, order, count or version under one outer request refuse before writes.
- Outer membership requests form a separate namespace. Derived child requests share the existing task request namespace and require actual collision checks.
- New writes require the real active owner. Reads use the existing fresh durability barrier. Incomplete task pairs refuse, including historical orphans followed by unrelated pairs.
- Acceptance grants no ownership, admission, budget or launch permission. Preserve consumed claims, member CAS and acknowledgement semantics.
- Redaction stays terminal and authoritative, verifies the original history, preserves unrelated fields and batches, and never regrants permission.
- No public CLI, scheduler, transport, task store, daemon, recurring schedule, Hub projection, memory API or native activation.
- Follow repository AGENTS.md. Never weaken a failing test. A test judged wrong requires agreement before editing. Use conventional commits with the required Codex co-author trailer.

## Review Focus

1. A journal has room for the checkpoint but not the aggregate: deterministic refusal must leave both records absent. Task 1 owns this proof.
2. A valid ordinary task request equals a derived batch child key: the whole batch must refuse, without adopting the task. Task 2 owns this proof.
3. Redacting one member changes its derived references while a provider/control field aliases a derived ID: nested and derived aliases must disappear without scrubbing unrelated sibling fields. Task 3 owns this proof.
4. A valid later checkpoint follows an orphaned aggregate checkpoint: both read and write operations must still refuse. Task 4 owns this proof.
5. A replay follows a consumed claim and an acknowledged terminal result: it must return that current state, never queued state or another grant. Tasks 2 and 4 own this proof.

---

## File Structure and Impact

| Owner | Production files | Responsibility |
| --- | --- | --- |
| Task 1 | `src/brigade/run_journal.py`, `run_checkpoint.py`, `run_lifecycle.py` | Narrow pair-bound preflight through existing writers. |
| Task 2 | `src/brigade/run_task_journal.py`, `run_events.py`, `run_tasks.py`, `run_projector.py` | Closed shape, identity helpers, aggregate fold, acceptance and scoped read. |
| Task 3 | `src/brigade/run_redaction_tasks.py`, `run_redaction.py` | Nested membership selection and coherent archival rewriting. |
| Task 4 | No production ownership | Process, crash, concurrency, compatibility and lifecycle acceptance proofs. |

Task 1 extends the six-file estimate in the design by three files. Source inspection showed `write_checkpoint` publishes before `append_event` enforces event-line and remaining-journal bounds. The parent accepts these three narrow feature changes to satisfy the approved preflight invariant. This permits no general writer redesign or unrelated refactor.

Actual planning impact: `brigade code sync .` succeeded, then `brigade code affected` on all nine paths returned 76 statically attributed test files and no missing files at the design branch. Full output is recorded in the local coordination artifact `batch-membership-affected-fe1ebaa0.json`. This is a lower bound, not coverage. It includes journal/checkpoint/lifecycle, task/launch/redaction, projector/shadow, budget, transport, native contract, fleet sync and export consumers. Executors must refresh exact path impact on their actual base before edits. Do not run all 76 during every focused iteration.

## Frozen Interfaces

Put the constants and pure dictionary-based identity helpers in `run_task_journal.py`, avoiding an import cycle with `TaskSpec`:

```python
BATCH_DESCRIPTOR_VERSION = 1
MAX_BATCH_MEMBERS = 8

def batch_membership_digest(*, descriptor_version: int, members: Sequence[Mapping[str, Any]]) -> str: ...
def batch_child_request_id(*, run_id: str, request_id: str, task_id: str) -> str: ...
def batch_reference(*, run_id: str, request_id: str, membership_digest: str) -> str: ...
```

Use SHA-256 of existing canonical bytes for these exact objects:

- Membership: `{"domain": "brigade.task-batch.members.v1", "descriptor_version": 1, "members": rows}`.
- Child request: `{"domain": "brigade.task-batch.child.v1", "run_id": run_id, "request_id": request_id, "task_id": task_id}`.
- Batch reference: `{"domain": "brigade.task-batch.ref.v1", "run_id": run_id, "request_id": request_id, "membership_digest": membership_digest}`.

Persist only the approved event payload, with rows in supplied order. Derive child requests and batch reference on replay. Hashing a supplied digest does not retain or validate omitted execution inputs.

Add these internal values and operations to `run_tasks.py`:

```python
@dataclass(frozen=True)
class TaskBatchResult:
    run_id: str
    request_id: str
    descriptor_version: int
    membership_digest: str
    batch_ref: str
    members: tuple[TaskResult, ...]

    @property
    def specs(self) -> tuple[TaskSpec, ...]: ...

def accept_task_batch(
    run_dir: Path, *, workspace: Path, request_id: str,
    specs: tuple[TaskSpec, ...], descriptor_version: int = 1,
) -> TaskBatchResult: ...

def read_task_batch(run_dir: Path, *, request_id: str) -> TaskBatchResult: ...
```

Require a tuple of validated `TaskSpec` values, take an immutable scalar snapshot, and reject unsupported richer input rather than projecting it silently. Invalid input and history surface bounded `TaskJournalError` diagnostics. Unknown batch reads refuse. `specs` derives the immutable tuple from current member handles.

Keep `_fold(run_id, events) -> dict[str, TaskResult]` as a compatibility wrapper around one private fold that also returns batch metadata. Do not maintain a second fold with different validation rules. The private representation is the Task 2 builder's choice, and must support Task 3 redaction without a separate durable store.

## Execution Preconditions

- [ ] Confirm the plan is reviewed. Recheck applicable instructions, dirty files and current ownership. Before fleet work inspect supported fleet status and claims. Empty execution claims do not prove absent durable Worklore ownership.
- [ ] Refresh the admitted Codex worker route and code-graph brief. Planning inspection found roster seat `sol61` configured with model `gpt-6.1-sol` and implementation role. This is configuration evidence, not proof of live admission, quota or health. Use the installed `brigade run` help and governed `--worker sol61` route if admitted.
- [ ] Do not inherit the PR #2113 V0 native-worker exception, alter the roster, suppress admission or silently switch transport. If the required route refuses, retain the concrete error and finish independent planning/review. A different execution route needs current authority.
- [ ] Preserve existing isolated workspace. A separate worker checkout must be app-owned through native tools, never created in a shell or in its prompt. Give workers only their owned files and the approved spec plus this plan.
- [ ] Run Tasks 1 and 2 sequentially with fresh task review. After Task 2's interfaces are integrated, Tasks 3 and 4 may run in parallel in isolated checkouts. Their new test files are disjoint. A Task 4 production defect is returned to its owning lane rather than patched concurrently.

## Task 1: Pair Bounds Before Checkpoint Publication

**Files:** Modify `src/brigade/run_journal.py:1083`, `src/brigade/run_checkpoint.py:997` and `src/brigade/run_lifecycle.py:816`. Create `tests/test_run_task_pair_preflight.py`.

**Interfaces:** Consumes current envelope builder and task transaction. Produces:

```python
def preflight_new_event_pair(
    journal_path: Path, *, run_id: str,
    requests: Sequence[tuple[str, Mapping[str, Any], str]],
) -> None: ...
```

The sequence must contain exactly two new requests, checkpoint first and its paired task fact second. Add optional `following_task_request: tuple[str, Mapping[str, Any], str] | None = None` to `write_checkpoint`. Existing callers omit it and retain their behavior. Task 2's aggregate path supplies it through lifecycle publication.

- [ ] **Write RED tests.** Reuse `tests.test_run_tasks.task_run`. `test_pair_capacity_refusal_precedes_checkpoint_publish` sets each limit to permit one record but not the actual pair, invokes `write_checkpoint` with a valid existing `task.accepted` following request, and asserts:

```python
assert journal_path.read_bytes() == before_journal
assert sorted(checkpoint_dir.iterdir()) == before_assets
```

Also add `test_pair_preflight_is_nonmutating`, `test_pair_preflight_refuses_existing_keys` and `test_pair_preflight_validates_both_lines`. Assert valid preflight leaves all bytes unchanged, either existing key refuses before mutation, and an invalid/oversized second request cannot publish a first record. Use actual serialized envelopes and new keys, not approximate lengths.

- [ ] **Observe RED through Brigade.** Run the focused command below with `tests/test_run_task_pair_preflight.py`. Expected: missing helper/keyword failures or demonstrated checkpoint publication. Record actual output, not this forecast.
- [ ] **Implement preflight.** Under the existing mutation lock, read the bounded verified tail, reject partial/corrupt history, construct both chained envelopes using existing canonical serialization, and enforce line/count/cumulative-byte limits. Refuse reused pair keys, including exact duplicates, because this helper is for two new records. Validate pair type/key correspondence. Make no ensure/create/append call. Fixed-width timestamp encoding must be identical in size to the actual writer's encoding.
- [ ] **Integrate optional checkpoint preflight.** Compute the existing checkpoint payload and key before publication. When the following request is present, verify it matches the paired task type and pairing digest, then preflight both records before `publish_checkpoint_file`. Preserve body stripping, degradation, owner checks, lock order and old default behavior. Lifecycle passes this argument only for `task.batch-accepted` once registered in Task 2.
- [ ] **Verify GREEN.** Run focused selectors `tests/test_run_task_pair_preflight.py`, `tests/test_run_journal.py`, `tests/test_run_checkpoint.py`, `tests/test_run_lifecycle.py` and `tests/test_run_task_journal.py`. Exit 0 from the complete focused gate is required. No full gate yet.
- [ ] **Commit and request fresh task review.** Suggested subject: `feat(run): preflight bounded task checkpoint pairs`. Return commit, actual receipts, owned diff and unresolved concerns. Parent fixes small findings and obtains re-review when material.

## Task 2: Aggregate Schema, Folding and Internal API

**Files:** Modify `src/brigade/run_task_journal.py:65`, `run_events.py:198,461`, `run_tasks.py:215,277,352,412` and `run_projector.py:167`. Create `tests/test_run_task_batches.py`. Extend `tests/test_run_events.py` and `tests/test_run_projector.py` only where their registry contracts require it.

**Interfaces:** Consumes Task 1's optional pair preflight. Produces all frozen interfaces above, with coherent legacy and aggregate task folding.

- [ ] **Write RED API tests.** Import the existing `task_run` fixture. `test_batch_replay_retains_complete_ordered_membership` accepts two distinct IDs on the same seat and digest, then asserts:

```python
specs = (tasks.TaskSpec("one", "worker", "a" * 64), tasks.TaskSpec("two", "worker", "a" * 64))
first = tasks.accept_task_batch(run_dir, workspace=workspace, request_id="batch-one", specs=specs)
assert first.specs == specs
assert tuple(m.status for m in first.members) == ("queued", "queued")
assert tasks.read_task_batch(run_dir, request_id="batch-one") == first
before = journal_path.read_bytes()
assert tasks.accept_task_batch(run_dir, workspace=workspace, request_id="batch-one", specs=specs) == first
assert journal_path.read_bytes() == before
```

Add `test_batch_identity_conflicts_are_nonmutating` for reorder, disjoint rows, subset/superset, seat/digest change, duplicate IDs, occupied task ID, version 2 and boolean version. Check exact bytes after every refusal. Add empty/nine-member, malformed nested row, extra dependency field and unsupported rich-input cases. A version-1 request token may equal a standalone request token without conflating their namespaces.

- [ ] **Write collision and live-state RED tests.** `test_batch_child_request_collision_refuses_all_members` first accepts a standalone task using the actual derived child key, then asserts batch refusal and unchanged bytes. `test_batch_replay_preserves_current_member_versions` prepares/claims/observes/acknowledges a member, checks sibling version stability and returns consumed/acknowledged state on replay. Use valid ownership and require a terminal decision when claiming a terminal task, never accept an owner error as success.
- [ ] **Observe RED.** Run focused selector `tests/test_run_task_batches.py` through Brigade. Expected missing API and unsupported event failures. Inspect actual failures.
- [ ] **Implement closed schema and helpers.** Add only the exact `members` nested exception after dedicated validation. Preserve generic scalar rejection. Check row keys, field validators, count, version and recomputed membership digest. Register schema-v2 task type and projector status neutrality. No placeholder `task_id` or JSON-string manifest.
- [ ] **Implement shared fold and APIs.** Handle aggregate facts before single-task indexing. Validate outer uniqueness, child key and task ID collisions in both history replay and new acceptance. Duplicate aggregate facts are invalid history. Insert every member with the aggregate digest as its initial version, maintain prior per-member facts and used dispatch bindings, and preserve `_fold` compatibility. Repair aggregate-unsafe single-task indexing in launch-history lookup. Scope batch reads to their retained rows and return current states.
- [ ] **Wire preflight and owner checks.** Use the existing active-owner transaction and paired append. Validate exact replay only after all-history semantic validation and fresh barrier. New aggregate writes pass the full following request to Task 1 preflight. A read/accept after malformed history must return bounded refusal, never raw `KeyError` or `TypeError`.
- [ ] **Verify GREEN.** Run focused selectors `tests/test_run_task_batches.py`, `tests/test_run_task_pair_preflight.py`, `tests/test_run_tasks.py`, `tests/test_run_task_launch.py`, `tests/test_run_task_journal.py`, `tests/test_run_events.py` and `tests/test_run_projector.py`.
- [ ] **Commit and request fresh task review.** Suggested subject: `feat(run): persist bounded descriptor batch membership`. Keep publication held until redaction and acceptance proof tasks complete.

## Task 3: Relationship-Aware Batch Redaction

**Files:** Modify `src/brigade/run_redaction_tasks.py:12,27` and `src/brigade/run_redaction.py:443,1988`. Create `tests/test_run_redaction_batches.py`. Preserve existing `tests/test_run_redaction_tasks.py` as regression coverage.

**Interfaces:** Consumes Task 2's validated aggregate and identity helpers. Preserve `validate_history`, `prepare_requests` and `_assert_affected_values_removed` signatures. Produce coherent rewritten aggregate/member/control history accepted by the same task fold.

- [ ] **Write RED transaction tests.** Build temporary terminal authoritative runs through real public task APIs with two batches, one standalone task and distinct fake provider IDs unless a case deliberately tests an alias. `test_batch_member_redaction_preserves_unrelated_membership` selects one later member fact, then asserts:

```python
assert private_selected_ref not in rewritten_bytes.decode()
assert untouched.specs == unrelated_specs
assert rewritten_selected.members[0].acknowledged
assert rewritten_selected.members[0].launch_claim is not None
```

Exercise aggregate-only, observation-only, acknowledgement-only, targeted-control-only and interleaved selections. Read rewritten batch identities from rewritten retained history, with no assumption that old references remain valid.

- [ ] **Add RED alias/corruption tests.** `test_batch_redaction_removes_nested_and_derived_aliases` places old/recomputed task, spec, child request, attempt and result IDs in provider/control aliases, and includes nested values equal to replacement candidates. Assert affected aliases disappear, structural version/count/order survive and unselected sibling fields remain. `test_corrupt_batch_redaction_refuses_without_replacement` corrupts duplicate membership, commitment or task/control identity, rebuilds envelope checksums, and requires semantic refusal with original journal/quarantine publication state unchanged.
- [ ] **Observe RED.** Run the new redaction selector through Brigade and record the unsupported aggregate/indexing/refusal evidence.
- [ ] **Implement aggregate relationship discovery.** Find containing rows for selected member/control facts. Maintain separate outer and member identities. Keep an unselected outer token unless selected or aliased. Enumerate nested reserved strings, seed existing selected-value/derived-alias closure, and rebuild row specs, child requests, attempts, results, commitment and controls through shared helpers. Preserve unrelated fields and batches. Related sibling child keys may change when their outer request changes.
- [ ] **Extend final affected-value validation.** Check nested aggregate scalars explicitly, exempt only structural descriptor version/count/order semantics, and preserve the existing original-history verification and checkpoint/chain rewrite. Validate the entire rewritten semantic history before replacement. No native recovery claim or redaction-triggered launch grant.
- [ ] **Verify GREEN.** Run focused selectors `tests/test_run_redaction_batches.py`, `tests/test_run_redaction_tasks.py`, `tests/test_run_redaction.py`, `tests/test_run_task_batches.py`, `tests/test_run_task_launch.py` and `tests/test_run_task_journal.py`.
- [ ] **Commit and request fresh task review.** Suggested subject: `feat(run): preserve batch identities through task redaction`. Return alias limitations explicitly, without changing unrelated fixtures to make a broad absence assertion pass.

## Task 4: Recovery, Concurrency and Compatibility Proof

**Files:** Create only `tests/test_run_task_batch_recovery.py`. Reuse `tests.test_run_tasks` fixtures and existing journal fault-injection helpers. No production or Task 3 test ownership.

**Interfaces:** Consumes Task 2's accepted API and Task 1's bounds. Proves the public internal contract through process restart, real locks and actual journal bytes. Can run in parallel with Task 3 after Task 2 integration.

- [ ] **Write process and concurrency tests.** `test_batch_restart_enumerates_without_caller_rows` starts a separate Python process, acquires a valid new owner, calls `read_task_batch` without descriptor arguments and compares ordered handles/current states. Exact replay then appends nothing. `test_concurrent_batch_acceptance_has_one_fact` races identical and conflicting inputs under the actual ownership/transaction contract with barriers and bounded joins. Assert one aggregate and all-or-no membership. Do not fake owner checks.
- [ ] **Write crash tests.** `test_batch_crash_windows_refuse_or_replay_complete_membership` injects before checkpoint, checkpoint-only, partial line, complete bytes with failed barrier and lost return. Assert actual preserved bytes, bounded refusal for damage, and fresh-barrier replay for complete history. `test_later_pair_cannot_hide_batch_orphan` appends a later real unrelated pair and requires both read and accept to refuse without more bytes.
- [ ] **Write compatibility and authority tests.** `test_batch_unsupported_reader_refuses_without_ignoring_fact` removes the new registration in an isolated reader process. `test_batch_version_and_hostile_history_refuse_boundedly` constructs chain-valid invalid rows, duplicate aggregates and version mismatch. `test_batch_is_status_neutral_and_preserves_next_authoritative_write` checks run status/cursors and a real subsequent authoritative write. `test_batch_replay_does_not_regrant_terminal_claim` uses valid owner, consumed claim and acknowledged result. Patch native/planner/synthesis/transport entry points to fail if invoked by batch operations.
- [ ] **Run the proof selector through Brigade.** Expected: pass against correct production, or a meaningful defect. This task is acceptance coverage, so do not force an artificial RED production change. Return a source-traced failure to the owning lane, with bytes/receipt and no concurrent production edits.
- [ ] **Verify and commit.** Run focused selectors `tests/test_run_task_batch_recovery.py`, `tests/test_run_task_batches.py`, `tests/test_run_task_journal.py`, `tests/test_run_task_launch.py`, `tests/test_run_checkpoint.py` and `tests/test_run_shadow.py`. Suggested subject: `test(run): prove descriptor batch recovery and compatibility`. Request fresh test review, distinguishing simulated faults from platform/runtime qualification.

## Verification Commands and Integration

Use the repository's development tools directory, not a Python executable, for `PY`. The current reusable environment is `.venv/ci312/bin`. Resolve the equivalent existing environment in each app-owned worker checkout, and point `PYTHONPATH` at that checkout's source.

```bash
PATH="$PWD/.venv/ci312/bin:$PATH" PY="$PWD/.venv/ci312/bin" PYTHONPATH="$PWD/src" \
  .venv/ci312/bin/brigade work verify run --target . \
  --argv-json '["./scripts/verify-focused","tests/test_run_task_batches.py"]' \
  --capture brigade-work
```

Replace the selector array with each task's listed selectors. The focused script runs lint, format, mypy, version and snapshots before named tests. Record actual RED/GREEN receipts and exact output. Ad hoc receipts are audit-only. Missing installed-skill attribution does not authorize installing skills or claiming an artifact score.

- [ ] Integrate clean reviewed worker commits with one accountable parent. Resolve conflicts without discarding unrelated changes. Review the combined diff against the approved spec and all five Review Focus cases.
- [ ] Run the combined focused selectors once after integration. Include the new proof and redaction modules plus existing task/journal/launch/events/projector/checkpoint/lifecycle/shadow/redaction regressions. Broaden for new failures or uncovered concerns, not repeated successful checks.
- [ ] Request fresh Astra whole-branch review of the exact source range and substantive security review under the approved available route. Native Codex PR reviews remain complementary. No paid credits, unavailable-model claims or duplicate scans. Fix verified findings, retain the exact head reviewed and refresh material fixes.
- [ ] At PR preparation, run the required full gate once through Brigade:

```bash
PATH="$PWD/.venv/ci312/bin:$PATH" PY="$PWD/.venv/ci312/bin" PYTHONPATH="$PWD/src" \
  .venv/ci312/bin/brigade work verify run --target . \
  --argv-json '["./scripts/verify"]' --timeout 3600 --capture brigade-work
```

- [ ] Respect shared verification slots. Never pass `--no-reuse` or retry status 75 with a longer timeout. The full local gate does not replace Windows, content, metadata, install or smoke CI checks.
- [ ] Run content/history guard and final prose checks before authorized push. Preserve no-em-dash writing rules, generic fixtures and existing compatibility names. Link an authored/pushed PR as the next tool call, then own current-head checks, review findings and protected merge queue through merge. If waiting on checks/reviews, watch and end the turn instead of polling.
- [ ] Verify the merged state, write and lint a durable handoff, release only this lane's actual execution claim with its exact holder, and unwatch before handing the completed work back. Epic execution activation remains held even after this slice merges.

## Plan Self-Review

Spec coverage: Task 1 covers deterministic pair bounds and existing writer compatibility. Task 2 covers shape, all three identities, ordering, collisions, scoped reads and lifecycle folding. Task 3 covers nested privacy, aliases, checkpoint rewrite and terminal refusal. Task 4 covers crash/process/concurrency, unsupported readers, authoritative next writes and consumed results. Native holds and omitted execution fields appear in every relevant boundary.

Interface consistency: Task 2 owns all shared batch names, and Tasks 3/4 consume them. Task 1's optional checkpoint argument is wired only by the aggregate lifecycle path, leaving old callers unchanged. No plan step introduces a second task fold, arbitrary nested payload exception or new persistence domain.

Review Focus: each of the five conditions has a named test in its owning task. Test fixtures must distinguish unrelated identities unless explicitly testing aliases. Unknown ownership, capacity, readiness and unavailable worker routes stay explicit.

Execution requires user review of this plan. Use the already selected Codex subagent method after that review, with governed routing revalidated first. Design approval does not activate native execution.
