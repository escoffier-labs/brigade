# Receipt JSON SIEM-readiness audit (#506)

Audit date: 2026-07-26. Scope: the three sanctioned receipt families on `main`:
work verification (`src/brigade/work_cmd/verification.py`), Brigade run receipts
(`src/brigade/run_receipts.py` serializers plus `run.json` and run sidecars in
`aboyeur.py` / `run_resume.py`), and outcome records (`src/brigade/outcome_cmd.py`).
Out of scope: route-decision, runbook, tool-call, daily, skills, and other
receipt producers.

Criteria:

1. **Deterministic field names**: stable string keys, no positional-only meaning.
2. **Stable key ordering**: canonical sorted-key JSON on disk.
3. **Append-only semantics**: new evidence is appended or written once. No silent
   rewrite of historical facts.
4. **Schema version**: machine-readable `schema_version` (and/or documented
   `schema` string) for evolution.

Patch identity from **#485** is present on the audited `main`. Lane **#491**
(telemetry projection) may add fields concurrently. Within a given
`schema_version`, **additive fields only** are permitted. Consumers must ignore
unknown keys.

Tag legend: `compliant`, `fixed-here`, `needs-follow-up-issue`.

## Writer-site inventory

### Work verification (`src/brigade/work_cmd/verification.py`)

| Function | Receipt | Behavior |
| --- | --- | --- |
| `_run_verify_commands` | `receipt.json` | Builds in-memory receipt. Disk write deferred to finalize |
| `_finalize_verify_receipt` | `receipt.json`, `summary.md` | **Single** `helpers._write_json` for `receipt.json`. Markdown is best-effort. Retention pruning follows |
| `_safe_finalize_verify_receipt` | `receipt.json` | Exception wrapper. Emergency write only when finalize itself raises |
| `_write_reused_receipt` | `receipt.json` | New verify-run directory. Copies prior commands. Retention pruning follows |
| `_prune_verify_runs` | verify-run directories | Deletes directories older than the newest 50, including their written receipts |
| `_work_closeout_payload` | `closeout.json` | One write per closeout via `helpers._write_json` |
| `verify_run` | (dispatch) | Entry point for `_run_verify_commands` |

### Run lifecycle `run.json`

| Module / function | Behavior |
| --- | --- |
| `aboyeur.record_run_start` | Initial `run.json` via `_run_payload` + `_write_json`. Detached and regular paths may run before the main loop writes the latest snapshot |
| `aboyeur` run loop (`run`, `plan`, dispatch/synthesis paths) | **In-place** `run.json` rewrites on status transitions (`planning`, `dispatching`, `synthesizing`, terminal states) |
| `aboyeur.record_run_termination` | Merge terminal failure/success into existing `run.json` |
| `aboyeur.record_artifact_collection` | Merge `artifact_collection` block into `run.json` |
| `aboyeur.record_dispatch_stage` | Merge dispatch stage metadata |
| `aboyeur.record_result_processing` | Merge result-processing seat metadata |
| `aboyeur.terminal_sigterm_handler` | SIGTERM path calls `record_run_termination` |
| `run_resume._resume_locked` | Rewrites `run.json` after resume synthesis |
| `runguard._recover_run_artifact` | Stale-lock recovery rewrites `run.json` via `localio.write_json` |
| `cli/run.py` | Error/exit paths call `record_run_termination` |

Shared serializer: `aboyeur._run_payload` (schema + `schema_version`). Shared writer:
`aboyeur._write_json` (`sort_keys=True`, atomic).

### Run sidecars (`.brigade/runs/<id>/`)

| Module / function | File | Builder |
| --- | --- | --- |
| `aboyeur.record_run_start` | `roster.json` | `aboyeur._roster_payload` |
| `aboyeur` planning phase | `plan.json` | `receipt_schema.run_plan_document` |
| `aboyeur` post-dispatch | `worker-results.json` | `receipt_schema.worker_results_document` |
| `aboyeur` post-synthesis | `synthesis.json` | `receipt_schema.synthesis_document` |
| `aboyeur.set_artifact_patch_ref` | `worker-results.json`, `synthesis.json` | Rewrites `ground_truth.patch_ref` on existing sidecars |
| `run_resume._resume_locked` | `worker-results.json`, `synthesis.json` | Same builders after resume |

