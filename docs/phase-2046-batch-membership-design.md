# Durable descriptor membership for epic #2046

Status: builder-reviewed proposed design awaiting user approval.

The user selected option 1: membership only, represented by one bounded journal fact. That selection authorizes this design. Implementation and native activation have separate gates.

Inspection baseline: `6bccce15768a9c0f8b589cb95578fd48940e7347`. Source references below refer to that revision. The local design branch starts at that revision. Implementation must refresh its actual base and impact before edits.

## Intent

Retain the complete ordered membership of a small batch in the existing run lifecycle journal. After restart, a reader can enumerate its exact task descriptors without receiving the original rows again. Reusing the same membership request with different rows refuses before a write.

Today `TaskSpec` retains task ID, seat and payload digest. Single-task acceptance checks a task and request pair. A loop can accept a prefix before a crash and cannot later reconstruct which outer membership request grouped those tasks. The new primitive supplies that missing relationship.

Its guarantee is an ordered descriptor-membership commitment. Full execution-request binding still requires source, native target, model options, policy and recoverable immutable input under a separately approved contract. Equal descriptor digests do not establish equality of those omitted inputs.

## Selected approach and alternatives

The selected approach stores all descriptor rows in one checkpoint-paired aggregate fact. A complete valid fact exposes every member. Damaged or incomplete history refuses through existing task-journal rules.

The dependency-bearing aggregate was considered. It would also retain edges and validate exact closure and cycles. Those requirements are deferred because reconstructing the existing descriptors does not require a dependency graph. This API takes descriptor values, with no generic execution-request dictionary or ignored extra fields. A future execution adapter must bind its entire supported request and explicitly reject unsupported dependency input.

The header/member/seal alternative would accommodate larger batches within the current per-line limit. It requires staged visibility, unfinished-batch recovery and seal validation. No current size requirement warrants that protocol. An oversized descriptor batch refuses rather than switching representations or splitting into standalone acceptances.

A digest/count record alone was rejected because it cannot enumerate members or discover and rewrite selected membership during redaction.

## Scope and authority

The slice adds internal Python acceptance and read operations, one persisted aggregate event shape, folding into existing task state, and relationship-aware redaction. No public CLI, JSON submission schema or native caller is added.

Acceptance creates queued task identities. It acquires no Worklore ownership, admission, readiness, budget reservation or launch grant. It does not prepare attempts, schedule work, submit providers, retry, synthesize results or manage threads and worktrees.

Future execution will reuse native discovery, delegation, app-owned worktrees, task controls and completion wakes. Brigade keeps governance, durable claims, budgets, verification and provenance. Observation remains distinct from admission. No duplicate task store, scheduler, daemon, dependency or recurring schedule belongs here.

#2046 owns this journal and adapter boundary. CLI and engine compatibility work stays with its existing owner. Hub rendering remains separate, and prior coordination gives no blanket backend edit authority. T3 server/API and admission-backend implementation owners remain unconfirmed. Memory APIs, promotion, ACLs, encryption and assisted triage remain with their owners.

## Immutable values and identities

The internal input contains an existing run, an outer membership request token and a nonempty ordered tuple of existing `TaskSpec` values. Version 1 fixes the stored descriptor shape to exactly `task_id`, `seat` and `payload_digest`. Existing field validators apply unchanged. The implementation may use frozen wrappers and choose function names without changing these semantics.

The proposed internal event name is `task.batch-accepted`. Its exact closed payload contains `descriptor_version`, `request_id`, `members` and `membership_digest`. Each member contains only the three descriptor fields. No stored count or child request duplicates information already derivable from the rows. Unknown fields, nested extras, unsupported versions, booleans used as integers and malformed scalar values refuse.

The builder will freeze canonical encoding and domain tags in the implementation plan. Use the existing canonical JSON bytes and SHA-256. The membership digest commits a domain tag, version and every ordered descriptor row. Order changes conflict in this internal version. The retained sequence also controls enumeration order.

A reconstructible batch reference derives from a separate domain tag, run ID, outer request and membership digest. It is a correlation identity, with no execution authority. It stays stable through ordinary member lifecycle updates and changes when redaction changes its inputs. The acceptance event digest is a history version, distinct from this reference.

Each child request derives deterministically from a third domain tag, run ID, outer request and task ID. It excludes the complete membership digest so an unrelated row edit does not rekey all siblings. Check actual derived request values for collisions with each other and every accepted task request. A naming prefix cannot reserve a namespace in the current API.

Outer requests use a separate membership namespace. Their string may equal a standalone task request without making the two objects equivalent. Derived child requests remain in the existing per-task namespace. Reuse of one outer request for another membership conflicts within that run.

Different task IDs may share a seat and payload digest. Duplicate task IDs always refuse. A task already accepted standalone or in another batch cannot be adopted, even with an identical descriptor. Exact replay of its original complete batch is the only replay case.

## Bounds and preflight

