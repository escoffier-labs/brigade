# Governed Orca qualification for epic #2800

Workstream 6 (#2884) reached the first governed claim after the Orca desktop RPC connection was restored. Brigade refused the claim. One batch opened, zero workers launched, and zero of the five planned pairs completed. The follow-up seat search found no eligible seat in live Hub policy revision 13. No retry batch opened, and there is no live timing or usage comparison.

## Attempt history

- The first attempt stopped at preflight. The run-batch route could not admit the unregistered `worker` seat and the Orca coordinator probe returned `runtime_unavailable`.
- The second attempt stopped at preflight. The coordinator probe again returned `runtime_unavailable` after #2892 and #2911 had merged.
- In the third attempt, Orca status reported a connected runtime and zero supervised workers. The qualification staged three native tasks in a dedicated coordinator terminal. Both route readiness checks returned `ready`, and the Pair 1A Orca check returned `ready` again immediately before the roster snapshot and batch open.

## Configuration and tier evidence

| Setting | Value |
| --- | --- |
| Orca CLI | `1.4.271` |
| Daemon artifact | Desktop-bundled backend. Its live lock reported version `unknown`. The bundled `daemon-entry.js` SHA-256 was `addb2480f2e790ff5ca86a58cc44c734aac3fe78e9bd6185772f6d5e2ac4a358` |
| Brigade executable source | `126cb1270e868613a4caa8814e44f3f0777af9d5` |
| Workload revision | `0c29e690312b88ec99adb266281e476cc3fa5516` |
| Worker seat, model and effort | `sol62`, Codex `gpt-6.2-sol`, medium. The model registry listed the seat as enabled at revision 297 |
| Planned concurrency and isolation | Three workers in separate worktrees per batch |
| Planned read-only enforcement | Worker prompt in Arm A. Hard read-only batch policy in Arm B |

| Arm | Configuration evidence | Preflight app-server config read | Effective worker tier |
| --- | --- | --- | --- |
| A, governed Orca | `service_tier = "default"`, `fast_default_opt_out = true` | `{"service_tier":"default"}` | Unobserved |
| B, Brigade run-batch | `service_tier = "default"` | `{"service_tier":"default"}` | Unobserved |

The app-server checks read configuration without submitting a prompt. No worker sessions existed for tier confirmation.

## Pair 1A claim and slot check

`brigade orca open` enrolled all three tasks. The first `brigade orca claim` exited 2 with:

```text
error [admission-denied]: seat admission refused this claim (policy-denied)
```

A read-only evaluation of the resolved live Hub policy returned `admissible: false` with reason `unknown-seat` for `sol62`. The enabled entry observed earlier came from the model registry. The claim gate consulted the live policy document. Qualification stopped at that gate.

`brigade orca finalize` closed the run as `failed`. The subsequent `brigade orca status` recorded workspace-wide `capacity.live = 0` and zero held tasks. The collector confirmed the slot check and recorded three nonterminal tasks and one other error. Its configuration check found no mismatch in the enrolled Arm A settings. Pair matching remains unassessed because Arm B did not run. [Redacted collector record](research/evidence/orca-qualification/pair-01-a/record.json)

## Read-only seat check after the claim denial

`brigade fleet policy resolve` returned version 13 for `brigade-run` and the qualification repository. The claim gate's admission evaluator checked that live document. `brigade fleet models list` and the local roster identified `coder` and `sol62` as the only seats with the requested Codex binding, model, and effort. Both were absent from the policy seat set.

Explicit `brigade fleet policy route` checks for those two seats returned `override-reason-required` with no selection or reservation. The admission results below come from the claim gate's policy evaluator. The route checks supplied no admission grant.

| Seat | Admission in policy 13 | Model | Reasoning | Codex service tier |
| --- | --- | --- | --- | --- |
| `coder` | Denied: `unknown-seat` | `gpt-6.2-sol` | Medium | No binding override, config `default` |
| `sol62` | Denied: `unknown-seat` | `gpt-6.2-sol` | Medium | No binding override, config `default` |
| `agy_flash` | Admitted | `gemini-3.8-flash-high` | None | N/A |
| `chef` | Admitted | `claude-opus-5-5` | None | N/A |
| `claude_standby` | Admitted | `claude-opus-5-5` | None | N/A |
| `cursor_fable51` | Denied: `seat-disabled` | `claude-fable-5-1` | Unlisted | N/A |
| `cursor_scout` | Denied: `seat-disabled` | `gemini-3.7-flash-high` | Unlisted | N/A |
| `daybreak` | Admitted | `gpt-daybreak-blue-latest` | High | No binding override, config `default` |
| `grok_native` | Denied: `seat-disabled` | `grok-4.6` | High in model registry | N/A |
| `luna6` | Admitted | `gpt-6-luna` | Medium | No binding override, config `default` |
| `muse13` | Denied: `training-not-permitted` | `opencode-muse-spark-1.3-contributor-free` | None | N/A |
| `sol61` | Admitted | `gpt-6.1-sol` | High | No binding override, config `default` |
| `sonnet55` | Admitted | `sonnet` | None | N/A |

Models for the two candidates come from model registry revision 297. Other models are the live policy declarations, with reasoning from the corresponding registry entries where available. The registry labels `agy_flash` with the policy's model, while the local roster declares `gemini-4-argon-high`. Neither is a match. The registry lists `grok-4.7` for the disabled `grok_native` seat.

Both candidate Codex bindings had no tier override. The run-batch Codex config and the Orca runtime-home Codex config each declared `service_tier = "default"`. The previous prompt-free config reads confirmed `default`. No worker sessions exist for effective-tier verification. No seat qualified, so the five-pair retry ended at seat selection and the table below preserves the last observed batch verdicts.

## Batch verdicts in planned order

A uses governed Orca. B uses explicit Brigade batch execution. A dash marks an unobserved field. Times are in seconds where observed. Usage covers input, output, and cached-input tokens.

| Batch | Arm | Verdict | Verified completion time | Errors | Parent interventions | Usage | Queue | Startup | Planning | Execution | Synthesis | Delivery | Matched or mismatched | Effective service tier |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1A | A | Claim denied | - | 1 claim denial, 3 tasks nonterminal | 0 | - | - | - | - | - | - | - | Unassessed | Unobserved |
| 1B | B | Unstarted | - | - | - | - | - | - | - | - | - | - | Unassessed | Unobserved |
| 2B | B | Unstarted | - | - | - | - | - | - | - | - | - | - | Unassessed | Unobserved |
| 2A | A | Unstarted | - | - | - | - | - | - | - | - | - | - | Unassessed | Unobserved |
| 3A | A | Unstarted | - | - | - | - | - | - | - | - | - | - | Unassessed | Unobserved |
| 3B | B | Unstarted | - | - | - | - | - | - | - | - | - | - | Unassessed | Unobserved |
| 4B | B | Unstarted | - | - | - | - | - | - | - | - | - | - | Unassessed | Unobserved |
| 4A | A | Unstarted | - | - | - | - | - | - | - | - | - | - | Unassessed | Unobserved |
| 5A | A | Unstarted | - | - | - | - | - | - | - | - | - | - | Unassessed | Unobserved |
| 5B | B | Unstarted | - | - | - | - | - | - | - | - | - | - | Unassessed | Unobserved |

## Attempt 4

Hub policy revision 14 admitted `sol62` and both route readiness checks passed.
The attempt used Orca CLI `1.4.271`, Brigade executable commit
`9606d45b536c217e1f9ee2c77449efc4ad441158`, and workload revision
`0c29e690312b88ec99adb266281e476cc3fa5516`. Both routes were configured for
Codex `gpt-6.2-sol` at medium effort and concurrency three in separate
worktrees. Prompt-free app-server reads reported `service_tier = "default"`
for both routes. The first worker exited before initialization, so effective
worker tier remains unobserved.

Pair 1A opened and its first claim succeeded. Orca's launch failed during
agent readiness because Codex received
`--dangerously-bypass-approvals-and-sandbox` twice. The created worktree was
clean at the pinned workload revision. The original request lookup showed
that the mutation completed. Orca released the retained terminal, and a fresh
show reported the resource released and worker liveness exited. Brigade's
`accepted` and `hold-resolve` both returned `start-unknown`. The original
receipt had state `failed` with residual resources, and the show-assisted
recovery requires a start receipt with state `outcome_unknown`.

Brigade refused finalization with `finalize-live`. Its final status still
reported `capacity.live = 1`, with all three tasks nonterminal. Qualification
stopped at 1A. Max observed swap usage was 5.46 GiB, below the 8 GiB stop
threshold. There were zero verified outputs, zero completed pairs, and no
comparable phase timings or token usage.

| Batch | Arm | Verdict |
| --- | --- | --- |
| 1A | A | Start unconfirmed in Brigade, one slot still occupied |
| 1B | B | Unstarted |
| 2B | B | Unstarted |
| 2A | A | Unstarted |
| 3A | A | Unstarted |
| 3B | B | Unstarted |
| 4B | B | Unstarted |
| 4A | A | Unstarted |
| 5A | A | Unstarted |
| 5B | B | Unstarted |

The collector recorded one recovery intervention and four command errors
from worker start, acceptance, hold resolution, and finalization. It found
no mismatch in the enrolled Arm A configuration. Pair matching remains
unassessed. [Attempt 4 collector record](research/evidence/orca-qualification/attempt-4-pair-01-a/record.json)

## Attempt 5

### Recovery of the attempt 4 claim

The first recovery used Brigade commit
`85a17d658c0cb48d3daef694273709ad62c57b30`. A fresh Orca snapshot showed
the failed worker's terminal released and its liveness exited.
`hold-resolve` returned `start-unknown` with the message
`the failed Orca worker has no confirmed release and exit`. That build
compared the residual-resource entries to a terminal handle string. The
original Orca receipt reported a terminal object and a worktree object.
Workspace-wide capacity stayed at one and the qualification batch sequence
remained unstarted.

The restart used Brigade commit `9190c006061bc185879e527e521417f67929668a`,
which includes the structured residual-resource fix from #2945. Brigade
accepted the original failed-start receipt and a fresh Orca snapshot.
`hold-resolve` returned `start-failed` with `worktree_retained = true`.
After acknowledgement, finalization closed attempt 4's run as failed.
Workspace-wide `capacity.live` was zero and no tasks held capacity.

### Qualification restart

Orca `1.4.271` was ready and Hub policy revision 14 admitted `sol62`. Both
routes passed readiness for Codex `gpt-6.2-sol`, medium effort, and three
separate worktrees at workload revision
`0c29e690312b88ec99adb266281e476cc3fa5516`. Both prompt-free Codex app-server
config reads reported `service_tier = "default"`.

After staging and claiming the first task in 1A, Orca again failed before
agent readiness. Codex reported that
`--dangerously-bypass-approvals-and-sandbox` was passed more than once. The
created worktree was clean at the pinned revision. Orca released the
terminal and a fresh snapshot showed exited liveness. Brigade recovered
the start as `start-failed` with `worktree_retained = true`, accepted
acknowledgement, and finalized the run as failed with no held tasks and
`capacity.live = 0`.

Pair 1A recorded one terminal failure and two unstarted tasks. The failure
prevented the planned three-worker dispatch, so the qualification stopped
at 1A. Effective worker tier, phase timings, and token usage were
unobserved. Peak observed swap was 6.10 GiB. Zero pairs completed.
[Attempt 5 collector record](research/evidence/orca-qualification/attempt-5-pair-01-a/record.json)

| Batch | Verdict | Verified completion | Zero held capacity at batch close |
| --- | --- | --- | --- |
| 1A | Failed at worker startup, then recovered and finalized | Unobserved | Yes |
| 1B | Unstarted | Unobserved | Unobserved |
| 2B | Unstarted | Unobserved | Unobserved |
| 2A | Unstarted | Unobserved | Unobserved |
| 3A | Unstarted | Unobserved | Unobserved |
| 3B | Unstarted | Unobserved | Unobserved |
| 4B | Unstarted | Unobserved | Unobserved |
| 4A | Unstarted | Unobserved | Unobserved |
| 5A | Unstarted | Unobserved | Unobserved |
| 5B | Unstarted | Unobserved | Unobserved |

## Attempt 6

The coordinator traced the duplicate Codex startup flag in attempts 4 and 5
to Orca's active profile. Its default Codex arguments supplied the same
sandbox-bypass flag as the launch configuration. Removing the profile
setting left the profile JSON and SQLite copies out of sync. Orca's
profile-state recovery gate then kept the runtime at `not_running` for
all 18 readiness probes over 172 seconds in the first attempt 6 window.
The operator reconciled the two copies using Orca's profile recovery
command and restarted the runtime before the live run.

The live restart used the same pinned Brigade executable and workload as
attempt 5. Orca `1.4.271` and both routes reported ready. Brigade reported
zero live capacity and zero holds before the batch. Both prompt-free
app-server config reads reported `service_tier = "default"`.

All three governed workers in 1A reached readiness. The local Codex
session records confirmed the selected model and effort and contained
three exact expected outputs. The three worktrees were clean at the pinned
revision. Each worker session recorded `service_tier = "default"` in
the Codex `thread_settings_applied` event.

During closeout, a fresh worker-show failed with
`runtime_unavailable` and the message
`This terminal could not connect to Orca.` The available Orca transcript
receipts were clipped, and live terminal observations showed retained
terminals. Brigade had no terminal result for any of the three tasks.
Brigade status was `awaiting-native-parent` with `capacity.live = 3`
and three held tasks when the coordinator paused the qualification.
The three worktree and output checks establish local task results, while
governed completion still requires terminal and release evidence.

### Closeout after the runtime returned

Orca returned to readiness under a user service. Fresh shows reported all
three native dispatches completed. The coordinator matched the first
worker's `worker_done` message, released its retained terminal, and captured
a fresh show with release state `released` and liveness `exited`. The exact
transcript command from Brigade still returned `contentComplete = false`
with `clipping = ["transcript_payload"]`. Brigade returned status
`unknown`, reason `result-transfer-missing`, and no result ID to acknowledge.

The coordinator stopped at that observation. Brigade still reported
`capacity.live = 3`, three held tasks, and zero terminal results. The
collector counted the outage as one parent intervention at that point.
The batch still lacked a verified completion time.

### Completion with paged transcripts

Brigade commit `225cc9ca4538f40ad36089916e46268eee814618` added ordered
transcript pages and settlement for exact transcripts clipped only as
`transcript_payload`. The coordinator used it to finish 1A on the same
three dispatches, without retrying a worker. Each retained transcript
required two pages. Brigade returned `transcript_clipped = true` for all
three, and the final assistant lines matched the expected outputs. Each
acknowledgement and post-release observation succeeded. The final status
reported `completed`, zero live capacity, and no held tasks.

### Original pair 1 history

Original 1A launched on Brigade `9190c00`. Brigade `225cc9c` handled its
paged closeout and the original 1B. Original 1A remains recovery evidence.
Both original records preserve their passed verdict and zero held capacity.

| Batch | Verdict | Verified elapsed (s) | Command time (s) | Held capacity |
| --- | --- | ---: | ---: | ---: |
| [Original 1A](research/evidence/orca-qualification/attempt-6-pair-01-a/record.json) | Passed | 8,080.964 | At least 57.476 | 0 |
| [Original 1B](research/evidence/orca-qualification/attempt-6-pair-01-b/record.json) | Passed | 54.926 | 14.555 | 0 |

Original 1A spent 22.266 seconds of instrumented command time reaching worker
acceptance, 5.588 seconds on the failed recovery, and 29.622 seconds on paged
closeout. The failure probe was at 21:17:24 UTC. Orca reported ready at
21:23:55 UTC, 391 seconds later. The outage onset was unobserved. The first
timed retry began at 21:25:05 UTC and returned a refusal at 21:25:11 UTC.
Closeout resumed at 23:30:41 UTC after a 7,530.6-second wait for the #2947
transcript fix. The collector
counts the outage and the Brigade re-pin as two parent interventions.

Original 1B's last batch command ended at 23:31:51.969 UTC. The collector
completed its output check at 23:32:32.339 UTC, 40.371 seconds later.

### Matched results

Matched pair 1 is the A-then-B repeat on Brigade `225cc9c`, the same build
used for pairs 2 through 5. All five matched pairs passed. Each batch
returned three exact outputs from clean worktrees at workload revision
`0c29e690312b88ec99adb266281e476cc3fa5516` and closed with zero held capacity.
All 30 worker sessions in the matched pairs recorded the default Codex tier.
Arm A used a read-only prompt. Arm B enforced read-only access through
Brigade's batch policy.

The projected `brigade.orca_qualification.v1` schema omits service tier
and transcript clipping, which this report verifies from the retained
local worker `thread_settings_applied` events and Brigade `observe`
receipts, respectively.

Verified elapsed has a different endpoint for each arm. Arm A runs from
`open.start` to `status.end`, after inline output checks. Arm B runs from
`batch-result.start` to the end of the collector's output and worktree
checks, including any coordinator delay before the collector starts.
Command time uses the same
method for both arms. It sums `end - start` for every retained timing receipt
inside that batch's verified clock span, excluding launcher preflight
outside the span.

| Batch | Verdict | Verified elapsed (s) | Command time (s) | Held capacity | Clipped transcripts |
| --- | --- | ---: | ---: | ---: | ---: |
| [Repeat 1A](research/evidence/orca-qualification/attempt-6-pair-01-repeat-a/record.json) | Passed | 60.818 | 60.203 | 0 | 3 |
| [Repeat 1B](research/evidence/orca-qualification/attempt-6-pair-01-repeat-b/record.json) | Passed | 14.457 | 13.808 | 0 | N/A |
| [2B](research/evidence/orca-qualification/attempt-6-pair-02-b/record.json) | Passed | 12.545 | 12.172 | 0 | N/A |
| [2A](research/evidence/orca-qualification/attempt-6-pair-02-a/record.json) | Passed | 53.821 | 53.358 | 0 | 3 |
| [3A](research/evidence/orca-qualification/attempt-6-pair-03-a/record.json) | Passed | 54.827 | 54.385 | 0 | 3 |
| [3B](research/evidence/orca-qualification/attempt-6-pair-03-b/record.json) | Passed | 19.081 | 18.685 | 0 | N/A |
| [4B](research/evidence/orca-qualification/attempt-6-pair-04-b/record.json) | Passed | 12.370 | 12.027 | 0 | N/A |
| [4A](research/evidence/orca-qualification/attempt-6-pair-04-a/record.json) | Passed | 57.801 | 57.354 | 0 | 3 |
| [5A](research/evidence/orca-qualification/attempt-6-pair-05-a/record.json) | Passed | 53.972 | 53.508 | 0 | 3 |
| [5B](research/evidence/orca-qualification/attempt-6-pair-05-b/record.json) | Passed | 20.664 | 20.193 | 0 | N/A |

Matched pair 1 is the repeat on `225cc9c`. The original 1A took 8,080.964
seconds through an Orca outage and a build change, with at least 57.5
seconds of instrumented work. The original 1B's 54.926 seconds include
40.371 seconds between its last batch command and the collector's output
check. Captured verification, graph sync, ledger indexing, and polling
were outside the batch command paths in both arms. Arm B recorded
`code_graph_delta.status = skipped_read_only`. Peak observed swap for
attempt 6 was 6.17 GiB.

### Protocol overhead and measurement limits

Across pairs 2 through 5, governed Orca batches took about 3.4 times as
long as Brigade batches: 220.421 against 64.660 seconds of verified
elapsed time (3.41 times), and 218.605 against 63.077 seconds of command
time (3.47 times). The matched pair 1 repeat shows the same pattern.

Governed batches in pairs 2 through 5 each made 45 timed calls, 24 Orca
and 21 Brigade, for 53.4 to 57.4 seconds of command time. The longest
gap between commands in those batches was 0.156 seconds. Launch took
22.0 to 26.1 seconds, including 18.4 to 20.0 seconds for the three
blocking worker starts at about six seconds each. Closeout took 31.1 to
32.0 seconds. About 80% of governed command time was in serial Orca CLI
calls, with an observed range of 80% to 84%. Orca closeout calls took
about 1.2 seconds per call. Brigade's closeout proof, including observe,
ack, finalize, and status, took 4.4 to 5.7 seconds per batch.

This protocol overhead can be reduced. Estimated from those timings,
concurrent per-task closeout chains would bring governed batches to
about 41 to 45 seconds. Collapsing closeout calls could save up to about
24 seconds per batch. These are separate estimates. The #2046
qualification sets no speed target, so the measured gap breaks no
threshold.

The phase-separation wording in #2046 is only partly met. Arm A's queue
and startup windows overlap, Arm B's queue and startup fields are unset,
and delivery is unmeasured. Execution windows were available for both
arms. Planning and synthesis were also unmeasured. The token counts below
come from observed worker session counters.

| Batch | Queue (s) | Startup (s) | Execution (s) | Input tokens | Output tokens | Cached input tokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Repeat 1A | 16.545 | 21.360 | 23.346 | 320,855 | 598 | 226,816 |
| Repeat 1B | Unobserved | Unobserved | 5.587 | 210,545 | 246 | 165,632 |
| 2B | Unobserved | Unobserved | 4.980 | 210,517 | 223 | 126,720 |
| 2A | 15.720 | 20.356 | 22.288 | 320,887 | 603 | 233,856 |
| 3A | 15.807 | 20.697 | 23.613 | 320,657 | 543 | 233,856 |
| 3B | Unobserved | Unobserved | 8.739 | 210,567 | 240 | 126,720 |
| 4B | Unobserved | Unobserved | 4.961 | 210,538 | 211 | 119,424 |
| 4A | 18.341 | 22.720 | 24.800 | 320,636 | 520 | 272,768 |
| 5A | 15.776 | 20.470 | 22.433 | 320,628 | 525 | 233,856 |
| 5B | Unobserved | Unobserved | 5.586 | 210,540 | 236 | 131,584 |
