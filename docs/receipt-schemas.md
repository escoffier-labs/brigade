# Brigade receipt schema reference

Machine-readable contracts for the three sanctioned receipt families audited in
[#506](receipt-schema-audit.md). These are documentation contracts, not runtime
JSON Schema files.

## Evolution rules (all families)

1. **`schema_version` is an integer.** Bump only for breaking changes (rename,
   remove, or change the type/meaning of an existing field).
2. **Within a `schema_version`, evolution is additive only.** New optional fields
   may appear (including #491 telemetry projection and #493 `causal_receipt`).
   Patch identity from #485
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

## `brigade.run_event.v1`: `schema_version: 2`

Lifecycle journals that contain `approval`, `run.ship`, or `run.merge` events
use schema version 2. Readers older than version 2 refuse these events with
`unknown event_type` rather than replaying them under an older contract.

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
| `producer_run_id` | string | no | Orchestrator run id from `BRIGADE_RUN_ID` when the producer ran under a Brigade run (#499). Omitted when unset; legacy receipts without it remain readable |
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
| `verification_contract` | object | no | Declared `brigade.verification_contract.v1` when the verify manifest carried one (#500) |
| `budget_use` | object | no | Observed latency/token use against the declared verification budget (#500). Hard wall-clock / worker-dispatch enforcement is #593 (`run_budget.*` lifecycle events). |
| `verification` | object | no | Verification outcome stamped separately from model completion (#500) |
| `model_completion` | object | no | For contract-bearing verify receipts: `{status: not_applicable, detail}` because verify is verifier-owned |

**`verification_contract` object** (`brigade.verification_contract.v1`)

Declared before execution on consequential runbooks (`consequential: true`) and
consequential verify manifests. Plan reports incompleteness when any of verifier,
rollback, or budget is missing. On verify manifests the verifier may omit an
explicit command and use `source: manifest_checks` (the manifest's own checks).

| Field | Type | Notes |
| --- | --- | --- |
| `schema` | string | `brigade.verification_contract.v1` |
| `schema_version` | integer | `1` |
| `verifier` | object | `{source, command?\|argv?\|manifest_id?}` with `source` in `command`, `argv`, `manifest_id`, `manifest_checks` |
| `rollback` | object | `{policy, command?}` with `policy` in `none`, `manual`, `command`, `git-restore` |
| `budget` | object | `{latency_seconds, token_budget?, wall_clock_seconds?, worker_dispatch_count?, input_tokens?, output_tokens?}` — #500 declaration; #593 enforces wall-clock and worker-dispatch ceilings before new work starts. Aggregate `token_budget` (receipt `tokens_used`) stays observed and is not reinterpreted as `input_tokens`. Optional `input_tokens` / `output_tokens` declare explicit split observed caps. When `wall_clock_seconds` is unset, `latency_seconds` maps to the wall-clock ceiling. |

**`budget_use` object**

| Field | Type | Notes |
| --- | --- | --- |
| `latency_seconds_budget` | integer | Declared latency ceiling |
| `latency_seconds_used` | number \| null | Observed wall time |
| `token_budget` | integer \| null | Declared aggregate token ceiling when set |
| `tokens_used` | integer \| null | Observed aggregate tokens when an adapter reports them |
| `input_tokens_budget` | integer | Declared input-token cap when set |
| `output_tokens_budget` | integer | Declared output-token cap when set |
| `exhausted` | boolean | Observation only; not an enforcement gate |

**`subject_binding` object** (additive, manifest-selected runs)

| Field | Type | Notes |
| --- | --- | --- |
| `binding_mode` | string | `patch_backed` or `fixture_eval` |
| `artifact_kind` | string | `skill` or `card` |
| `artifact_id` | string | Verifier-owned subject id |
| `content_fingerprint` | string | Subject content fingerprint at verify time |
| `manifest_binding` | object | `{manifest_id, payload_sha256, source_path?}` for the exact tracked verifier manifest |
| `patch_source` | string | `worktree` or `generated` (patch-backed only) |
| `generated_patch_quarantine` | object | Required for scoreable `patch_source: generated` receipts (#507). See below. |
| `producer_binding` | object | `{work_session_id, owned_delta_sha256, subject_clean_at_start, start_git}` for patch-backed runs |
| `verifier_identity` | object | `{verifier_id, session_id}` independent verifier session |
| `patch_binding` | object | Patch-backed tuple plus `subject_path` and `subject_hash` |
| `fixture_binding` | object | `{manifest_id, case_id, check_id}` for fixture evaluation runs |

**`generated_patch_quarantine` object** (additive, `patch_source: generated` only; #507)

| Field | Type | Notes |
| --- | --- | --- |
| `schema` | string | `brigade.generated_patch_quarantine.v1` |
| `schema_version` | integer | `1` |
| `status` | string | Always `quarantined` on the proposal envelope; independent verify + effectiveness checks lift it for scoring |
| `candidate_count` | integer | ≥ 1 sampled candidates from the producing model |
| `model` | string | Model id that produced the candidates |
| `model_version` | string | Model version / revision string |
| `model_confidence`, `lexical_similarity`, `textual_similarity`, `repeated_sampling`, … | number \| integer | Optional audit-only fields. Explicitly non-promoting; ignored for eligibility and `signal_value` |

Generated patches fail closed without complete quarantine metadata
(`generated_patch_quarantine_incomplete`), without an independent verifier
session (`verifier_not_independent`), or without at least one effectiveness
check (`generated_patch_missing_repository_tests`). See
[`docs/design/generated-patch-quarantine.md`](design/generated-patch-quarantine.md).

Ad hoc `--command` / `--argv-json` runs omit `subject_binding` and remain audit-only (non-scoreable).

Tracked workspace verifier manifests live under `verify/manifests/*.json`. A manifest owns its
subject, ordered checks, required utility ids, optional scoped-write globs, and optional route
opt-in (`route_paths` or exact `route_classes`). Untracked manifests cannot produce scoreable
receipts or routing authority.

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

## `brigade.verify_archive_index.v1`: `schema_version: 1`

**Path:** `<verify-archive-root>/index.jsonl` (one JSON object per line, append-only,
sorted keys per line). The default archive root is `.brigade/work/verify-archive`;
`.brigade/config.json` keys `verify_archive_enabled` and `verify_archive_dir` override it.

Retention prunes the local `.brigade/work/verify-runs/` directory down to the newest
`verify_runs_keep` runs (default 50). Before any run directory is deleted it is copied
into the archive root as `<verify-archive-root>/<run-id>/` and one index line is
appended. A run directory whose archival fails is kept locally, so pruning never
destroys receipt evidence that was not preserved first. Archival re-checks integrity
both ways: the archived `receipt.json` bytes must hash to the source bytes, and a
receipt carrying `digests.receipt_sha256` must still re-hash to that value after the
copy. The archive root must not overlap the local verify-runs root in either direction,
including through a symlink alias. Source trees containing symlinks or special files
are kept locally. An existing archive destination is reused only when it is a regular
directory with the same files and file hashes as the source.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `schema` | string | yes | Always `brigade.verify_archive_index.v1` |
| `schema_version` | integer | yes | Always `1` for this contract |
| `run_id` | string | yes | Run directory name that was archived |
| `archived_at` | string (ISO-8601) | yes | When the archival completed |
| `source_run_dir` | string | yes | Original run directory path |
| `archive_run_dir` | string | yes | Archived copy path |
| `already_archived` | boolean | yes | `true` when an identical archive already existed |
| `receipt_file_sha256` | string \| null | yes | SHA-256 of the archived `receipt.json` bytes; `null` when the run dir had no receipt |
| `receipt_schema_version` | integer \| null | yes | The receipt's own `schema_version`; `null` for legacy receipts without one |
| `receipt_sha256` | string \| null | yes | The receipt's self-declared canonical digest; `null` when absent |
| `signature` | string \| null | yes | Receipt signature when the run was signed; `null` otherwise |
| `key_id` | string \| null | yes | Signing key id paired with `signature`; `null` otherwise |
| `status` | string \| null | yes | Receipt status at archival time |
| `started_at` | string \| null | yes | Receipt start timestamp |
| `completed_at` | string \| null | yes | Receipt completion timestamp |

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

## Work plan receipt (`.brigade/work/plans/<task-id>.json`)

Task plan artifacts written by `brigade work task plan --write`. This family is
documentation-only (no `schema` / `schema_version` stamp today). Evolution is
additive: consumers must ignore unknown keys. Readers must accept legacy
receipts that omit optional fields.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `task_id` | string | yes | Canonical task id |
| `kind` | string | yes | `plan` or `meta` |
| `title` | string | yes | |
| `status` | string | yes | `draft` or `accepted` |
| `created_at`, `updated_at` | string | yes | ISO-8601 |
| `source_context` | array of string | yes | |
| `assumptions` | array of string | yes | |
| `acceptance` | array of string | yes | |
| `risks` | array of string | yes | |
| `steps` | array of string | yes | |
| `decisions` | array of object | no | Optional decision checkpoints (#496). **Absent or omitted is valid** (legacy). When present must be a list; non-list values and malformed entries fail closed |
| `next_command` | string | yes | Suggested next safe command |
| `receipt_paths` | array of string | yes | Related relative paths |
| `research_runs` | array of object | yes | Quarantined research attachments |

### `decisions[]` entry

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `id` | string | yes | `[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}` |
| `prompt` | string | yes | Checkpoint question |
| `options` | array of string | yes | Non-empty allowed selections (each entry a non-empty string) |
| `selected` | string \| null | yes | Chosen option when resolved |
| `rationale` | string \| null | yes | Why that option |
| `evidence_ref` | string \| null | yes | Opaque receipt path or external evidence id. Stored as written; **not** validated as a local file path (external/opaque ids are allowed) |
| `status` | string | yes | `pending` or `resolved` |
| `created_at` | string | yes | ISO-8601 |
| `resolved_at` | string \| null | yes | ISO-8601 when resolved |

A checkpoint is resolved only when `selected`, `rationale`, and `evidence_ref`
are all non-empty and `selected` is one of the non-empty `options`. Every
decision entry must include a present, non-empty `options` array of non-empty
strings; `options: null`, `options: []`, or missing `options` fail closed.
Unresolved checkpoints block plan `--accept`, `work task claim`, `work claim`,
and `work task done`. Corrupt `decisions` data (non-list, non-object entry,
invalid/missing id, duplicate id, wrong field types) must fail closed with exit code 2 /
`malformed_plan_decisions` rather than being silently dropped.

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
| `skill_route_policy` | object | no | Frozen pre-plan score inputs, assignments, quota counters, and acceptance reasons |
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
| `lifecycle_journal_requested` | boolean | no | Durable enrollment request. Present and `true` on every new run |
| `run_journal_authority_requested` | boolean | no | Durable authority request. Present and `true` on every new run |
| `projector_version` | integer | no | Journal projector version used for the compatibility snapshot |
| `journal_present` | boolean | no | Whether the verified lifecycle journal exists |
| `journal_last_sequence` | integer | no | Last event sequence applied to this snapshot |
| `journal_last_event_digest` | string / null | no | Digest at `journal_last_sequence`. Null only at sequence zero |
| `approval_reference` | object | no | Redacted approval identity, source, fingerprints, and decision state |
| `approval` | object | no | Latest signed human decision: `{decision, scope, approver_principal, decided_at, expires_at, reason, nonce, statement_sha256, sod}`. The reason preimage is local-only |
| `run_budget` | object | no | Optional projected `brigade.run_budget.v1` summary when a coordinator wrote one (#593). Journal events remain authoritative. |

### Signed human approval (`brigade.run_event.v1` event type)

An `approval` event records a separately signed human decision without changing
the run lifecycle status. Its payload uses this closed key set. Legacy v1
approval events may omit the two fields marked below:

| Field | Type | Notes |
| --- | --- | --- |
| `decision` | string | `allow`, `deny`, or `hold` |
| `scope` | string | `run` or `merge` |
| `approver_principal` | string | Principal verified through the target's OpenSSH `allowed_signers` policy |
| `approver_keyid` | string | SHA-256 fingerprint of the approver's public key |
| `subject_tree` | string | Final `tree_fingerprint` covered by the statement |
| `nonce` | string | Random 32-character hexadecimal value |
| `decided_at` | string | v2 signed decision time, equal to the event envelope time. Omitted by legacy v1 approvals |
| `expires_at` | string | RFC 3339 UTC expiry |
| `statement_sha256` | string | SHA-256 digest of the canonical in-toto Statement bytes |
| `attestation_path` | string | Run-relative `approvals/<nonce>.json` envelope path |
| `producer_keyids` | array of string | Verified SSHSIG Test Result signer key ids bound by v2. May be omitted by legacy v1 approvals |

The projected `approval.sod` object has a `result` of `PASSED`, `FAILED`, or
v2-only `INDETERMINATE` and a `checks` array. Each check contains its policy
`id` and a `status` of `passed`, `failed`, or v2-only `indeterminate`. The
journal event and signed envelope remain the evidence.
`run.json` is the latest compatibility projection.

### Run budget lifecycle (`brigade.run_event.v1` event types, #593)

Append-only facts under `events/lifecycle.jsonl`. Status-neutral in the run
projector; the coordinator applies terminal policy via `run.failed` /
`run.interrupted` (or receipt termination) using run-owned kinds
`budget-exhausted` and `operator-cancelled`. These are **not** #576 worker
`FailureClass` values and are **not** infrastructure-neutral under #580.

| Event type | Payload keys | Notes |
| --- | --- | --- |
| `run_budget.threshold_reached` | `dimension`, `mode`, `declared`, `used`, `remaining`, `threshold_pct`, `reason_class` | Idempotent per dimension+threshold |
| `run_budget.reservation_denied` | `dimension`, `mode`, `declared`, `used`, `remaining`, `request_id`, `reason_class` | Emitted before new work starts |
| `run_budget.exhausted` | `dimension`, `mode`, `declared`, `used`, `remaining`, `reason_class` | Enforceable ceiling reached |
| `run_budget.cancel_requested` | `request_id`, `reason_class`, `transport_capability`, `dimension` | Best-effort cancel request |
| `run_budget.cancelled` | `request_id`, `reason_class`, `transport_capability`, `transport_result`, `active_remaining`, `active_seats`, `outcomes`, `dimension` | Cancel receipt with bounded per-seat or per-transport outcomes and observed work that may remain active |
| `run_budget.usage_reconciled` | `dimension`, `mode`, `usage_source`, `estimated_used`, `provider_used`, `used`, `request_id`, `reason_class` | Estimated and provider values stay distinct |

Enforceable dimensions: `wall_clock_seconds`, `worker_dispatch_count`.
Observed dimensions (never universal hard gates in this slice):
`model_call_count`, `tool_call_count`, `input_tokens`, `output_tokens`,
`estimated_cost_micros`, `provider_usage_units`. Legacy aggregate
`token_budget` (paired with receipt `tokens_used`) is preserved on the
declaration and is not an `input_tokens` alias. Child allocation is #594.
Unknown schema versions or dimensions fail closed with a bounded diagnostic.
Payloads never carry raw diagnostics.

**Declared-only hard ceilings.** Only runs that carry a persisted
`run_budget` declaration or a `verification_contract.budget` with enforceable
fields receive hard wall-clock / worker-dispatch ceilings. Ordinary
`brigade run`, dogfood, and model-trial paths that supply neither declaration
remain unbounded for backward compatibility: Brigade does not invent CLI
flags, default numeric ceilings, or a dogfood-timeout→budget mapping on those
entry paths. Agent process timeouts (`timeout_seconds`) stay separate from
run-budget enforcement.

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

**Lifecycle:** journal authority is the default for every new run.
`events/lifecycle.jsonl` is the append-only lifecycle record and `run.json` is its
latest `brigade.run.v1` compatibility projection. Existing run directories that
lack both durable request fields remain snapshot-only and are not migrated in
place. Readers must ignore additive keys. A paused approval projects `status:
running` so a previous version still sees a known nonterminal status.

Do not create a journal or add durable request fields to a legacy receipt by
hand. Journal-aware writers verify the bounded chain and checkpoint before
replacing the snapshot. An older release may inspect the additive `run.json`
shape, but operators must roll forward before it writes, recovers, or resumes a
journal-authoritative run. A retry after an approval action redeemed its claim
but exited before outcome persistence verifies the same run and fingerprints.
Daily approvals also bind the redeemed claim to the exact completed Daily run
receipt. A missing or changed completion receipt is not recoverable by retry.
The source-store lock spans validation, any missing `approval.consumed` and
`run.resumed` facts, and the refreshed snapshot. Review writes wait until that
transaction finishes. Reconciliation does not execute the action again.
Sidecars (`roster.json`, `plan.json`, `worker-results.json`, `synthesis.json`)
are write-once per phase (resume salvage and patch-ref binding may rewrite
worker/synthesis artifacts).

---

## `brigade.route-decision.v1`

**Path:** `.brigade/runs/<run-id>/route-decision.json`

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `schema_version` | string | yes | `brigade.route-decision.v1` |
| `chosen_route` | array of string \| null | yes | Route stages selected for the run |
| `confidence`, `template_version` | string \| null | yes | Route metadata |
| `admissible_seats` | array of string | yes | Non-orchestrator seats |
| `decided_at` | string | no | Pre-plan policy timestamp |
| `policy_version` | string | no | Skill route-policy version |
| `score_inputs` | object | no | Receipt-only score inputs keyed by artifact id |
| `skill_assignments` | array of object | no | Band, authority, manifest, scope, and exploration selection |
| `exploration` | object | no | Route class, 7/30-day counters, quota, and accept/reject reasons |

When skill routing applies, this receipt preserves the decision made before planning. Finalization
must not recompute it from post-run state.

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
| `selected_skill_ids` | array of string | Optional pre-plan exploratory skill binding |
| `domain` | string | Optional CandidateSetGate domain requirement |
| `capabilities` | array of string | Optional required tool capability labels |
| `max_risk_class` | string | Optional risk ceiling (`read`, `local-write`, `network`, `privileged`) |
| `admissible_tool_ids` | array of string | Gate output: tool ids admitted for this step |

---

## `brigade.candidate-set.v1`

**Path:** `.brigade/runs/<run-id>/candidate-set.json`

Written after planning and before dispatch. Filters `.brigade/tools.toml` by each
assignment's declared `domain`, `capabilities`, and `max_risk_class`. When an
assignment declares any of those requirements and no tool is admissible, the run
records a typed `no-admissible-tool` planning failure (with one bounded replan)
instead of letting the worker improvise tools.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `schema` | string | yes | `brigade.candidate-set.v1` |
| `schema_version` | integer | yes | `1` |
| `gate_version` | string | yes | `candidate-set-gate.v1` |
| `run_id` | string | yes | Same id as the run directory |
| `decided_at` | string | yes | ISO-8601 UTC |
| `tool_count` | integer | yes | Catalog entries considered |
| `catalog_errors` | array of string | no | Loader errors when present |
| `steps` | array of object | yes | Per-assignment gate results |
| `empty_required_steps` | array of object | no | Present only when enforcement found an empty set |

**Step object**

| Field | Type | Notes |
| --- | --- | --- |
| `stage`, `worker`, `task` | — | Assignment identity |
| `requirements` | object | Declared `domain` / `capabilities` / `max_risk_class` (omit empty) |
| `enforcement` | boolean | True when any requirement was declared |
| `admissible` | array of scored tool | Sorted by score desc, then tool id |
| `rejected` | array of scored tool | Rejected catalog entries with reasons |
| `empty` | boolean | True when `admissible` is empty |

**Scored tool object**

| Field | Type | Notes |
| --- | --- | --- |
| `tool_id` | string | Catalog id |
| `score` | number | Deterministic match score |
| `domain` | string \| null | Tool domain label |
| `capability` | array of string | Tool capability labels |
| `risk_class` | string \| null | Tool risk class |
| `reasons` | array of string | Match / reject reasons |

`brigade skills audit <run>` uses `selected_skill_ids` plus each skill's
additive `skill.json` `obligations` to report missing check / review / handoff
receipts as advisory findings (`brigade.skill_obligations_audit.v1`). Per-run
matching requires exact `producer_run_id` equality with the audited
orchestrator run id; timestamp proximity never satisfies. Legacy receipts
without `producer_run_id` are reported as unattributed and cannot satisfy.
Workers receive the orchestrator id as `BRIGADE_RUN_ID` through the run
transport env path. See `docs/skill-registry.md`.

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
| `reused_evidence_ref` | string | no | Reused verify receipt path when `evidence_ref` was canonicalized through `reused_from` (#650) |

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

## `brigade.work-run`: `schema_version: 1`

Portable multi-file archive for one Brigade run directory. The archive root is a
directory containing `work-run.json` (this envelope) and a `payload/` tree with
the exported run files. Published JSON Schema artifact:
[`schemas/work-run.v1.schema.json`](../schemas/work-run.v1.schema.json).

Following the Agent Client Protocol pattern, the schema **artifact** version
(the `work-run.v1` filename / `$id`) is separate from archive **wire**
compatibility (`schema` + `schema_version` on `work-run.json`). Consumers must
not infer import acceptance from the artifact filename alone.

**Commands:** `brigade runs export`, `brigade runs import`,
`brigade runs validate-archive`.

### Layout

```text
<archive>/
  work-run.json
  payload/
    run.json
    roster.json
    ...
    events/lifecycle.jsonl
    events/<worker>.scrubbed.json            # scrubbed worker streams (#592)
    events/**/<worker>.scrubbed.json         # nested worker streams also scrubbed
    events/recovery-checkpoints/<sha256>.json   # artifact references only
```

### Manifest fields

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `schema` | string | yes | Always `brigade.work-run` |
| `schema_version` | integer | yes | `1` |
| `run_id` | string | yes | `YYYYMMDD-HHMMSS-<8 hex>` |
| `exported_at` | string | yes | ISO-8601 UTC (`Z`) |
| `exporter_brigade_version` | string | yes | Brigade version that wrote the archive |
| `format` | string | yes | Always `directory` |
| `payload_dir` | string | yes | Always `payload` |
| `compatibility` | object | yes | Closed import/export rules (below) |
| `files` | array | yes | Declared payload entries with digests |
| `run` | object | no | Compact summary projected from `run.json` |
| `source_run_dir` | string | no | Absolute source path at export time |
| `schema_artifact` | string | no | Repo-relative published schema path |

**`files[]` entry**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `path` | string | yes | Relative path under `payload/` |
| `sha256` | string | yes | Lowercase hex digest of file bytes |
| `byte_size` | integer | yes | Byte length |
| `role` | string | yes | `receipt`, `journal`, `checkpoint-reference`, `artifact`, `support`, or `other` |
| `media_type` | string | no | Best-effort media type |
| `nested_schema` | string | no | Nested document `schema` when known |
| `nested_schema_version` | integer | no | Nested document `schema_version` when known |
| `privacy_class` | string | no | `public`, `private`, or `redacted` |

### Compatibility rules (import / export)

| Rule | v1 behavior |
| --- | --- |
| Unsupported `schema` / `schema_version` | **Refuse** hard |
| Unknown manifest keys | **Refuse** (closed envelope) |
| Nested receipt unknown keys | **Ignore** (receipt-family additive evolution) |
| Symlinks / special files | **Refuse** |
| Recovery-checkpoint bodies | **Strip** to closed artifact references on export (#636); validate refuses bodies |
| Raw worker event streams (`events/**/*.jsonl` except authenticated lifecycle) | **Refuse** as portable public support; export replaces with scrubbed sidecars (`events/**/<worker>.scrubbed.json`, `privacy_class=redacted`) or fails closed (#592); nested smuggling paths are rejected |
| Reserved `events/lifecycle.jsonl` | **Authenticate** as `brigade.run_event.v1` chain (or empty); reject raw JSON-RPC worker envelopes disguised as the journal (#592) |
| Resume after import | **Not supported** in v1 (`resume_supported: false`); import is inspection/audit oriented |
| Digest mismatch | **Refuse** |

`compatibility` object fields are closed for v1: reader window pinned to
`schema_version` 1, `private_checkpoint_bodies` =
`strip_to_artifact_reference`, `unsupported_archive_version` = `refuse`,
`nested_receipt_unknown_keys` = `ignore`, `symlinks_and_special_files` =
`refuse`, and `journal_authority` one of `none` / `present` / `authoritative`.

---

## `brigade.causal_receipt.v1`: `schema_version: 1`

Lineage-only companion for plan, run, verify, outcome, handoff, and synthesis
artifacts (issue #493). It is not a second provenance, trust, or privacy
envelope. Artifacts carry the companion under the additive `causal_receipt`
field; run handoffs also write a sibling `*.causal-receipt.json`. Historical
artifacts without the field remain readable and are not rewritten.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `schema` | string | yes | Always `brigade.causal_receipt.v1` |
| `schema_version` | integer | yes | `1` |
| `subject` | object | yes | `{kind, id}` stable subject reference |
| `parents` | array | yes | Typed parent links. Empty for a root. Max 16 |
| `parent_manifest` | object | no | `{id, digest}` when fan-in exceeds the parent or size cap. Inline `parents` must then be empty. The hashed document is written beside `synthesis.json` as `<id>.json` |

**Parent object**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `relation` | string | yes | Closed: `planned_from`, `executed_from`, `verified_from`, `captured_from`, `handed_off_from`, `synthesized_from` |
| `kind` | string | yes | Closed: `plan`, `run`, `worker-result`, `synthesis`, `verify`, `outcome`, `handoff` |
| `id` | string | yes | Safe identity label. Never an absolute path |
| `digest` | string | no | Bare lowercase SHA-256 hex of the parent companion record |
| `link` | string | yes | `recorded` on new writes; `inferred` is reserved for #583 backfill |

Compact JSON is capped at 2048 bytes. Unknown relations, malformed digests,
broken parents, digest mismatches, and unsupported versions produce bounded
diagnostics. Companions store references and digests only — never prompts,
model output, tool arguments, retrieved content, stack traces, credentials,
or home paths.

Typical recorded chain: plan → run (`planned_from`) → verify (`executed_from`)
→ outcome (`captured_from`) → handoff (`handed_off_from`). Synthesis may list
multiple `synthesized_from` worker-result parents. When fan-in exceeds the
inline bounds, those parents live in `brigade.causal_parent_manifest.v1`
(`<run-id>-parent-manifest.json`); `walk_ancestors` resolves them through
that sibling artifact.

### `brigade.causal_parent_manifest.v1`: `schema_version: 1`

**Path:** `.brigade/runs/<run-id>/<run-id>-parent-manifest.json`

Lineage-only hashed parent list referenced by `causal_receipt.parent_manifest`.
It is not a second provenance envelope. Compact JSON is capped at 65536 bytes.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `schema` | string | yes | Always `brigade.causal_parent_manifest.v1` |
| `schema_version` | integer | yes | `1` |
| `id` | string | yes | Safe identity label. Matches `parent_manifest.id` |
| `parents` | array | yes | Same parent-object schema as the causal receipt |

## Run View read contracts (#631)

Versioned, read-only CLI JSON contracts for higher-level surfaces (the Center
Runs view). Each payload is an explicit allowlist over run artifacts: it never
includes environment values, auth or control tokens, secret references, full
prompts, transcript bodies, raw stdout/stderr, log paths, or absolute
workspace paths. Every string that enters a list, detail, or watch JSON
contract passes through one `_clean_str` chokepoint: the value is bounded,
then home-directory prefixes are rewritten to `~` as the last transform.
That rewrite is universal — not limited to `failure.detail` or
`commands[].command`. Matched prefixes are POSIX `/home/<user>` and
`/Users/<user>` (including a doubled slash such as `/home//<user>`, and
usernames that contain a space or an apostrophe) and Windows
`C:\Users\<name>\` plus `C:/Users/<name>/`. The authoritative artifacts
and human CLI output are never modified.

`brigade runs serve` exposes the same three contracts on loopback as
`GET /api/runs`, `GET /api/runs/<run-id>`, and `GET /api/runs/<run-id>/events`
(SSE of watch records). It does not add a second artifact reader.

### `brigade.runs-list.v1` — `brigade runs list --json`

```json
{"schema": "brigade.runs-list.v1", "runs": [], "skipped_invalid": 0}
```

Each entry in `runs` carries browser-safe summary fields only: `run_id` (the
run directory name, never an absolute path), `status`, bounded `task`,
`started_at`, `finished_at`, `duration_seconds`, `failure_phase`, `mode`
(`normal` / `read-only`, with `, dry-run` appended), `resume_available`, and
optional `parent_run_id` when the run is a durable child of another run.
Invalid run directories are counted in `skipped_invalid` and skipped.
That includes a child that is a symlink and a directory whose resolved
path leaves the runs root, so list JSON cannot enumerate an alternate tree.

### `brigade.run-detail.v1` — `brigade runs show <run> --json`, `brigade runs latest --json`

Both commands share one serializer:

```json
{
  "schema": "brigade.run-detail.v1",
  "run": {},
  "roster": {},
  "plan": {},
  "workers": {},
  "synthesis": {},
  "verification": [],
  "briefs": []
}
```

- `run`: identifier, status, bounded task, mode, timestamps, duration,
  `failure` (`phase` / `kind` / bounded `detail`), bounded `error`,
  `suspected_noop`, `resume_available`, and optional `lineage`
  (`kind`, `parent_run_id`, `branch_point_event_id`, `shared_prefix`,
  and `children` discovered from sibling receipts the same way as
  human `runs show`) when the run is a durable parent or child.
  Child `status` and `branch_point_event_id` are bounded.
- `roster`: orchestrator, worker limits, allowed models, and per-seat `cli`,
  `model`, `reasoning`, bounded `role`, and `timeout_seconds`.
- `plan`: worker `assignments` with stage, worker, and bounded task.
- `workers`: per-result state (ok/status, bounded detail, duration, exit code,
  timeout flag, requested model, transport, failure class).
- `verification`: verify receipt summaries (receipt run id, status, duration,
  `command`, and exit code) from recorded ground truth.
- `briefs`: attachment markers for the Code Intelligence (`code-graph`),
  drift (`drift-impact`), and Evidence Ledger (`evidence`) briefs.

### `brigade.run-watch.v1` — `brigade runs watch <run> --json`

Every newline-delimited watch record (`watch`, `run`, `plan`, `event`,
`workers`, `synthesis`, `final`, `summary`) carries
`"schema": "brigade.run-watch.v1"` and is filtered through the same
allowlist / one-line helpers as list and detail. Consumers must ignore
unknown future record types. `brigade runs events` keeps its own
`brigade.run_event.v1` lifecycle contract and is unchanged.

- `watch` and `summary` identify the run as `run_id` (directory name,
  never an absolute path). The stale-lock `inspect_command` is
  `brigade runs show <run_id>`.
- `run`, `plan`, `workers`, and `synthesis` reuse the detail-contract
  field allowlists and bounded strings.
- `event` records emit only `method` and `item_type`. Auth tokens,
  prompts, transcript bodies, raw stdout/stderr, log paths, and
  absolute paths in `params` are omitted.
- `final` text is one-lined and bounded through the same `_clean_str`
  chokepoint; a value that cannot be rendered safely is omitted.

### `brigade.run-diff.v1` — `brigade runs diff <child> [other] --json`

Read-only comparison of a durable child against its recorded parent
(one-argument form) or two sibling runs that share a parent (explicit
two-run form). The command never writes to either run directory.
Unknown, corrupt, or parentless run IDs fail closed with no JSON.

```json
{
  "schema": "brigade.run-diff.v1",
  "relation": "child-parent",
  "left": {},
  "right": {},
  "parent_run_id": "",
  "branch_point_event_id": "",
  "lifecycle": {},
  "workers": {},
  "verification": {},
  "outcome": {},
  "graphtrail": {}
}
```

- `relation`: `child-parent` or `siblings`. Left is the child (or first
  sibling); right is the parent (or second sibling).
- `lifecycle`, `workers`, `verification`, and `outcome` each carry
  `changed`, optional `changes` (`field` / `left` / `right`), and the
  compared left/right views. Compared strings use the same `_clean_str`
  chokepoint as list/show/latest/watch. Verification `changed` is the
  ordered `{status, command, exit_code}` sequence across every receipt
  and command; `run_id` and the `final.txt` digest are display-only.
- `graphtrail` compares GraphTrail snapshot attestations only when both
  sides have compatible snapshots (`ok`/`status=ok` plus a before or
  after snapshot sha256). Absent or incompatible snapshots set
  `status: skipped` and a `reason` (`absent snapshots` or
  `incompatible snapshots`) instead of guessing.

---

## `brigade.attestation.sshsig-dsse.v1`

**Path:** `<run-dir>/attestation.json` (exported via `brigade receipts export attestation`)

An additive envelope profile packaging a verify receipt (`brigade.work_verify_receipt`) as a detached, portable in-toto Statement v1 inside a DSSE (Dead Simple Signing Envelope) v1 structure signed with OpenSSH (`ssh-keygen -Y sign`).

### DSSE Envelope

```json
{
  "payloadType": "application/vnd.in-toto+json",
  "payload": "<base64 canonical statement bytes>",
  "signatures": [
    {
      "keyid": "<sha256 fingerprint of the signer public key, as ssh-keygen -lf prints it>",
      "sig": "<base64 of the full SSHSIG armored block>"
    }
  ],
  "brigade": {
    "profile": "brigade.sshsig-dsse.v1",
    "namespace": "brigade-attestation"
  }
}
```

- `payloadType`: standard in-toto JSON MIME type `application/vnd.in-toto+json`.
- `payload`: base64-encoded UTF-8 bytes of the canonical in-toto Statement (`sort_keys=True`, `separators=(",", ":")`).
- `signatures`: array of signer entries. `keyid` is the `SHA256:...` fingerprint of the public key (from `ssh-keygen -lf`). `sig` is the base64 encoding of the full ASCII-armored OpenSSH signature block (`-----BEGIN SSH SIGNATURE-----...-----END SSH SIGNATURE-----`).
- `brigade`: marker object identifying the envelope profile (`brigade.sshsig-dsse.v1`) and the OpenSSH signature namespace (`brigade-attestation`). This indicates the signature is an SSHSIG armored block rather than a raw signature algorithm output.

### Pre-Authentication Encoding (PAE)

The bytes signed by `ssh-keygen -Y sign -n brigade-attestation` follow standard DSSE v1 Pre-Authentication Encoding:

```text
"DSSEv1" SP LEN(payloadType) SP payloadType SP LEN(payload) SP payload
```

where `payload` is the raw canonical statement bytes (not base64) and `LEN(x)` is the base-10 ASCII byte length of `x`.

### Statement Shape

The decoded payload is an in-toto Statement v1 with the Test Result v0.1 predicate:

```json
{
  "_type": "https://in-toto.io/Statement/v1",
  "subject": [
    {"name": "git:tree", "digest": {"gitTree": "<tree_fingerprint>"}},
    {"name": "changes.patch", "digest": {"sha256": "<changes_patch_sha256>"}}
  ],
  "predicateType": "https://in-toto.io/attestation/test-result/v0.1",
  "predicate": {
    "result": "PASSED | FAILED",
    "configuration": [
      {
        "name": "receipt.json",
        "uri": "urn:brigade:verify:<run_id>:receipt",
        "digest": {"sha256": "<digests.receipt_sha256>"},
        "mediaType": "application/json",
        "annotations": {
          "brigade": {
            "run_id": "<run_id>",
            "baseline_commit": "<baseline_commit>",
            "producer_run_id": "<producer_run_id>"
          }
        }
      }
    ],
    "url": "urn:brigade:verify:<run_id>",
    "passedTests": ["<check_id or command>", "..."],
    "warnedTests": [],
    "failedTests": ["..."]
  }
}
```

- `subject`: records the exact Git tree identity (`gitTree`) and `changes.patch` SHA-256 digest. Never includes target workspace paths, absolute paths, environment variables, or raw logs. If either `tree_fingerprint` or `changes_patch_sha256` is missing, export is refused.
- `predicate.result`: `PASSED` only when receipt `status == "completed"` and every command has `exit_code == 0`. Otherwise `FAILED`. Commands with null or nonzero exit codes are recorded in `failedTests`.
- `predicate.configuration`: references the verify receipt digest with annotations carrying `run_id`, `baseline_commit`, and `producer_run_id` when present.

### Human approval predicates

#### `human-approval/v1` compatibility profile

The same envelope profile also carries `https://brigade.dev/attestation/human-approval/v1`
statements written under `<run-dir>/approvals/<nonce>.json`. The subject list
binds the decision to the run's final tree and every verify receipt whose
`producer_run_id` matches the run:

```json
{
  "_type": "https://in-toto.io/Statement/v1",
  "subject": [
    {"name": "git:tree", "digest": {"gitTree": "<tree_fingerprint>"}},
    {"name": "verify:<verify-run-id>", "digest": {"sha256": "<receipt_sha256>"}}
  ],
  "predicateType": "https://brigade.dev/attestation/human-approval/v1",
  "predicate": {
    "schemaVersion": 1,
    "run": {"id": "<run-id>", "journalChainHead": {"sha256": "<event digest before approval>"}},
    "decision": "allow | deny | hold",
    "scope": "run | merge",
    "approver": {"principal": "<allowed_signers principal>", "keyid": "SHA256:...", "kind": "human"},
    "decidedAt": "<RFC 3339 UTC>",
    "expiresAt": "<RFC 3339 UTC>",
    "nonce": "<32 hex characters>",
    "reasonCode": "<bounded reason code>",
    "reason": "<plain text, at most 500 characters>",
    "policy": {"name": "brigade.sod.v1", "digest": {"sha256": "<policy text digest>"}}
  }
}
```

An `allow` statement requires at least one verify-receipt subject. `deny` and
`hold` may have none. Verification recomputes all subjects and the
`brigade.sod.v1` checks from local evidence. It reports `APPROVED`, `DENIED`,
`HELD`, `UNAPPROVED`, `APPROVAL-INVALID`, `APPROVAL-STALE`, `SOD-VIOLATION`,
or `APPROVAL-EXPIRED` for each run. `APPROVAL-STALE` means a valid approval no
longer describes the live Git tree, or a signed verify-receipt subject is now
missing. It does not change the command exit status. The signed receipt
subjects need only be a subset of current evidence, so later receipts do not
invalidate an approval. JSON output includes `live_tree` (`unavailable` when
the tree cannot be computed) and earlier superseded decisions.

Every approval event remains in the journal. Verification uses the latest one
and reports prior decisions with their principal and recorded time. The SoD
checks include `approver-key-not-workspace-key`: the approver key must not be
the workspace's default attestation key. The common v1/v2 verifier enforces
`approval-before-merge-ship` from journal sequence. An approval event must
precede every merge or ship event even if its signed `decidedAt` was backdated.
Projector version 7 makes existing `run.json` snapshots stale input for
`run_shadow`; regenerate the shadow after the projection is refreshed.

Approval v1 remains verification-only compatibility. Verification reports
`binding: receipt` because its subjects contain verify receipt digests. That
binding is not portable Test Result evidence and cannot satisfy a future
agent-change evidence index.

#### `human-approval/v2` portable evidence profile

New decisions use
`https://brigade.dev/attestation/human-approval/v2` with policy
`brigade.sod.v2`. An `allow` decision is written only after every local verify
receipt attributed to the run by exact `producer_run_id` and naming the
approved final Git tree has all of the following:

- complete baseline commit, final Git tree, and `changes.patch` identity;
- a retained `changes.patch` whose SHA-256 matches the receipt;
- a canonical SSHSIG Test Result envelope at `attestation.json` that verifies
  as `SIGNED-OK`, re-derives exactly from the local receipt, and has predicate
  result `PASSED`.

Receipts for the same producer run at older Git trees are outside the approved
Test Result set and are ignored. The final-tree receipts must agree on one
baseline commit and one patch digest. If attributed receipts exist only for
other trees, Brigade reports that no Test Result covers the run's final tree. A
missing, failed, untrusted, non-canonical, or inconsistent final-tree item
refuses the approval before its envelope or journal event is written. Deny and
hold decisions do not require Test Result evidence.

The v2 subject list contains the final `git:tree`, the agreed
`changes.patch` SHA-256, and one `test-result:<verify-run-id>` subject per
canonical Test Result payload. Test Result subjects are ordered by payload
SHA-256 and then verify run id. The predicate repeats the exact sorted payload
digest set and records, for every item, its verify run id, canonical envelope
SHA-256, envelope profile, verified SSHSIG `signerKeyid`, and
`producerKeyids`. The emitted `producerKeyids` value is the exact singleton
list containing `signerKeyid`; removable receipt HMAC metadata is not a
producer identity. Verification compares the signed set to the complete
current final-tree set, so a missing, changed, or later final-tree receipt
makes an allow approval stale. The predicate has a closed key set and records
only `reasonCode` and `reasonSha256`, not the approval reason text.
`reasonSha256` is SHA-256 over the ASCII nonce, one NUL byte, and the UTF-8
reason. The reason preimage remains only in the local `run.json` approval
projection. Verification recomputes the commitment from that projection and
reports a mismatch as `APPROVAL-STALE` without printing either reason. JSON
verification reports `binding: test-result`.

The approval journal event binds the signed `decidedAt` value as `decided_at`.
Verification requires the signed value, event payload value, and event envelope
time to agree. The common v1/v2 `approval-before-merge-ship` check uses journal
sequence, not timestamps: any merge or ship event before the approval event
fails the check, including when either event carries a backdated timestamp.

Before v2 uses requester identity, it re-verifies the referenced
`agent-request/v1` SSHSIG envelope against `allowed_signers` and the optional
KRL, checks its canonical statement digest against the paired
`request.signed` event, and binds both the request statement and envelope
digests into the approval. The mutable requester projection in `run.json` is
not an identity source. A missing signed request gives the v2 SoD result
`INDETERMINATE` and approval status `SOD-INDETERMINATE`. A matching requester
principal or key id gives `FAILED` / `SOD-VIOLATION`.

`brigade receipts verify` keeps its existing exit behavior. The opt-in
`--strict-approvals` gate also returns nonzero for stale, expired,
SoD-indeterminate, or unapproved runs. Invalid approvals and SoD violations
already return nonzero without strict mode. Deny and hold remain reported
decisions rather than strict verification failures.

### Trust Policy

Trust is evaluated offline via standard OpenSSH `allowed_signers` files:
- Default path: `.brigade/attestation/allowed_signers`.
- Key file: `.brigade/attestation/signing-key` (Ed25519, mode 0600).
- Key revocation list (optional): `.brigade/attestation/revoked_keys`.
- Allowed signers entry format: `<principal> namespaces="brigade-attestation" <key-type> <base64-key>`.
- Verification executes `ssh-keygen -Y verify -f <allowed_signers> -I <principal> -n brigade-attestation -s <sigfile>` over the recomputed DSSE PAE. Re-derivation of the in-toto Statement from the local verify receipt, and therefore the `SUBJECT-MISMATCH` check, only runs when `--target` is given and the run directory referenced by the receipt exists.
- `brigade receipts verify-attestation --require-receipt --target <workspace>` requires that local receipt. A missing receipt reports `EVIDENCE-MISSING`; without this opt-in, cross-machine signature-only verification remains valid.

### `brigade.attestation_verify_result.v1`

Machine-readable result printed by `brigade receipts verify-attestation --json`:

| Field | Type | Notes |
| --- | --- | --- |
| `schema` | string | Always `brigade.attestation_verify_result.v1` |
| `status` | string | One of `SIGNED-OK`, `SIGNATURE-MISMATCH`, `UNTRUSTED-KEY`, `UNVERIFIABLE-SIGNATURE`, `SUBJECT-MISMATCH`, `EVIDENCE-MISSING` |
| `principal` | string \| null | Verified signer principal when status is `SIGNED-OK` |
| `keyid` | string \| null | `SHA256:...` fingerprint of the signing key when known |
| `subject` | array of object | Reproduced in-toto Statement subjects |
| `run_id` | string \| null | Run id recovered from the statement `predicate.url` when it is safe |
| `rederived` | boolean | True only when a local Test Result statement was successfully re-derived from its referenced receipt |

### Agent request predicate

`brigade run --requester-key <path>` records an SSHSIG `agent-request/v1`
statement before dispatch and records a checkpoint-paired `request.signed`
journal event under the active run lock. The
statement binds the run id, baseline Git commit, SHA-256 task digest, request
time, and nonce. It does not include task text, workspace paths, argv,
environment values, or command output. Requester principal and key id are
projected into `run.json` for separation-of-duties evaluation.

Approval v1 keeps its current compatibility behavior and may use those
projected fields when present. A future approval v2 must not trust the
projection alone. Before using requester identity for segregation of duties,
it must load the referenced request envelope, verify its SSHSIG signature
against the target's `allowed_signers` policy and optional KRL, recompute the
canonical statement digest, and require that digest to match the paired
`request.signed` event. This requirement applies only to the SSHSIG request
profile. It does not route request verification through the separate cosign
profile.

---

## `brigade.attestation.cosign-dsse.v1`

**Path:** `<run-dir>/attestation.sigstore.json` (exported via `brigade receipts export attestation --profile cosign`)

An opt-in signer profile packaging a verify receipt (`brigade.work_verify_receipt`) as an unwrapped standardized Sigstore bundle returned by `cosign attest-blob`. The file is the unmodified standardized Sigstore bundle output returned by cosign. Its `dsseEnvelope` signs the existing in-toto Test Result Statement without additional wrapper objects or custom fields.

### Bundle Structure

- Media type: `application/vnd.dev.sigstore.bundle.v0.3+json`
- Default output file: `<run-dir>/attestation.sigstore.json`, keeping SSHSIG output (`attestation.json`) distinct.
- Envelope payload type: `application/vnd.in-toto+json`
- Scope: local cosign key files only. The default private key path is `.brigade/attestation/cosign.key`, overridable via `BRIGADE_COSIGN_KEY_FILE` or `--key`. When using password-encrypted keys (such as those generated by `cosign generate-key-pair`), supply the password via the `COSIGN_PASSWORD` environment variable. Keyless OIDC, Fulcio, Rekor transparency log upload, KMS, and TSA are excluded.
- Safe versions: cosign release versions 2.6.5 and newer within major 2 (invoked with `--new-bundle-format=true` and `--tlog-upload=false`) and 3.1.3 and newer within major 3 (invoked with a temporary offline `--signing-config`). Prereleases, distro-suffixed versions, other major versions, and vulnerable ranges from GHSA-fx35-mq7g-6g98 are rejected.

### External Verification

Brigade does not provide a cosign verification wrapper command. `brigade receipts verify-attestation` remains the SSHSIG verifier and does not verify Sigstore bundles.

External consumers verify the bundle directly with `cosign verify-blob-attestation`:

```bash
cosign verify-blob-attestation \
  --bundle <run-dir>/attestation.sigstore.json \
  --key .brigade/attestation/cosign.pub \
  --insecure-ignore-tlog=true \
  --type=https://in-toto.io/attestation/test-result/v0.1 \
  --digest=<git-tree> \
  --digestAlg=gitTree
```

Because Rekor transparency log upload is excluded from this profile, `--insecure-ignore-tlog=true` is required when verifying. Passing `--type=https://in-toto.io/attestation/test-result/v0.1` matches the in-toto Test Result predicate type, and `--digest=<git-tree>` with `--digestAlg=gitTree` enables claim checking against the in-toto Statement subject.

---

## Related commands

- `brigade receipts verify`: digest chain checks for verify receipts and outcome rows
- `brigade run approve`: record a signed human decision for a completed run
- `brigade receipts export attestation`: export verify receipt as SSH-signed (default) or cosign-signed in-toto attestation
- `brigade receipts verify-attestation`: offline verification of attestation against allowed_signers
- `brigade receipts attestation-keygen`: generate Ed25519 attestation signing key and allowed_signers entry
- `brigade receipts export miseledger`: adapter export (separate `miseledger.adapter.v1` envelope)
- `brigade runs export` / `import` / `validate-archive`: portable `brigade.work-run` archives
- `brigade outcome rebuild-status`: prove `status.json` matches decision receipts
