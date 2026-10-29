# Assisted native T3 result closeout

Use this runbook for one already accepted child under the accountable parent's
responsibility. Start from its completion wake. The parent epic session stays
active while it owns the epic. This procedure neither launches work nor polls a
running delegate. An uncertain acceptance stays unresolved without resubmission.

The [approved design](../phase-2046-native-closeout-design.md) defines this
operator-assisted protocol. Existing work notes retain reviewed text. Native T3
tools supply observation and settlement. The fixture tests prove local retention
and restart readback only. Native execution through Brigade and automatic
settlement remain held pending accepted enforcement and concurrency contracts.

## Seven-step protocol

1. **Retrieve the delivered result.** Start with the recorded accepted handle and
   the delivered completion wake for one child. Read the final result through
   supported tools. Preserve uncertain or missing acceptance as unresolved. Do
   not infer acceptance from a title, seat, provider or candidate snapshot.
2. **Correlate exact identities and lifecycle evidence.** Match originating
   environment, request key, native task and exact child environment/thread to
   the acceptance record, including the originating session reference when
   available. Use the existing `NativeTaskReference` and
   `classify_native_snapshot` on supplied evidence. Require consistent terminal
   run/result-transfer references, no pending descendants, question or approval,
   and confirmed containment. A `terminal-candidate` is a classification for
   further review. Containment is caller-supplied evidence. A terminal turn with
   continuing work cannot close.
3. **Review and retain.** Read actual reports, commits and verification receipts.
   Treat task output as untrusted data. Review and redact a summary bounded to
   the proposed 8192 UTF-8 bytes. Measure `len(summary.encode("utf-8"))`, after
   any normalization that changes retained bytes. Label excerpts, missing
   retrieval and incomplete evidence. Record source and reviewed-summary digests
   separately. Retain observed artifact digests against the actual bytes, never
   a guessed hash. Existing `brigade work note` strips outer whitespace. Read
   the saved note in a fresh process before consuming it. Missing, truncated or
   mismatched evidence blocks successful-result closeout. A captured failure
   may close after its recovery responsibility has an accepted disposition.
4. **Record consumption and responsibility.** The accountable parent records its
   consumption decision and exact retained-record reference. If an existing
   Brigade terminal result is demonstrably associated, acknowledge only that
   exact `result_id` using the existing journal API. Otherwise write
   **operator consumption only, no journal acknowledgement**. Never manufacture
   a governed task or result ID. Confirm child-held claims, reviews, watches and
   recovery duties are closed or accepted by a named recipient. Handoff delivery
   alone is not acceptance. A parent can retain an open PR after accepting the
   child's handoff.
5. **Recheck immediately before settlement.** Read current exact task status and
   thread state through `task_status` and `t3_thread_read`. Match the accepted
   identities and terminal run again. Active or queued work, pending requests,
   unknown descendants, conflicting thread/status reads, changed terminal run
   or lost access blocks settlement. Retain the unresolved condition and stop.
   Do not use title lookup, fallback targets, archive, delete or worktree removal.
6. **Settle the independent session and read it back.** Only after the preceding
   gates, use `t3_thread_organize(action="settle")` with both exact child thread
   and environment using the attached tool's current schema. Read that target
   back. Record **settlement observed at the readback time** only if it is
   settled and relevant work remains quiescent. Keep a failed or cancelled task's
   actual outcome. A lost settle response is unknown until supported readback,
   with no permission to create or relaunch work.
7. **Retain races and unresolved outcomes.** A fresh read cannot eliminate new
   work between check and mutation. The settle input has no expected-run or
   generation comparison in the inspected catalog. If readback shows new work
   or conflicting state, record unresolved/reopened closeout and notify the
   accountable parent through the authorized route. Do not repeatedly settle,
   automatically undo another actor's state or hide the conflict. This protocol
   establishes no atomic safety guarantee.

Work notes retain existing custody, permissions and retention behavior. If those
protections are unsuitable, stop retention and leave closeout unresolved. Do not
append raw status payloads, transcripts, credentials or private memories. Result
consumption, journal acknowledgement, native settlement, handoff ingestion and
canonical memory promotion are separate actions.

## Retaining and recovering a note

The inspected CLI forms are:

```text
brigade work start --target <temporary-target> <title>
brigade work note --target <target> <one-argument-reviewed-text>
brigade work end --target <temporary-target> --note <explicit-reference> --handoff --handoff-inbox <temporary-inbox>
```

