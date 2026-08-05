# Outcome scoring

Outcome capture scores runs at read time from stored receipts. The score is not frozen when the receipt is written. Upgrading Brigade changes how `outcome capture` and `outcome rank` classify the same historical `run.json` and `worker-results.json` files.

## Read-time recomputation

`brigade outcome capture` loads the run receipt, worker results, and any linked verify receipt, then applies the current classifier in `worker_failure.py` and `outcome_cmd.py`. The `signal_value` on the outcome record reflects that read-time classification, not the wording stamped on the receipt at run completion.

Reinstalling or upgrading Brigade therefore re-scores existing history the next time capture runs or when ranking reconciles against stored runs.

## Verdict rules

For failed or incomplete runs:

- `infrastructure_only=True` with no verifier failure scores **0** (neutral).
- `infrastructure_only=False` scores **-1** (negative).
- Successful runs score **+1**.

Verifier failure on a completed verify receipt after the run started forces **-1** even when worker failures are infrastructure-only.

## Kind to domain to verdict

Run-level classification uses `run_failure_is_infrastructure_at_read_time`. Worker-level classification uses `normalized_failure` / `worker_result_failure` and the `domain` field on structured failures. Capture consults run level first; when run level defers (`worker-failure` or no kind), it aggregates failed workers by domain.

### Run-owned failure kinds (nested `failure.kind` or legacy top-level `failure_kind`)

| Kind | Infrastructure at run level | Capture verdict (failed/incomplete, no verifier failure) |
| --- | --- | --- |
| `invalid-plan`, `non-final-output`, `empty-output`, `malformed-final-output`, `tool-only-output`, `suspected-noop-run` | no | -1 |
| `branch-head-drift`, `owner-process-exited`, `transport-error`, `provider-error`, `unsupported-sandbox`, `unexpected-error` | yes | 0 |
| `worker-failure` | defer to workers | see worker table |
| `agent-error`, `orchestrator-error` | yes only when `failure.phase` is `startup`, `run-isolation`, `stale-lock-recovery`, or `dispatch` | 0 or -1 by phase |
| `planning`, `inference`, `output-validation` with catch-all kinds above | no | -1 |
| missing or unknown `failure.phase` with catch-all kinds above | no | -1 |
| Other run-owned kinds (`interrupted`, `spawn-error`, `planner-failure`, etc.) | same catch-all phase rule | 0 or -1 by phase |
| Unknown string kind | no | -1 |
| No failure kind present | defer | -1 if no qualifying worker infra |

### Run-level `FailureClass` values (`failure.kind` equals class name)

| Class | On infrastructure allowlist | Capture verdict |
| --- | --- | --- |
| `configuration-invalid`, `executable-unavailable`, `auth-required`, `entitlement-denied`, `version-gate`, `model-unavailable`, `capacity-exhausted`, `network-unavailable`, `transport-unavailable`, `transport-hang`, `interactive-blocked`, `worker-crash`, `timeout`, `provider-rejected` | yes | 0 |
| `output-contract-violation`, `unclassified`, `isolation-breach` | no | -1 |

### Worker adapter kinds (legacy `failure_kind` on worker-results entries)

| Kind or input | `FailureClass` | Domain | Capture verdict when this worker failed |
| --- | --- | --- | --- |
| `empty-output`, `malformed-final-output`, `tool-only-output`, `non-final-output`, `non-final-stop`, `malformed-transport`, `grok-session-missing`, `suspected-noop-run` | `output-contract-violation` | `model-output` | -1 |
| Unknown adapter kind (not in `LEGACY_FAILURE_KIND_MAP`) | `unclassified` | `model-output` | -1 |
| `auth-status-unavailable`, `auth-status-unrecognized` | `unclassified` | `infrastructure` | 0 when all failed workers are infrastructure |
| Other mapped adapter kinds (e.g. `timeout`, `decode-failure`, `network-error`) | mapped class | `infrastructure` | 0 when all failed workers are infrastructure |
| Structured `brigade.worker_failure.v1` payload | from payload | from payload (`infrastructure` default when omitted) | 0 if all failed workers have `domain=infrastructure`, else -1 |

The two `auth-status-*` kinds are mapped to `unclassified` deliberately: they record a probe Brigade could not run, not output a seat produced. Only kinds absent from `LEGACY_FAILURE_KIND_MAP` fall to the `model-output` domain.

### Suspected no-op runs

A run whose seats all exited ok but produced no non-`.brigade` file changes is recorded on the run receipt as `suspected_noop: true`. Capture reads that flag and scores the run **-1** even though its status is `ok`, because a seat that ships no deliverable did not do the work. Dry runs and read-only runs are exempt and keep their neutral **0**. Receipts that name the failure kind `suspected-noop-run` directly reach the same verdict through the model-quality kind set.

### Status-only outcomes

| Run status | Capture verdict |
| --- | --- |
| `ok` | +1 |
| `error` | -1 |
| `failed` / `incomplete` without infrastructure-only attribution | -1 |
