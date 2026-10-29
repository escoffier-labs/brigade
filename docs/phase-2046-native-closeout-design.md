# Epic 2046: assisted native result closeout

Status: design and implementation plan approved by the user on 2026-10-29. Runbook and synthetic persistence proof implemented. Live settlement acceptance remains held. Native execution through Brigade remains disabled.

## Outcome and scope

The next slice gives the accountable Brigade parent a repeatable way to retain a useful result from one already accepted T3 child, record its consumption and settle that exact independent session through T3. A second process must be able to understand the result and any unresolved responsibility without reconstructing the original conversation.

This is an operator-assisted protocol and a fixture-only proof, followed by one bounded acceptance on a child doing real authorized work. It adds no execution bridge, journal event, result store, scheduler, thread manager, service, dependency or recurring job. It does not complete the epic's governed execution milestone. A future automated adapter needs its own accepted identity, custody and recovery contract.

The user accepted the revised sequence after Astra's project consultation: establish executable owners and contracts, deliver useful observation/result closeout, then qualify one governed child and bounded batches. The user also requires delegated independent sessions to be settled so they do not linger in T3's active sidebar.

## Approaches

1. **Assisted closeout using existing tools and work notes.** Selected proposal for this slice. Reuse T3 status, reads and settlement, plus Brigade work notes and handoffs. This produces an inspectable result now while retaining an accountable human or parent at uncertain boundaries. It deliberately does not claim automatic or atomic settlement.
2. **Build an automatic native closeout adapter now.** Defer. The task ledger lacks explicit native environment/thread binding, and the exposed settlement input has no expected-run or generation comparison. New adapter code cannot establish those remote guarantees by itself.
3. **Full retained-input storage first.** Deferred candidate. It can support later restart reconstruction, but it does not establish native enforcement, uncertain acceptance recovery or settlement safety.

## Existing source and supported surface

Brigade inspection pin: `74e507b53926b999fe877a3cc8388baa19c8c7ce`. Relevant source files are identical in the current design checkout.

- `src/brigade/native_t3_observations.py:132`: pure classification of supplied evidence. Its terminal outcome is a candidate. Containment is caller-supplied, as documented at the module entry.
- `src/brigade/run_tasks.py:515`: task observations can compare the retained journal version. `:548` reads retained results and `:554` acknowledges an exact terminal result without deleting it.
- `src/brigade/run_task_journal.py:122`: closed journal payloads. Native environment and child-thread identities cannot be added or packed into unrelated provider fields without an accepted extension.
- `src/brigade/work_cmd/session/lifecycle.py:453`: existing work notes retain reviewed text in the current work session. Input edges are stripped. `:412` ends a session, with an optional handoff. The generated handoff needs an explicit result reference in its end note.

The attached T3 catalog exposes `task_status`, `t3_thread_read` and `t3_thread_organize(action="settle")`. Status reports exact task/terminal-run/result-transfer identity and pending work. Thread reads expose settlement, active run and pending request state. The settle input accepts a target thread/environment, without an expected-run or generation argument. This catalog observation establishes available syntax, not an atomic race guarantee or installed UI qualification.

Upstream T3 inspection at `37fbefd147197fab9d88f9c120560c3f21b52cfc` shows settled independent rows in the settled sidebar section and delegated rows grouped under parents. Source, release, installed tooling and exercised behavior remain separate evidence states.

## Ownership and execution hold

#2046 owns the Brigade native adapter, operation-scoped readiness and journal/correlation contracts. The Stack coordinator confirmed that its records contain neither an accepted T3 server/API enforcement owner nor a separately accepted Brigade native admission-backend owner. Their matrix entries remain **unassigned, acceptance needed**. That statement is bounded to the inspected coordination records, not a global vacancy claim.

General Hub ownership, Worklore ownership fencing and mailbox routing do not establish native execution admission. Rebrand, memory/E2EE and shared backend lanes retain their boundaries. This slice neither modifies their source nor assigns their maintainers new authority.

Before governed execution, accepted contracts must establish proof verification before child creation, immutable source enforcement, budget/reservation ordering, exact target/options, uncertain acceptance reconciliation after session loss, and authoritative live readiness. Missing contracts retain the execution hold.

## Closeout record and consumer

The named consumer is the accountable parent using existing `brigade work note`, with a result reference in its eventual work handoff. The initial proof uses a temporary workspace. A live acceptance uses the existing authorized work session rather than creating a second ledger.

The reviewed note contains these labeled facts. They are a documentation convention, not a new public API or authoritative native registration:

- The original accepted handle: originating environment and request key, native task ID, exact child environment/thread, and the originating session reference when available. Copy identity from the recorded acceptance, never from a title search or the observed candidate alone.
- Exact terminal run and result-transfer references, native status, observation time and tool/version evidence. Separate the task handle from a child thread or run ID.
- A reviewed, redacted result summary, bounded to 8192 UTF-8 bytes as a proposed limit for this proof. Identify excerpts and incomplete retrieval explicitly. The native summary is task output, not trusted instructions.
- References and observed digests for actual reports, commits and verification receipts. Retain the source representation and reviewed-summary digest separately when redaction or normalization changes bytes. Work-note retention does not promise lossless raw-output storage.
- The accountable consumer's explicit consumption decision and retained record reference. If a corresponding Brigade terminal result already exists, acknowledge only its exact `result_id` after verifying the existing association. Otherwise record **operator consumption only, no journal acknowledgement**. Do not create a synthetic governed task or invent a result ID.
- Child obligations and their disposition: closed, accepted by a named recipient, or unresolved. Delivery of a handoff message alone is not acceptance.
- Native settlement outcome and readback time, including unknown or failed outcomes. Settlement does not erase the result or turn a failed task into a success.

