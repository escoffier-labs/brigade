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