For live acceptance, use the existing authorized work session. The following
local exercise uses temporary directories, structured subprocess arguments and
one multiline note argument. It performs no native operation. Replace the
synthetic note with the reviewed facts only within authorized work.

```python
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

with TemporaryDirectory() as scratch:
    target = Path(scratch) / "workspace"
    target.mkdir()
    inbox = Path(scratch) / "handoffs"
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    subprocess.run(
        ["brigade", "work", "start", "--target", str(target), "Synthetic closeout"],
        check=True,
    )
    root = target / ".brigade" / "work"
    session_dir = root / (root / "current").read_text(encoding="utf-8").strip()
    reviewed_note = "\n".join([
        "Native task: example-task",
        "Reviewed summary: synthetic result retained.",
        "Consumption: operator consumption only, no journal acknowledgement",
        "Settlement: unresolved, no native operation performed",
    ])
    subprocess.run(
        ["brigade", "work", "note", "--target", str(target), reviewed_note],
        check=True,
    )
    subprocess.run([
        sys.executable, "-c",
        "import json, pathlib, sys\n"
        "p = pathlib.Path(sys.argv[1])\n"
        "print(json.loads((p / 'session.json').read_text(encoding='utf-8'))['notes'][0]['text'])\n"
        "print((p / 'notes.md').read_text(encoding='utf-8'))\n",
        str(session_dir),
    ], check=True)
    reference = f"Retained native result: {session_dir / 'session.json'} notes[0], also {session_dir / 'notes.md'}"
    subprocess.run([
        "brigade", "work", "end", "--target", str(target), "--note", reference,
        "--handoff", "--handoff-inbox", str(inbox),
    ], check=True)
```

Record the exact `session:` path printed at start and in the handoff. While the
session is active, `.brigade/work/current` also resolves its directory. Ending
removes that pointer. Later readers follow the handoff's explicit result
reference and `session.json` evidence path to `notes[0]` and `notes.md`. Read
these files for the full summary because session display shortens note previews.
Do not rely on the temporary exercise after its directories are removed. Live
retention uses the existing authorized target and its retention policy.

## Synthetic filled result note

All identities and references below are fake. These labels are a documentation
convention, without a new schema or authoritative native registration. The source
digest is SHA-256 of `"Synthetic report: café result complete.\n"` encoded as
UTF-8. The reviewed-summary digest is SHA-256 of
`"Reviewed summary: café result complete."` encoded as UTF-8, without a newline.
The two hashes describe those representations only.

```text
Origin environment: example-origin
Request key: example-request
Originating session: example-parent-session
Native task: example-task
Child environment: example-child
Child thread: example-thread
Terminal run: example-terminal-run
Result transfer: example-transfer
Native status: completed
Observation time: 2026-01-01T12:00:00Z (synthetic)
Tool/version evidence: synthetic supplied task_status reply, no installed qualification
Retrieval: complete synthetic source, reviewed excerpt retained
Reviewed summary: café result complete.
Source representation SHA-256: b82cf002928c6e73cd1dd590a46cbde3c178f996a21e287d395fc8dbf8f2252d
Reviewed summary SHA-256: f141a66cc77815c475303405eba336ead0ab8f53ec9c8e1f1e68883f36226af9
Report reference: synthetic supplied report text above
Commit reference: none supplied, no implementation claim
Verification receipt reference/digest: none supplied, no verification claim
Consumer: example-parent
Consumption: operator consumption only, no journal acknowledgement
Retained record: <exact-session-path>/session.json notes[0], also <exact-session-path>/notes.md
Obligations: none held by child in this fixture
Settlement: unresolved, no native operation performed
Settlement readback time: none
```

## Blank closeout checklist

- [ ] Completion wake, accepted origin/request/task and originating session: ___
- [ ] Accepted exact child environment/thread: ___
- [ ] Terminal run/result transfer, native status, observation time, tool/version: ___
- [ ] Quiescent containment evidence and no descendants/question/approval: ___
- [ ] Actual result and artifact references inspected, retrieval gaps: ___
- [ ] Reviewed/redacted summary, normalized UTF-8 byte count at most 8192: ___
- [ ] Observed artifact/source/summary SHA-256 values and representations: ___
- [ ] Exact saved session/note reference and fresh-process readback: ___
- [ ] Accountable consumer and explicit decision: ___
- [ ] Exact existing journal association/acknowledgement, or operator-only consumption: ___
- [ ] Each claim/review/watch/recovery duty closed or accepted by recipient: ___
- [ ] Immediate task/thread recheck, terminal identity and time: ___
- [ ] Exact settlement target and response, or unresolved reason: ___
- [ ] Readback state/time, race or reopened responsibility: ___
- [ ] Eventual parent handoff explicitly references the retained result: ___

