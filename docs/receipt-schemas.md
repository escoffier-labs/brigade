# Brigade receipt schema reference

Machine-readable contracts for the three sanctioned receipt families audited in
[#506](receipt-schema-audit.md). These are documentation contracts, not runtime
JSON Schema files.

## Evolution rules (all families)

1. **`schema_version` is an integer.** Bump only for breaking changes (rename,
   remove, or change the type/meaning of an existing field).
2. **Within a `schema_version`, evolution is additive only.** New optional fields
   may appear (including #491 telemetry projection). Patch identity from #485
   is already represented by verify `schema_version: 2`. Consumers **must
   ignore unknown keys**.
3. **Absent vs null:** optional fields are **omitted** when unset. Producers should
   not write JSON `null` for optional top-level fields unless the field's presence
   carries semantic meaning. Intentional nullable shapes:
   - Verify version 2 always includes `baseline_commit`, `tree_fingerprint`, and
     `changes_patch_sha256`. All three are `null` when identity capture is
     unavailable, keeping the binding tuple structurally complete.
   - Verify `commands[].exit_code` may be `null` when an interrupted or timed-out
     child has no exit status. A command rejected before execution uses exit code `2`.
   - Run `scheduler.used` may be `null` while a run is in flight.
   - Run `roster.json` agent rows may include `"env": null` when the seat has no
     env overrides (snapshot preserves the roster table shape).
   - Synthesis `orchestrator` may be explicit JSON `null` in direct-worker mode.
   - Outcome `prev_digest` may be `null` on the first ledger row.
   - Work closeout `task` and `verification` may be `null` when absent.
   - Reused verify receipts **omit** `reused_from` when the source receipt lacks
     a `run_id`.
4. **Serialization:** on-disk JSON uses UTF-8, indent 2, trailing newline, and
   `sort_keys=True` at every object level unless noted (JSONL: one sorted object
   per line).
5. **Readers** must accept records **without** `schema_version` (pre-#506 storage).

---

## `brigade.work_verify_receipt`: `schema_version: 2`

**Path:** `.brigade/work/verify-runs/<run-id>/receipt.json`

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `schema_version` | integer | yes (new writes) | Always `2` for this contract |
| `run_id` | string | yes | Timestamp-prefixed id |
| `target` | string | yes | Absolute workspace path |
| `status` | string | yes | `running`, `completed`, `failed`, `rejected`, `canceled` |
| `started_at` | string (ISO-8601) | yes | UTC timestamp |
| `completed_at` | string (ISO-8601) | no | Set at finalize |
| `duration_seconds` | number | no | Wall time |
| `timeout` | integer | no | Per-command timeout seconds |
| `path` | string | yes | Run directory path |
| `commands` | array of object | yes | See command object below |
| `planned_commands` | array of string | no | Display argv joined |
| `evidence` | object | no | Workspace evidence snapshot |
| `baseline_commit` | string \| null | yes | Verified Git baseline, or `null` when identity capture is unavailable |
| `tree_fingerprint` | string \| null | yes | Verified Git tree hash, or `null` with the unavailable identity tuple |
| `changes_patch_sha256` | string \| null | yes | SHA-256 of `changes.patch`, or `null` with the unavailable identity tuple |
| `git` | object | no | `{head, branch, dirty_files}` |
| `code_graph_delta` | object | no | GraphTrail summary |
| `harness_session` | object | no | `{harness, fingerprint}` |
| `digests` | object | no | `{algorithm, logs, receipt_sha256, signature?, key_id?}` |
| `reused_from` | string | no | Prior run id when reused |
| `interruption` | object | no | Cancel metadata |
| `verify_manifest_id` | string | no | Registered manifest id when the run was manifest-selected |
| `required_utility_check_ids` | array of string | no | Manifest-owned utility guardrail ids required for scoring (#503) |
| `subject_binding` | object | no | Verifier-authored scoreable subject metadata (manifest runs only) |
| `failure_class` | string | no | Receipt-level #474-style failure class when status is not completed |
| `failure_kind` | string | no | Receipt-level failure kind paired with `failure_class` |

**`subject_binding` object** (additive, manifest-selected runs)

| Field | Type | Notes |
| --- | --- | --- |
| `binding_mode` | string | `patch_backed` or `fixture_eval` |
| `artifact_kind` | string | `skill` or `card` |
| `artifact_id` | string | Verifier-owned subject id |
| `content_fingerprint` | string | Subject content fingerprint at verify time |
| `patch_source` | string | `worktree` or `generated` (patch-backed only) |
| `producer_binding` | object | `{work_session_id, owned_delta_sha256, subject_clean_at_start, start_git}` for patch-backed runs |
| `verifier_identity` | object | `{verifier_id, session_id}` independent verifier session |
| `patch_binding` | object | Patch-backed tuple plus `subject_path` and `subject_hash` |
| `fixture_binding` | object | `{manifest_id, case_id, check_id}` for fixture evaluation runs |

Ad hoc `--command` / `--argv-json` runs omit `subject_binding` and remain audit-only (non-scoreable).

**Command object**

| Field | Type | Notes |
| --- | --- | --- |
| `command` | string | Display command |
| `argv` | array of string | no | Resolved argv |
| `env` | array of string | Sorted `KEY=value` pairs |
| `status` | string | `completed`, `failed`, `timed_out`, `rejected`, `interrupted` |
| `exit_code` | integer \| null | Child exit status. `null` when interrupted or timed out without one. Rejected commands use `2` |
| `started_at`, `completed_at` | string | ISO-8601 |
| `duration_seconds` | number | |
| `stdout_summary`, `stderr_summary` | string | |
| `stdout_log_path`, `stderr_log_path` | string | Paths under run dir |
| `check_role` | string | `effectiveness` or `utility_guardrail` (manifest-selected runs) |
| `check_id` | string | Stable verifier-owned check id (manifest-selected runs) |
| `obligation_id` | string | Optional obligation id from the manifest |
| `failure_class` | string | #474-style class when the command did not succeed |
| `failure_kind` | string | Typed failure kind paired with `failure_class` |

---

## `brigade.work_closeout`: `schema_version: 1`

**Path:** `.brigade/work/closeouts/<closeout-id>/closeout.json`

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `schema_version` | integer | yes (new writes) | `1` |
| `closeout_id` | string | yes | |
| `target` | string | yes | |
| `status` | string | yes | `ready` or `blocked` |
| `ready` | boolean | yes | |
| `created_at` | string | yes | ISO-8601 |
| `session` | object | yes | Session summary |
| `session_path` | string | yes | |
| `task` | object \| null | | Task summary |
| `acceptance_criteria` | array | yes | |
| `verification` | object \| null | | Latest verify receipt ref |
| `scanner_sweep` | object | yes | |
| `code_review` | object | yes | |
| `handoff_drafts` | object | yes | |
| `blockers` | array of string | yes | |

**Session summary** (`session` object)

| Field | Type | Notes |
| --- | --- | --- |
| `path` | string | Session directory |
| `id` | string | Session id |
| `status` | string | Session status |
| `title` | string \| null | |
| `started_at`, `ended_at` | string \| null | ISO-8601 |
| `note`, `latest_note` | string \| null | |
| `handoff` | object \| null | Handoff metadata when present |
| `branch` | string \| null | Git branch from snapshot |
| `dirty_files` | integer | Count from snapshot |
| `next` | string \| null | Suggested next step |

**Verification summary** (`verification` object, when present)

| Field | Type | Notes |
| --- | --- | --- |
| `run_id` | string | Latest verify run id |
| `status` | string | Verify receipt status |
| `path` | string | Verify run directory |
| `command_count` | integer | Number of command records |

---

## `brigade.run.v1`: `schema_version: 1`

**Path:** `.brigade/runs/<run-id>/run.json`

The required column below describes normal run creation. Stale-lock recovery may
create the partial recovery variant documented after the main table when the
original file is missing, corrupt, or not an object.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `schema` | string | yes | Always `brigade.run.v1` |
| `schema_version` | integer | yes (new writes) | `1` |
| `task` | string | yes | |
| `cwd` | string | no | Omitted when unknown |
| `orchestrator` | string | yes | Seat name |
| `dry_run`, `read_only` | boolean | yes | |
| `status` | string | yes | Lifecycle status |
| `started_at`, `status_started_at` | string | yes | ISO-8601 UTC (`Z`) |
| `finished_at` | string | no | |
| `duration_seconds` | number | no | |
| `suspected_noop` | boolean | yes | |
| `code_graph_brief`, `drift_impact_brief`, `evidence_brief`, `brief_budget` | object | yes | Brief attachment summaries |
| `scheduler` | object | no | `{requested, used, fallback_reason}` |
| `roster` | object | no | Resolution metadata |
| `lock_workspace` | string | no | |
| `route` | object | no | Routing brief |
| `worker` | string | no | Direct-worker seat |
| `git` | object | no | |
| `pre_run_snapshot` | object | no | Run-guard snapshot |
| `code_graph_delta` | object | no | |
| `context_eval` | object | no | |
| `artifacts` | string | no | Output directory |
| `handoff` | string | no | Handoff path |
| `error` | string | no | |
| `failure_phase`, `failure_kind` | string | no | |
| `failure` | object | no | `{phase, kind, detail, seat?}` |
| `transport_warning` | object | no | |
| `codex_transport` | string | no | |
| `control_transport`, `control_socket` | object / string | no | |
| `active_stage` | integer | no | Current dispatch stage |
| `active_seats` | array of string | no | Seats active in the current dispatch stage |
| `phase_owner` | string | no | Seat responsible for result processing |
| `artifact_collection` | object | no | Artifact-retention result |
| `resumed_at` | array of string | no | ISO-8601 resume timestamps |
| `recovery_history` | array of object | no | Prior failure objects retained after a successful resume |

**Partial stale-recovery variant**

When no valid original `run.json` object survives, `runguard._recover_run_artifact`
writes this smaller receipt:

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `schema` | string | yes | `brigade.run.v1` |
| `schema_version` | integer | yes | `1` |
| `artifacts` | string | yes | Recovered run directory |
| `recovery_preserved_artifact` | string | no | Renamed corrupt source, when one existed |
| `cwd`, `lock_workspace` | string | no | Recovered from lock metadata |
| `started_at` | string | no | Recovered lock acquisition time |
| `status`, `status_started_at`, `finished_at` | string | yes | Terminal recovery state and timestamps |
| `error`, `failure_phase` | string | yes | Recovery summary |
| `failure` | object | yes | Stale-lock failure variant documented below |

**Brief attachment objects** (`code_graph_brief`, `drift_impact_brief`, `evidence_brief`)

| Field | Type | Notes |
| --- | --- | --- |
| `attached` | boolean | Whether the brief was attached |
| `bytes` | integer | Serialized brief size |

`drift_impact_brief` also includes `pending_count` (integer).

**`brief_budget` object**

| Field | Type | Notes |
| --- | --- | --- |
| `bytes` | integer | Budget ceiling |
| `attached` | array of object | Rows shaped `{name: string, bytes: integer, truncated: boolean}` |

**`scheduler` object**

| Field | Type | Notes |
| --- | --- | --- |
| `requested` | string | Requested scheduler name |
| `used` | string \| null | Resolved scheduler. `null` while unresolved |
| `fallback_reason` | string \| null | `null` unless a fallback scheduler was used |

**`roster` object** (resolution metadata on `run.json`)

| Field | Type | Notes |
| --- | --- | --- |
| `path` | string | Resolved roster file path |
| `source` | string | Resolution source label |
| `shadowed` | array of string | Shadowed roster paths |

**`failure` object**

| Field | Type | Notes |
| --- | --- | --- |
| `phase` | string | Failure phase |
| `kind` | string | Failure kind |
| `detail` | string | Human-readable detail |
| `seat` | string | Optional single seat attribution |
| `seats` | array of string | Optional multi-seat attribution |
| `owner_pid` | integer | Stale-lock recovery only |
| `prior_status` | string | Stale-lock recovery only |
| `recovered_at` | string | Stale-lock recovery only, ISO-8601 |

**`artifact_collection` object**

| Field | Type | Notes |
| --- | --- | --- |
| `status` | string | `ok` or `failed` |
| `patch_ref` | string | Relative patch path when collected |
| `changed` | boolean | Whether the worktree changed |
| `tracked_count`, `untracked_count` | integer | Change counts |
| `worktree` | string | Detached worktree path when used |
| `failure` | object | Same shape as `failure` when collection failed |

**Lifecycle:** this file is **updated in place** during a run. Treat each write as
the latest snapshot, not an append-only log. Sidecars (`roster.json`, `plan.json`,
`worker-results.json`, `synthesis.json`) are write-once per phase (resume salvage
and patch-ref binding may rewrite worker/synthesis artifacts).

---

## `brigade.roster_snapshot.v1`: `schema_version: 1`

**Path:** `.brigade/runs/<run-id>/roster.json`

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `schema` | string | yes | Always `brigade.roster_snapshot.v1` |
| `schema_version` | integer | yes (new writes) | `1` |
| `orchestrator` | string | yes | Seat name |
| `max_workers` | integer | yes | |
| `timeout_seconds` | integer \| null | yes | |
| `allow_models` | array of string | yes | |
| `sandbox` | string \| null | yes | |
| `agents` | object | yes | Seat name → agent row (below) |

**Agent row** (values in `agents`)

| Field | Type | Notes |
| --- | --- | --- |
| `cli` | string \| null | CLI adapter name for direct seats |
| `model` | string \| null | Model id |
| `reasoning` | string \| null | Reasoning effort tier |
| `transport` | string | `direct`, `acpx`, `app-server`, etc. |
| `transport_version` | string \| null | Transport adapter version |
| `role` | string | `orchestrator` or `worker` |
| `timeout_seconds` | number \| null | Per-seat timeout override |
| `invalid_final_fallback` | string \| null | Fallback seat for invalid finals |
| `read_only_capable` | boolean | Whether the seat may run read-only |
| `env` | object \| null | Env override table (names/refs only) |

---

## `brigade.run_plan.v1`: `schema_version: 1`

**Path:** `.brigade/runs/<run-id>/plan.json`

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `schema` | string | yes | Always `brigade.run_plan.v1` |
| `schema_version` | integer | yes (new writes) | `1` |
| `assignments` | array of object | yes | See assignment object below |

**Assignment object** (`run_receipts.assignment_payload`)

| Field | Type | Notes |
| --- | --- | --- |
| `stage` | integer | Dispatch stage number |
| `worker` | string | Assigned seat name |
| `task` | string | Task text for the worker |
| `covers` | array of string | Optional covered artifact ids |

---

## `brigade.worker_results.v1`: `schema_version: 1`

**Path:** `.brigade/runs/<run-id>/worker-results.json`

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `schema` | string | yes | Always `brigade.worker_results.v1` |
| `schema_version` | integer | yes (new writes) | `1` |
| `results` | array of object | yes | Worker result entries (below) |
| `ground_truth` | object | no | No-op / ground-truth metadata when present |

**Worker result entry** (`run_receipts.worker_payload`)

| Field | Type | Notes |
| --- | --- | --- |
| `worker` | string | Seat name |
| `task` | string | Assigned task text |
| `ok` | boolean | Whether the worker succeeded |
| `detail` | string | Failure or status detail |
| `text` | string | Worker output text |
| `transport` | string | Transport used |
| `failure_phase` | string | Optional failure phase |
| `failure_kind` | string | Optional failure kind |
| `transport_warning` | object | Optional transport warning metadata |
| `thread_id` | string | App-server thread id when resumable |
| `status` | string | App-server turn status (with `thread_id`) |
| `exit_code` | integer | Optional child exit code |
| `timed_out` | boolean | Present when exit metadata or timeout applies |
| `stdout_log`, `stderr_log` | string | Optional log paths under the run dir |
| `duration_seconds` | number | Optional wall time |
| `requested_model` | string | Optional requested model |
| `effective_model` | string | Optional resolved model |
| `reasoning` | string | Optional reasoning tier |
| `stop_reason` | string | Optional terminal reason |
| `protocol_version` | integer | Optional protocol version |
| `session_id` | string | Optional session id |
| `request_id` | string | Optional request id |
| `acpx_version` | string | Optional ACPX adapter version |
| `events` | array of object | Optional redacted transport events |
| `env_overrides` | array of string | Sorted env override key names |
| `endpoint_host` | string | Comma-joined endpoint hosts from env |
| `attempts` | array of object | Optional retry log (below) |

**Attempt object** (`run_receipts._attempt_payload`)

| Field | Type | Notes |
| --- | --- | --- |
| `kind` | string | Attempt kind label |
| `worker` | string | Seat name |
| `task` | string | Task text |
| `transport` | string | Transport used |
| `model` | string \| null | Model id |
| `reasoning` | string \| null | Reasoning tier |
| `started_at`, `finished_at` | string | ISO-8601 timestamps |
| `exit_code` | integer \| null | Child exit code |
| `terminal_reason` | string | Terminal status label |
| `failure_phase` | string \| null | Failure phase when applicable |
| `failure_kind` | string \| null | Failure kind when applicable |
| `session_id` | string \| null | Session id when applicable |
| `selected` | boolean | Whether this attempt was selected |
| `stdout_log`, `stderr_log` | string | Optional log paths |

---

## `brigade.synthesis.v1`: `schema_version: 1`

**Path:** `.brigade/runs/<run-id>/synthesis.json`

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `schema` | string | yes | Always `brigade.synthesis.v1` |
| `schema_version` | integer | yes (new writes) | `1` |
| `orchestrator` | string \| null | no | Orchestrator seat. `null` in direct-worker mode |
| `worker` | string | no | Direct-worker seat name when `mode` is `direct-worker` |
| `mode` | string | no | `direct-worker` when dispatch skipped planning/synthesis |
| `result` | object | yes | `{ok, detail, text}` from `run_receipts.agent_result_payload` |
| `ground_truth` | object | no | Copied from worker-results when present |

**Synthesis `result` object** (`run_receipts.agent_result_payload`)

| Field | Type | Notes |
| --- | --- | --- |
| `ok` | boolean | Whether synthesis succeeded |
| `detail` | string | Failure or status detail |
| `text` | string | Synthesis output text |
| `transport` | string | Transport used |
| `failure_phase` | string | Optional failure phase |
| `failure_kind` | string | Optional failure kind |
| `transport_warning` | object | Optional transport warning metadata |
| `exit_code` | integer | Optional child exit code |
| `timed_out` | boolean | Present when exit metadata or timeout applies |
| `stdout_log`, `stderr_log` | string | Optional log paths |
| `duration_seconds` | number | Optional wall time |
| `requested_model` | string | Optional requested model |
| `effective_model` | string | Optional resolved model |
| `reasoning` | string | Optional reasoning tier |
| `stop_reason` | string | Optional terminal reason |
| `protocol_version` | integer | Optional protocol version |
| `session_id` | string | Optional session id |
| `request_id` | string | Optional request id |
| `acpx_version` | string | Optional ACPX adapter version |
| `events` | array of object | Optional redacted transport events |

---

## `brigade.outcome_record`: `schema_version: 1`

**Path:** `memory/outcome/records.jsonl` (one object per line)

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `schema_version` | integer | yes (new writes) | `1` |
| `artifact_id` | string | yes | Skill or card id |
| `artifact_kind` | string | yes | `skill` or `card` |
| `task_id` | string | yes | May be empty |
| `source` | string | yes | `verify`, `run`, `friction`, … |
| `signal_value` | integer | yes | `-1`, `0`, or `+1` |
| `evidence_ref` | string | yes | Path to receipt |
| `ts` | string | yes | ISO-8601 |
| `prev_digest` | string \| null | yes | Chain link |
| `digest` | string | yes | Row digest |
| `code_graph_delta` | object | no | Compact delta |
| `context_eval` | object | no | |
| `content_fingerprint` | string | no | Artifact bytes hash |
| `context` | object | no | Harness manifest |
| `capability_fingerprint` | string | no | |
| `route` | object | no | Route manifest |
| `route_fingerprint` | string | no | |

---

## `brigade.outcome_decision`: `schema_version: 1`

**Path:** `memory/outcome/decisions/<timestamp>-<slug>.json`

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `schema_version` | integer | yes (new writes) | `1` |
| `artifact_id` | string | yes | |
| `action` | string | yes | `install`, `rollback`, `hold`, … |
| `prior_status`, `new_status`, `decided_status` | string | yes | |
| `reason` | string | yes | |
| `score` | object | yes | Scoring breakdown |
| `execution` | string | yes | Physical side-effect result |
| `created_at` | string | yes | ISO-8601 |
| `content_fingerprint` | string | no | Current content fingerprint when stale evidence was excluded |
| `lifetime_score` | number | no | Lifetime score before fingerprint filtering |
| `lifetime_helped` | integer | no | Lifetime positive-signal count |
| `lifetime_hurt` | integer | no | Lifetime negative-signal count |
| `stale_records` | integer | no | Records excluded as stale |
| `legacy_records` | integer | no | Records without a content fingerprint |

---

## Related commands

- `brigade receipts verify`: digest chain checks for verify receipts and outcome rows
- `brigade receipts export miseledger`: adapter export (separate `miseledger.adapter.v1` envelope)
- `brigade outcome rebuild-status`: prove `status.json` matches decision receipts