Entry serialization for worker rows: `run_receipts.worker_payload`,
`run_receipts.assignment_payload`, `run_receipts.agent_result_payload`.

The run directory also contains support artifacts that are not receipts:

| Artifact | Classification |
| --- | --- |
| `pre-run-snapshot.json` | Run-guard input snapshot, not an event receipt |
| `plan-attempts.json` | Planner retry trace, not the accepted `plan.json` receipt |
| `read-only-enforcement.json` | Enforcement evidence sidecar, not a run-state receipt |
| GraphTrail before/after/delta JSON | Code-graph evidence referenced by receipts, not a Brigade receipt type |

### Outcome (`src/brigade/outcome_cmd.py`)

| Function | Receipt | Behavior |
| --- | --- | --- |
| `append_records` | `memory/outcome/records.jsonl` | JSONL append, one sorted object per line |
| `record` | (dispatch) | Builds `OutcomeRecord`, calls `append_records` |
| `_record_payload` | row shape | Adds `schema_version` before append |
| `reconcile` | `memory/outcome/decisions/<stamp>-<slug>.json` | One decision file per applied transition via `localio.write_json` |
| `_decision_path` | decision filename | Second-resolution timestamp plus a lossy artifact slug can collide |

Readers: `outcome_cmd._read_run_receipt` (run.json for capture), `load_records`
(legacy rows without `schema_version` accepted).

## Verification family

| Receipt type | Path | Names | Ordering | Append-only | Schema version | Tag |
| --- | --- | --- | --- | --- | --- | --- |
| Work verify run | `.brigade/work/verify-runs/<id>/receipt.json` | Stable snake_case, command objects use fixed keys and a fixed identity tuple | `helpers._write_json` (`sort_keys=True`) | Finalization now writes once, but `_prune_verify_runs` deletes receipts beyond the newest 50 | `schema_version: 2` | names **compliant**, ordering **compliant**, write-once **fixed-here**, retention **needs-follow-up-issue**, version **compliant** |
| Work verify reuse | same layout | Copies prior `commands`, same key set and a fresh identity tuple | same | New directory per reuse, followed by the same retention pruning | `schema_version: 2` | names **compliant**, ordering **compliant**, append-only **needs-follow-up-issue**, version **compliant** |
| Work closeout | `.brigade/work/closeouts/<id>/closeout.json` | Stable keys, nested session/verification summaries | `helpers._write_json` | Written once per closeout | `schema_version: 1` | names **compliant**, ordering **compliant**, append-only **compliant**, version **fixed-here** |

Notes:

- Verify `commands[].env` is a sorted list of `KEY=value` strings (**compliant**).
- `digests.receipt_sha256` uses `localio.canonical_json_digest` (sorted keys)
  (**compliant**).
- The 50-run retention cap is operationally useful, but deletion is not
  append-only evidence storage (**needs-follow-up-issue**).

## Brigade run family

| Receipt type | Path | Names | Ordering | Append-only | Schema version | Tag |
| --- | --- | --- | --- | --- | --- | --- |
| Run lifecycle | `.brigade/runs/<id>/run.json` | Stable keys, status values enumerated | `aboyeur._write_json` (`sort_keys=True`) | **In-place lifecycle mutation** across statuses | `schema` + `schema_version: 1` | names **compliant**, ordering **fixed-here**, append-only **needs-follow-up-issue**, version **fixed-here** |
| Roster snapshot | `roster.json` | `schema` + roster fields | sorted writer | Write-once at run start | `schema` + `schema_version: 1` | names **compliant**, ordering **fixed-here**, append-only **compliant**, version **fixed-here** |
| Run plan | `plan.json` | `schema` + `assignments` | sorted writer | Write-once per planning phase | `schema` + `schema_version: 1` | names **compliant**, ordering **fixed-here**, append-only **compliant**, version **fixed-here** |
| Worker results | `worker-results.json` | `schema` + `results` from `run_receipts.py` | sorted writer | `run_resume._resume_locked` and `aboyeur.set_artifact_patch_ref` rewrite the sidecar | `schema` + `schema_version: 1` | names **compliant**, ordering **fixed-here**, append-only **needs-follow-up-issue**, version **fixed-here** |
| Synthesis | `synthesis.json` | `schema` + orchestrator/result | sorted writer | `run_resume._resume_locked` and `aboyeur.set_artifact_patch_ref` rewrite the sidecar | `schema` + `schema_version: 1` | names **compliant**, ordering **fixed-here**, append-only **needs-follow-up-issue**, version **fixed-here** |

