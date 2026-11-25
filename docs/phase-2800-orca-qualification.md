# Governed Orca qualification for epic #2800

Workstream 6 (#2884) stopped before Pair 1A when the local Orca coordinator probe returned `runtime_unavailable`. Zero of ten batches started. No workers, matched pairs, slot checks, or collector records resulted from this attempt.

## Preflight configuration

| Setting | Observed or planned value |
| --- | --- |
| Orca CLI version observed | `1.4.271` |
| Brigade checkout and local verification commit | `126cb1270e868613a4caa8814e44f3f0777af9d5` (includes #2886, #2892, and #2911) |
| Worker seat | `sol62`, enabled in Hub registry revision 297 for `brigade-run` |
| Model and effort | Codex `gpt-6.2-sol`, medium, confirmed in the seat registry and selected for both arms |
| Planned concurrency and isolation | Three workers in separate worktrees per batch |
| Planned read-only enforcement | Worker prompt in Arm A. Hard read-only batch policy in Arm B |
| Workload revision for this attempt | No revision staged before the stop |

`brigade fleet models list --seat sol62` confirmed the provider binding, model and effort. The current plan already uses `sol62`, which replaces the unregistered `worker` example from the previous attempt. Registry enablement establishes seat selection. Batch admission and worker capacity were untested in this attempt.

## Service-tier evidence

| Arm | Configuration evidence | Effective worker tier |
| --- | --- | --- |
| A, governed Orca | Orca's Codex runtime-home config declared `service_tier = "default"` and `fast_default_opt_out = true` | Unobserved |
| B, Brigade run-batch | The inherited Codex config declared `service_tier = "default"` | Unobserved |

Only the configuration fields were inspected for tier evidence. Worker session evidence was unavailable because zero workers started.

## Coordinator stop

`orca-ide orchestration run-current --environment local --json` exited 1. Its output was:

```text
{"ok":false,"error":{"code":"runtime_unavailable","message":"This terminal could not connect to Orca.","data":{"nextSteps":["Orca is usually busy or restarting: wait a few seconds, then retry. If the command changes something, first check whether it already took effect, and retry with the request ID this error names, if any.","Do not run 'orca open' to recover. Run it only if the user asks, or if 'orca status --json' reports Orca is not running or still cannot connect after about 30 seconds of retries."]}}}
error: runtime_unavailable: This terminal could not connect to Orca.
```

This was a preflight error. Per-batch readiness, roster snapshots, and post-batch capacity queries were never reached. No attempt journal or redacted collector record was generated.

## Batch verdicts in planned order

A uses governed Orca. B uses explicit Brigade batch execution. A dash marks an unobserved field. Times would be in seconds. Usage would include input, output, and cached-input tokens.

| Batch | Arm | Verdict | Verified completion time | Errors | Parent interventions | Usage | Queue | Startup | Planning | Execution | Synthesis | Delivery | Matched or mismatched | Effective service tier |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1A | A | Stopped at preflight | - | - | - | - | - | - | - | - | - | - | Unassessed | Unobserved |
| 1B | B | Unstarted | - | - | - | - | - | - | - | - | - | - | Unassessed | Unobserved |
| 2B | B | Unstarted | - | - | - | - | - | - | - | - | - | - | Unassessed | Unobserved |
| 2A | A | Unstarted | - | - | - | - | - | - | - | - | - | - | Unassessed | Unobserved |
| 3A | A | Unstarted | - | - | - | - | - | - | - | - | - | - | Unassessed | Unobserved |
| 3B | B | Unstarted | - | - | - | - | - | - | - | - | - | - | Unassessed | Unobserved |
| 4B | B | Unstarted | - | - | - | - | - | - | - | - | - | - | Unassessed | Unobserved |
| 4A | A | Unstarted | - | - | - | - | - | - | - | - | - | - | Unassessed | Unobserved |
| 5A | A | Unstarted | - | - | - | - | - | - | - | - | - | - | Unassessed | Unobserved |
| 5B | B | Unstarted | - | - | - | - | - | - | - | - | - | - | Unassessed | Unobserved |
