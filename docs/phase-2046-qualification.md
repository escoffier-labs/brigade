# Matched T3 qualification for #2046

Run five pairs comparing governed native T3 with explicit `brigade run batch`.
Pair 1 runs T3 first. Each subsequent pair reverses the previous order.
The runner waits for terminal verification and closeout before offering the
next batch. There is no speedup target.

## Matched settings

| Setting | Value |
| --- | --- |
| T3 | The parent calls its own `delegate_task` using Brigade's exact claim arguments. Brigade governs enrollment, claims, observation, acknowledgement, and finalization. |
| Run batch | The runner invokes `brigade run batch` directly with the same file and seat. |
| Workers | Codex `gpt-6.2-sol`, low reasoning, standard service (`default`) |
| T3 parent | Codex `gpt-6.2-sol`, medium reasoning, standard service |
| Workload | Three independent read-only tasks. Each reports its Git HEAD and the SHA-256 of one tracked file. Git objects at the pinned revision provide the expected answers. |
| Input | One `brigade.run_batch.v1` file per pair, with the same request ID, task text, worktree isolation, and no synthesis. Both routes derive the pinned `base_sha` from the same clean checkout. |
| Concurrency | Roster `max_workers = 3` and enough capacity for all three tasks on either route |

T3 receives the read-only instruction in its task prompt. The direct route
also enforces read-only through Brigade's worker policy. The harness checks
that all observed worktrees are distinct, clean, and at the pinned revision.
Approval waits, if any, remain in the measured time.

## Run the campaign

Use a dedicated clean checkout attached to the designated local T3 parent
project. Keep other workers and benchmark runners off that checkout. Use an
admitted, direct Codex worker seat with no fallback, a read-only-compatible
sandbox, and a [native T3 target](t3-governed-delegation.md#seat-bindings) for
the same seat. Set the target's `reasoningEffort` to `low` and `serviceTier`
to `default`. If the target sets `max_live`, it must allow three workers.
The collector checks the parent model, effort, and tier in the saved T3
capabilities reply. Set the direct worker's effective Codex
configuration to standard service and keep its local session logs available
to the verifier. Use the workload checkout's effective roster for both
routes because T3 claims resolve that roster. Use distinct workload and
evidence directories.

Run the module with the same Python environment that runs Brigade. Pass the
commit of that installed Brigade source and the direct worker's Codex sessions
directory.

```sh
python -m brigade.t3_qualification run \
  --workspace WORKLOAD_CHECKOUT --roster ROSTER_TOML --seat ADMITTED_SEAT \
  --brigade-commit BRIGADE_COMMIT --codex-sessions CODEX_SESSIONS_DIR \
  --output NEW_CAMPAIGN_DIRECTORY
```

For each T3 arm, give the printed `coordinator.txt` to the designated parent.
The runner stages the shared batch, expected run and task identities, and
independent expected answers. The parent checks readiness, opens the run,
claims and delegates the three tasks, and records observations from native
completion wakes. A wake can arrive during the launch turn. The parent marks
and processes it after saving that task's delegate reply, then resumes launch.
It verifies and acknowledges the exact results before
finalizing. The parent's finish command verifies the retained outputs and
publishes `measurements.json` atomically. The runner checks the readiness
receipt and capabilities report and checks the measurements against the
durable evidence before advancing. Each wake must follow its own task's reply
mark. A wake can precede the batch-wide launch-complete mark. It waits up to one hour by default for each parent
submission. `--parent-timeout` sets that limit in seconds.

A refusal, inconclusive observation, evidence mismatch, or timeout stops the
campaign. Keep the failure marker, raw evidence, and any completed batch
records. Settle any outstanding native work through the documented lifecycle
before starting another campaign. A fresh campaign uses a new evidence directory
and new request identities. Keep the raw evidence private. It contains paths,
tool replies, and native identities.

## Measurements and limits

- **Verified completion** starts immediately before `t3 open` or `brigade run batch`.
  It ends at the harness's monotonic mark after verifying all three results.
- **Queue and startup** run from the opening mark to the first child turn
  start. The available evidence cannot separate these phases.
- **Execution** spans the earliest child turn start to the latest child turn
  end. It is a wall-clock window across workers.
- **Delivery** runs from the last child turn end to verified completion. It
  includes any notification handling, acknowledgement, finalization and
  verification that fall inside that window.
- **Planning and synthesis** are zero by design. **Usage** is recorded as
  `not recorded` for both arms.
- **Errors** are reported per arm. A failed arm has its diagnostic in
  `failure.json` and any retained status or stderr. The direct route counts provider error events. Additional observed errors
  can be counted from the arm's `errors.jsonl`. **Parent interventions** count
  journaled cancel requests and hold facts plus distinct manual actions
  recorded in `interventions.jsonl`. Record manual actions outside the
  journaled facts to avoid counting an action twice.

T3's `task_status` identifies the exact terminal child run. The matching T3
activity run supplies `startedAt` and `completedAt`. When the terminal run
differs from the initial delegation, the measurements retain both identities.
The parent preserves the raw replies. The harness records those timestamps
alongside wall and monotonic clock marks in `measurements.json`. Brigade's governed journal
does not record native turn starts or ends. The direct route reads start and
end timestamps and model, effort, and tier from the local Codex session for
each isolated worktree and keeps a private copy of that session in the arm
directory for replay. Before dispatch it reads the effective tier through
Codex's prompt-free `config/read` request. Missing or ambiguous timing
evidence blocks the report. The harness allows up to 50 ms of wall-clock drift
and maps phase boundaries between the opening and verification monotonic marks.

`report.json` contains each batch record and per-arm medians, error totals,
and intervention totals. Rebuild it with
`python -m brigade.t3_qualification report CAMPAIGN_DIRECTORY`.
The report rebuilds each row from the retained evidence, including the
workspace's native capacity at batch close, and compares it against the saved
row. Keep the campaign directory and Brigade journals available for replay.
The worker worktrees must still be clean at the pinned revision.
The collector requires all five verified pairs with alternating order, a
matched envelope and no overlapping batch intervals before computing medians.