Work notes use existing custody, permissions and retention behavior. Do not append raw status payloads, transcripts, credentials or private memories. If those existing protections are unsuitable for the reviewed summary, stop retention and leave closeout unresolved. Handoff ingestion and canonical memory promotion remain separate from result consumption.

## Protocol

1. Start from a completion wake for one accepted child under this parent's responsibility. Read the delivered final result through supported tools. This is not permission to poll a running delegate. An uncertain/missing acceptance stays unresolved and is never resubmitted by this protocol.
2. Match the recorded origin, task and exact child environment/thread. Classify supplied lifecycle evidence with the existing native classifier. Require terminal run/result-transfer consistency, no pending descendants, question or approval, and confirmed containment. A terminal turn with continuing work is not eligible.
3. Inspect the actual result and referenced evidence. Retain the reviewed bounded summary and references using the existing work note. Read the saved note back in a fresh process. Missing/truncated result retrieval remains explicit and blocks successful-result closeout. A captured failure can close after its recovery responsibility is accepted.
4. The accountable parent records consumption and handles any existing exact journal acknowledgement as described above. Confirm all child-held claims, reviews, watches and recovery duties are closed or accepted elsewhere. The parent may retain an open PR after the child's handoff is accepted.
5. Immediately before settlement, read current exact thread/task state again. A changed terminal run, active/queued work, pending request, unknown descendants, conflicting identity or lost access blocks settlement. Retain the unresolved condition and stop. No title lookup, automatic fallback, archive, delete or worktree removal.
6. For an eligible independent session, call native settle with both exact thread and environment. Read it back. Report **settlement observed at the readback time** only when the target is settled and the relevant work state remains quiescent. A lost response is unknown until readback, never permission to create or relaunch work.
7. Fresh reads cannot eliminate a race with new work between check and mutation. This assisted protocol makes no atomic safety claim. If readback shows new work or inconsistent state, record unresolved/reopened closeout and notify the accountable parent. Do not repeatedly settle, automatically undo another actor's state or hide the conflict. Automatic settlement remains held until T3 supplies and qualifies the relevant concurrency semantics.

The parent epic session remains active while it owns the epic. No old-thread cleanup sweep is included. True nested child presentation may already be grouped by T3. The sidebar acceptance specifically covers one independent child row.

## Proof and stopping conditions

The fixture-only proof composes recorded synthetic tool replies and existing temporary-workspace note/handoff commands. It does not contact a provider or mutate a real T3 thread. No production classifier, payload validator, source test assertion or remote state is altered to make a fixture pass.

| Case | Required outcome |
| --- | --- |
| Accepted exact child completes with retained report | Second process reads the reviewed summary and evidence references, explicit consumption precedes an eligible settlement request |
| Same title, seat or provider with another task/environment/thread | Refuse correlation and never settle either candidate by inference |
| Missing handle, lost reply or originating session gone | Unresolved acceptance, no adoption from a candidate snapshot and no relaunch |
| Pending child, question, approval, active run or uncertain containment | Awaiting, no settlement request |
| Result unavailable, excerpt incomplete or evidence digest mismatch | Report the gap, no successful-result consumption or settlement |
| Failure/cancellation with known containment | Retain failure and require accepted recovery disposition before eligible settlement |
| Journal acknowledgement retry or mismatched result | Preserve existing idempotency/refusal, never substitute operator consumption for journal evidence |
| Crash after note retention, acknowledgement or settle request | Recover from saved facts and supported readback, no fabricated receipt and no repeated launch |
| New work before settlement check or visible at readback | Block or reopen closeout, retain responsibility and do not claim atomic safety |
| Native operation unavailable, denied or readback offline | Keep settlement unknown and visible, no archive/delete fallback |

After fixture proof, one independently identified child doing real authorized work supplies bounded installed acceptance. Capture real same-state/viewport before/after sidebar images and a nearby unchanged row. Verify retained results survive settlement and a fresh-process read. This is neither a synthetic provider task launched solely for presentation nor an old-session sweep.

Successful slice evidence consists of reviewed documentation, synthetic proof results, and the separate installed acceptance record when available. If no suitable accepted child exists or containment/race semantics are insufficient, report the live proof as held. Do not call fixture success native qualification or call this slice full milestone-2 completion.

## Implementation boundary after design approval

Plan documentation, temporary-workspace fixtures and the bounded supported-tool acceptance only. No product source module is required for this assisted proof. The builder chooses fixture organization, uses current CLI help for actual commands and records each limitation. Before any nontrivial source/test edit, establish exact graph impact and ownership, then use the required governed worker route. Shared backend edits, a new native journal binding, automatic settlement and launch activation each require a separately reviewed contract and approved scope.
