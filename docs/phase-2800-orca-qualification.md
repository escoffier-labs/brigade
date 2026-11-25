# Governed Orca qualification for epic #2800

Workstream 6 (#2884) reached the first governed claim after the Orca desktop RPC connection was restored. Brigade refused the claim. One batch opened, zero workers launched, and zero of the five planned pairs completed. There is no live timing or usage comparison.

## Attempt history

- The first attempt stopped at preflight. The run-batch route could not admit the unregistered `worker` seat and the Orca coordinator probe returned `runtime_unavailable`.
- The second attempt stopped at preflight. The coordinator probe again returned `runtime_unavailable` after #2892 and #2911 had merged.
- In this attempt, Orca status reported a connected runtime and zero supervised workers. The qualification staged three native tasks in a dedicated coordinator terminal. Both route readiness checks returned `ready`, and the Pair 1A Orca check returned `ready` again immediately before the roster snapshot and batch open.

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