Builder choice for this internal version: accept 1 through 8 members. Preserve the existing 512-character scalar limit, 16,384-byte event-line limit and run-journal limits. This private cap is not a promised public batch size, and eight legal descriptors are not guaranteed to fit every remaining journal.

Under the existing owner transaction, construct and validate the actual proposed checkpoint and aggregate envelopes before either is appended. Preflight exact serialized lines, remaining event count and cumulative bytes for the pair. A deterministic size or capacity refusal must leave no checkpoint prefix. Check actual encoding including the newline and identity fields, rather than estimating from count alone.

Reuse existing bound enforcement and serialization. If current append interfaces cannot express pair preflight, extend the smallest existing journal helper needed. Do not raise limits, bypass checks or add a parallel writer. Files beyond the expected six require a recorded concrete invariant and scope review by the parent before editing.

## Acceptance, replay and reads

Acceptance requires the existing active run owner and strict task transaction. Validate all historical task/checkpoint pairs and semantic task history before inspecting an existing batch or writing a new one.

For new acceptance, validate the complete input, occupied task IDs, outer identity, derived child requests and pair bounds. Publish one aggregate fact through existing checkpoint-paired task machinery. Fold it into all queued `TaskResult` entries with their existing `TaskHandle` structure. Initial member versions use the aggregate event digest.

For exact replay, compare version and retained rows, verify the stored commitment, refresh the existing durability barrier and return the same batch identity with ordered current `TaskResult` values. Append nothing. A changed row, order, count or version under the same outer request refuses without mutation. A duplicate aggregate event in stored history is invalid, even if its bytes describe the same membership.

A batch read takes the run and outer membership request and reconstructs only that batch from retained history through the existing read transaction and fresh durability barrier. It returns the batch reference, immutable descriptor sequence and current member states in accepted order. Unknown membership refuses with the existing bounded task error family. Reads do not adopt a writer or repair history.

Existing `read_tasks` continues to include all accepted tasks. Batch-specific reads never accidentally return every task in a run. Existing standalone acceptance and replay retain their behavior. Replaying a batch does not reset prepared attempts, observations, acknowledgements or consumed launch claims.

Subsequent member facts update only the addressed member's version. A sibling's version remains usable until that sibling changes. Aggregate acceptance must be handled before consumers dereference a top-level `task_id`. There is no synthetic placeholder task for the batch.

## Crash and concurrency behavior

Atomic membership means recovered valid history exposes every descriptor from the aggregate. It does not promise that checkpoint and fact writes cannot tear.

| Observed history | Required outcome |
| --- | --- |
| No new pair bytes after a failed attempt | A later valid acceptance may proceed under existing owner rules. |
| Checkpoint only, partial aggregate or broken chain | Bounded refusal, preserving history for recovery. No new request or remaining-member submission. |
| Complete valid aggregate, reply lost or durability barrier failed | Reconcile through existing fresh barrier. Exact replay returns all identities without another acceptance. Unresolved durability still refuses. |
| Complete aggregate followed by later member facts | Return current member states without reverting them. |
| Earlier orphan followed by unrelated valid pairs | Refuse. The later pairs cannot hide the orphan. |

Real transaction locking serializes concurrent acceptors. Identical requests yield one fact and replayed identities. Conflicting requests yield one winner and no accepted prefix from the loser. Owner changes and process restart use existing lifecycle mechanisms. Orphan repair and automatic retry are outside this slice.

Existing launch semantics remain: prepare, exact budget reservation, consumed claim and requested recording stay separate. An already-consumed claim never becomes a replayed grant. This primitive is not wired to production native execution, and its batch reference does not satisfy the full prelaunch binding requirement by itself.

## Persisted compatibility and projection

Register the aggregate as a schema-v2 task-family fact with one exact bounded nested-payload exception. All other payload validation retains its current rules. Do not encode members inside a scalar string or admit arbitrary mappings and lists.

Task-family registration must preserve all-history pair validation, owner locking and strict incomplete-pair refusal in journal, lifecycle, checkpoint, shadow and rewrite consumers. Merely adding an event name to a registry is insufficient. Audit every single-task assumption, including launch-history lookup.

The run projector treats the aggregate as status-neutral while preserving journal cursor and authoritative-next-write behavior. Existing journals remain readable. A reader without the new event registration must refuse the aggregate as unsupported. Unknown descriptor versions also refuse. No ignored-event, downgrade or split-acceptance fallback is allowed.

## Selective archival redaction

Redaction stays restricted to terminal authoritative runs under existing ownership and replacement rules. Validate the complete original semantic history before preparing replacement bytes. Corrupt membership, task history, aliases or pair relationships refuse without replacement.

Selecting an aggregate fact selects its outer request and member fields according to the existing selected-fact scrub policy. Selecting a later member fact or targeted control discovers that member's retained aggregate row. Rewrite the affected row and any equal-value aliases, then recompute its spec, child request, attempt/result identities and membership commitment as required. Rebuild checkpoint pairing keys and the event chain through existing rewrite machinery.