Notes:

- `run.json` omits explicit JSON `null` for absent `cwd` (**fixed-here**).
- `record_run_termination` / `runguard._recover_run_artifact` merge into existing
  `run.json`. The latest-snapshot file supports `brigade runs watch` polling. Append-only lifecycle events
  deferred (**needs-follow-up-issue**).
- Resume salvage and patch-ref binding both overwrite `worker-results.json` and
  `synthesis.json`. Preserving each attempt as a new sidecar is deferred
  (**needs-follow-up-issue**).
- Patch identity from #485 uses verify `schema_version: 2`. Future #491 telemetry
  cross-references must remain additive within the version used by each receipt
  family.

### Null and absent-field evidence

These cases support the deterministic-names findings in the family tables:

| Shape | Policy |
| --- | --- |
| Verify `baseline_commit`, `tree_fingerprint`, `changes_patch_sha256` | Explicit `null` tuple when identity capture is unavailable in version 2 |
| Verify `reused_from` | Omitted when no source run id is available |
| Verify command `exit_code` | Explicit `null` means no child exit status exists |
| Run `cwd` | Omitted when unavailable |
| Roster agent `env`, model metadata | Fixed snapshot rows retain explicit `null` |
| Synthesis `orchestrator` | Explicit `null` identifies direct-worker mode |
| Work closeout `task`, `verification` | Explicit `null` records that no item was available |
| Outcome `prev_digest` | Explicit `null` identifies the first chain row |

## Outcome family

| Receipt type | Path | Names | Ordering | Append-only | Schema version | Tag |
| --- | --- | --- | --- | --- | --- | --- |
| Outcome ledger row | `memory/outcome/records.jsonl` | Stable snake_case from `OutcomeRecord` | `json.dumps(..., sort_keys=True)` per line | JSONL append with digest chain. Concurrent writers can select the same predecessor | `schema_version: 1` | names **compliant**, ordering **compliant**, append-only **needs-follow-up-issue**, version **fixed-here** |
| Reconcile decision | `memory/outcome/decisions/<stamp>-<slug>.json` | Stable keys, nested `score` | `localio.write_json` | Second-resolution, lossy-slug filenames can overwrite a prior decision | `schema_version: 1` | names **compliant**, ordering **compliant**, append-only **needs-follow-up-issue**, version **fixed-here** |
| Status cache | `memory/outcome/status.json` | `version` + `artifacts` map | sorted write | Regenerated from decisions (derived cache) | `version: 1` (file format, not receipt `schema_version`) | names **compliant**, ordering **compliant**, append-only **compliant**, version **compliant** (documented derived cache) |

Notes:

- Legacy rows without `schema_version` still load via `_record_from_dict` and
  `_read_run_receipt` (**fixed-here** backward-compat tests).
- `append_records` does not lock the last-digest read and append as one
  transaction (**needs-follow-up-issue**).

## Follow-up issues (draft titles)

1. **`receipts: replace mutable run.json snapshots with append-only lifecycle events`**
2. **`receipts: preserve worker and synthesis sidecars across resume and patch-ref updates`**
3. **`receipts: archive verification evidence before retention pruning`**
4. **`outcome: make decision receipt filenames collision-safe and write-exclusive`**
5. **`outcome: serialize digest-chain appends across concurrent captures`**

## Finding counts

Counts are tag occurrences across the four criterion findings for each receipt
type. The work-verify append-only cell has both a fixed write-once defect and a
remaining retention follow-up.

| Tag | Count |
| --- | --- |
| **compliant** | 24 |
| **fixed-here** | 14 |
| **needs-follow-up-issue** | 7 |

See [`receipt-schemas.md`](receipt-schemas.md) for field-level contracts and
evolution rules.