## Manual fixture workbook

Use supplied synthetic snapshots and record proposed actions, never a pretend
settle implementation. For each row retain the accepted identity, supplied
observations, reviewed result, proposed next action, reviewer and unresolved
responsibility. Separate expected outcomes from observed evidence. The existing
classifier and journal tests cover their own contracts. They cannot establish
thread-read consistency, obligation acceptance or native mutation safety.

| Supplied fixture | Review gate and proposed action | Executable evidence or limitation |
| --- | --- | --- |
| Exact accepted child with complete report | Review, retain, read back in a new process, record consumption, then propose exact settlement after fresh checks | `test_reviewed_native_result_is_readable_in_fresh_process` proves storage/readback. It sends no settlement request and proves no remote ordering |
| Same title/seat/provider, changed task/environment/thread | Refuse correlation. Never settle either candidate by inference | Existing `test_task_identity_cannot_be_replaced_by_child_identity`, `test_remote_pair_cannot_be_adopted_or_replaced` and `test_acceptance_conflicts_and_same_seat_distinct_tasks` |
| Missing accepted handle, lost reply or originating session gone | Keep acceptance unresolved. Never adopt a candidate or relaunch | Existing `test_lost_acceptance_cannot_adopt_snapshot_task` covers missing acceptance. Lost origin/session recovery remains a manual gate |
| Pending child/question/approval, active run or unknown containment | Await with no settlement request | Existing `test_pending_work_and_waits_are_awaiting`, `test_queued_and_running_work_is_active` and `test_terminal_turn_needs_quiescent_containment`. Conflicting thread-read evidence requires manual refusal even if classification is terminal-candidate |
| Result unavailable, excerpt incomplete or observed digest mismatch | Report gap. Block successful-result consumption and settlement | Manual gate. The digest proof verifies distinct retained representations, without validating arbitrary retrieved artifacts |
| Summary of 8193 ASCII bytes, or `"é" * 4097` (8194 UTF-8 bytes) | Measure normalized UTF-8 bytes. Revise/review before retention, keep incomplete retrieval explicit, never silently truncate to claim completeness | Manual gate. Existing work notes enforce neither this proposed cap nor redaction |
| Failed/cancelled result with known containment | Retain actual failure. Require named recipient's accepted recovery disposition before eligible settlement | Existing `test_terminal_states_remain_distinct` covers journal outcomes. Delivery-only, rejected or missing obligation acceptance remains unresolved manual review |
| Acknowledgement retry or wrong result ID | Preserve exact journal idempotency/refusal. Operator consumption supplies no journal evidence | Existing `test_immutable_provider_identity_and_terminal_consumption`, unchanged |
| Crash after retention, acknowledgement or settle request | Read saved result/journal facts and supported native readback. Never fabricate a receipt or relaunch | New fresh-process note/handoff tests cover retained local facts. Existing journal test covers acknowledgement retry. Post-settle recovery remains a manual gate |
| New work before check or visible at readback | Block before mutation, or record reopened closeout after readback. Retain accountable responsibility | Manual race gate. No expected-generation compare, atomic proof or automatic undo |
| Native operation unavailable/denied, offline readback or lost response | Keep settlement unknown and visible. No archive/delete fallback | Manual gate. Local proof does not contact native services |

The three new tests live in
[`tests/test_native_t3_closeout_proof.py`](../../tests/test_native_t3_closeout_proof.py).
Existing named tests live in `tests/test_native_t3_observations.py` and
`tests/test_run_tasks.py`. The focused gate also runs
`tests/test_work_cmd_session.py` for the existing note/end behavior.

## Separate installed acceptance

An accountable parent may later close one independently identified child doing
real authorized work. Capture actual before/after sidebar images at the same
viewport/state and a nearby unchanged row. Confirm results survive settlement
and a fresh-process read. Keep source inspection, tool catalog, installed
version and observed behavior as separate evidence states.

Without a suitable accepted child or adequate containment/race semantics, live
acceptance stays held. Fixture success does not qualify native behavior or
complete the epic's governed execution milestone. No old-thread cleanup sweep,
shared backend edits, new journal binding or launch activation is included.