Keep an unselected outer request unchanged unless it is itself an affected alias. A selected or aliased outer request may change all derived child requests. Those are related identity changes. Preserve unrelated sibling seat/payload fields and unrelated batches where they are not selected or aliased. Later envelope digests may change because the chain changes.

Enumerate nested scalar values when checking selected-value removal and reserving collision-free replacements. Preserve the descriptor version, member count and order. Retain the existing derived-alias passes, including provider/control fields equal to old or recomputed task, spec, child-request, attempt or result identifiers. Recompute the batch reference from rewritten inputs.

The rewritten history must replay coherently and retain every consumed launch claim. Old handles and references are archival identities that become invalid when their inputs change. A collision-free substitute is not a real native reconciliation coordinate. No redaction path may regrant launch permission or reconstruct deleted payload bytes.

## Offline acceptance evidence

The implementation plan must cover these behaviors with existing temporary-run fixtures and meaningful failure injection. No test is claimed passing by this design.

1. Accept repeated-seat members with distinct IDs and identical payload digests. Restart in another process with valid renewed ownership, enumerate without caller rows and replay without journal byte changes. Include unrelated tasks and another batch.
2. Refuse changed, reordered, subset and superset membership, duplicate IDs, occupied tasks, unknown versions, invalid nested shapes and actual derived child-key collisions. Verify both pair records remain unchanged on deterministic rejection.
3. Exercise count, exact line size and remaining journal-capacity limits before checkpoint publication. Cover maximum-length valid scalar inputs without assuming they fit.
4. Race identical and conflicting acceptors through the real transaction path with bounded joins. Inject each crash window above and prove no partial usable membership or hidden orphan.
5. Prepare, claim, observe and acknowledge one member using existing APIs. Preserve siblings' versions, consumed claims, run status and authoritative cursor behavior across replay and restart.
6. Redact the aggregate, a member fact, a targeted control and interleaved selections across two batches. Verify nested affected references and derived aliases disappear, unrelated fields survive, rewritten pairs replay and corrupt originals refuse without replacement.
7. Prove old journals and standalone task behavior remain valid. Test unsupported-reader/version refusal and absence of planner, scheduler, synthesis, transport and native submission calls. Tests must not claim a descriptor digest covers omitted execution fields.

Expected production center: `run_tasks.py`, `run_task_journal.py`, `run_events.py`, `run_projector.py`, `run_redaction_tasks.py` and `run_redaction.py`. Existing journal/checkpoint/lifecycle/shadow/rewrite paths need impact review and behavioral coverage. This is an estimated feature touch set, not graph evidence or a fixed six-file promise.

Expected focused tests include a new batch test module plus existing task, task-journal, launch, event, projector and task-redaction selectors. Obtain fresh exact graph impact before implementation and run the required focused gate through Brigade. A later PR needs its full local gate, current-head review and required CI. Ad hoc receipts are audit evidence, not artifact scores.

## Builder decisions and held work

The builder may settle Python names, frozen wrapper layout, shared validators, precise domain strings, bounded diagnostics, test fixture organization and minimal journal-helper placement. Record those calls in the implementation plan. The plan must preserve all invariants above and recheck current implementation routing rather than inheriting the earlier V0 exception.

The user selected descriptor-only membership. Adding dependency metadata, full execution input, public submission behavior, a larger/staged representation or native activation changes that scope and needs another explicit decision.

This design neither closes #2047's public batch execution requirements nor proves authoritative readiness. Target-side governance, exact source/target enforcement, non-reserving readiness, acceptance lookup/replay and restart delivery remain separate native/backend contract dependencies. Proposed, merged, released, exposed and qualified capabilities remain distinct evidence states. Dated maintenance research is not current runtime qualification.

The builder check returned "nothing important left". The user must approve this design before implementation planning. This reviewed draft is retained at `docs/phase-2046-batch-membership-design.md`, and the approved implementation plan will refer to it. No product source, provider, service, ownership backend or runtime enablement changes are part of writing this draft.

## Pinned source references

- `src/brigade/run_tasks.py`, especially descriptor definitions, `_fold`, `accept_task`, launch claim and result reads.
- `src/brigade/run_task_journal.py`, closed task payloads and strict transaction/paired append.
- `src/brigade/run_events.py`, canonical encoding, payload bounds and task-family schema gating.
- `src/brigade/run_journal.py`, append outcomes and line/count/cumulative-byte enforcement.
- `src/brigade/run_checkpoint.py`, `run_lifecycle.py` and `run_shadow.py`, strict task pair handling.
- `src/brigade/run_redaction_tasks.py`, `run_redaction.py` and `run_redaction_rewrite.py`, relationship rewriting and terminal publication.
- `src/brigade/run_projector.py`, explicit status-neutral events.
- `docs/phase-2046-native-t3-app-hosted-design.md`, full input/binding and activation prerequisites.

All references are available at the inspection baseline above. Supporting source scouts and architecture reports are local design evidence, not test receipts or runtime qualification.
