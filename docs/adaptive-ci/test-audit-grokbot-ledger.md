# Grokbot test audit ledger

Pinned base: origin/main `3510da921d64d1c0f5a531ec5eb8347a125e784d`. One row per test declaration (a `parametrize` is one declaration, its row count is shown). Marks follow the OpenClaw test-audit skill: **R** retain, **F** fix assertion or setup, **C** consolidate into a named owner, **D** delete with the remaining proof named.

## How to read the evidence column

- **read**: the test body was read in full against its production owner. Used for files tagged `read in full`, and for every changed row.
- **partial**: the file was read in part (the tests named in a row are read, the rest are tier 2).
- **cov**: not individually reviewed. Retained on a mechanical basis: the per-test coverage context run (`--cov-context=test`) shows how many grokbot source lines the test executes and how many of those no other grokbot test executes (`uniq`). This is a weak basis. 77 percent of the tests have `uniq=0`, so `uniq` was never used as a deletion criterion, only to rank reading order. A `cov` row is a retention default, not a finding that the test is good.

Every changed row names the mutation (`M..`, `R..`) that proves the keeper still fails when the production owner is broken, the mutation table is in `test-audit-grokbot.md`.

## L1 jobs queue (owner: grokbot_jobs, job_validation, job_clock, jobs_expiry, jobs_hub, jobs_claim_target, fleet_client_grokbot)

Files: `test_grokbot_jobs.py`. Declarations at base: 118.

### test_grokbot_jobs.py (118 declarations, read in full)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_grokbot_jobs_expiry_imports_before_grokbot_jobs` | 1 | R | read, contract named by the test, cov no context recorded (subprocess or class-scoped) |
| `test_enqueue_returns_opaque_handle_and_private_projection` | 1 | R | read, contract named by the test, cov 332 lines, uniq 0 |
| `test_storage_is_private_and_keeps_the_full_envelope` | 1 | R | read, contract named by the test, cov 232 lines, uniq 0 |
| `test_concurrent_scout_admission_creates_only_one_active_job` | 2 | R | read, contract named by the test, cov no context recorded (subprocess or class-scoped) |
| `test_status_returns_only_safe_projections` | 1 | R | read, contract named by the test, cov 336 lines, uniq 0 |
| `test_enqueue_is_idempotent_only_for_identical_canonical_task_content` | 1 | R | read, contract named by the test, cov 308 lines, uniq 0 |
| `test_enqueue_recovers_a_job_when_the_idempotency_write_failed` | 1 | R | read, contract named by the test, cov 316 lines, uniq 8 |
| `test_enqueue_rejects_invalid_envelope_classes` | 15 | R | read, contract named by the test, cov 76 lines, uniq 3 |
| `test_enqueue_recursively_rejects_sensitive_field_names` | 3 | R | read, contract named by the test, cov 30 lines, uniq 0 |
| `test_enqueue_rejects_invalid_idempotency_keys` | 3 | R | read, contract named by the test, cov 81 lines, uniq 0 |
| `test_enqueue_rejects_scout_artifact_mismatch` | 1 | R | read, contract named by the test, cov 74 lines, uniq 1 |
| `test_get_job_rejects_malformed_handles` | 2 | R | read, contract named by the test, cov 20 lines, uniq 0 |
| `test_storage_refuses_symlinked_queue_components` | 5 | R | read, contract named by the test, cov 135 lines, uniq 2 |
| `test_storage_refuses_symlinked_queue_files` | 2 | R | read, contract named by the test, cov 249 lines, uniq 0 |
| `test_concurrent_claims_have_one_winner` | 1 | R | read, contract named by the test, cov 232 lines, uniq 0 |
| `test_claim_is_idempotent_for_same_lease_and_rejects_another` | 1 | R | read, contract named by the test, cov 395 lines, uniq 0 |
| `test_claim_execution_context_is_exact_and_safe_claim_stays_redacted` | 1 | R | read, contract named by the test, cov 416 lines, uniq 0 |
| `test_claim_execution_context_rejects_conflicting_and_expired_leases` | 1 | R | read, contract named by the test, cov 404 lines, uniq 0 |
| `test_renew_clamps_to_deadline_and_increments_revision` | 1 | R | read, contract named by the test, cov 401 lines, uniq 0 |
| `test_claim_rejects_out_of_range_leases` | 2 | R | read, contract named by the test, cov 255 lines, uniq 0 |
| `test_valid_lifecycle_transition_and_draft_pr_artifact` | 1 | R | read, contract named by the test, cov 450 lines, uniq 0 |
| `test_completed_artifacts_are_exact_and_match_specification` | 8 | R | read, contract named by the test, cov 439 lines, uniq 3 |
| `test_branch_and_report_artifacts_complete_their_allowed_roles` | 1 | R | read, contract named by the test, cov 454 lines, uniq 0 |
| `test_failure_terminalizes_claimed_or_running_job` | 1 | R | read, contract named by the test, cov 431 lines, uniq 0 |
| `test_transitions_require_the_current_unexpired_lease` | 1 | R | read, contract named by the test, cov 417 lines, uniq 0 |
| `test_cancel_queued_and_cooperative_live_job` | 1 | R | read, contract named by the test, cov 433 lines, uniq 0 |
| `test_cancel_requested_claim_cannot_start` | 1 | R | read, contract named by the test, cov 423 lines, uniq 1 |
| `test_mutation_rejects_backward_clock` | 1 | R | read, contract named by the test, cov 407 lines, uniq 1 |
| `test_storage_os_errors_use_the_unsafe_storage_reason` | 1 | R | read, contract named by the test, cov 255 lines, uniq 0 |
| `test_posix_storage_write_stays_anchored_when_visible_directory_is_replaced` | 1 | R | read, contract named by the test, cov 78 lines, uniq 0 |
| `test_status_expires_a_job_past_its_timeout_without_an_operator_sweep` | 1 | R | read, contract named by the test, cov 357 lines, uniq 0 |
| `test_status_reads_each_job_file_once` | 1 | R | Call-count on a private reader, kept: it is the only proof of the single-traversal property. Candidate for a future behavioural replacement. |
| `test_status_leaves_a_job_inside_its_timeout_queued` | 1 | R | read, contract named by the test, cov 334 lines, uniq 0 |
| `test_status_expires_a_running_job_past_its_own_deadline` | 1 | R | read, contract named by the test, cov 429 lines, uniq 0 |
| `test_a_read_does_not_end_a_job_whose_lease_alone_lapsed` | 1 | R | read, contract named by the test, cov 421 lines, uniq 0 |
| `test_a_lease_call_past_the_execution_deadline_expires_and_says_job_expired` | 1 | R | read, contract named by the test, cov 429 lines, uniq 0 |
| `test_renew_after_the_execution_deadline_answers_job_expired` | 1 | R | read, contract named by the test, cov 429 lines, uniq 0 |
| `test_a_late_claim_gets_its_whole_execution_budget` | 1 | R | read, contract named by the test, cov 401 lines, uniq 0 |
| `test_an_unclaimed_job_expires_at_its_queue_ttl` | 1 | R | read, contract named by the test, cov 384 lines, uniq 0 |
| `test_an_envelope_may_declare_a_bounded_queue_ttl` | 1 | R | read, contract named by the test, cov 355 lines, uniq 4 |
| `test_expire_still_terminalizes_a_job_whose_lease_alone_lapsed` | 1 | R | read, contract named by the test, cov 401 lines, uniq 0 |
| `test_claim_past_the_deadline_expires_the_job_and_stays_job_expired` | 1 | R | read, contract named by the test, cov 385 lines, uniq 2 |
| `test_expiry_never_requeues_and_late_completion_is_rejected` | 1 | R | read, contract named by the test, cov 447 lines, uniq 0 |
| `test_corrupted_lifecycle_records_fail_closed` | 7 | R | read, contract named by the test, cov 450 lines, uniq 5 |
| `test_corrupted_idempotency_record_fails_closed` | 1 | R | read, contract named by the test, cov 261 lines, uniq 1 |
| `test_cli_queue_lifecycle_uses_generic_listener_identity_file` | 1 | R | read, contract named by the test, cov 38 lines, uniq 0 |
| `test_cli_queue_lifecycle_keeps_host_identity_without_generic_listener_file` | 1 | R | read, contract named by the test, cov 5 lines, uniq 0 |
| `test_cli_queue_lifecycle_rejects_unsafe_generic_listener_file_before_hub_call` | 4 | R | read, contract named by the test, cov 22 lines, uniq 0 |
| `test_cli_generic_listener_identity_does_not_override_hub_role_policy` | 1 | R | read, contract named by the test, cov 48 lines, uniq 1 |
| `test_cli_grokbot_lifecycle_commands_keep_private_content_out_of_output` | 1 | R | read, contract named by the test, cov 526 lines, uniq 0 |
| `test_cli_grokbot_rejects_invalid_inputs_with_exit_2` | 2 | R | read, contract named by the test, cov 20 lines, uniq 0 |
| `test_cli_grokbot_rejects_invalid_json_files` | 2 | R | read, contract named by the test, cov 4 lines, uniq 0 |
| `test_cli_grokbot_complete_rejects_invalid_artifact_files` | 3 | R | read, contract named by the test, cov 411 lines, uniq 0 |
| `test_report_snapshot_is_private_and_verified` | 1 | R | read, contract named by the test, cov 502 lines, uniq 0 |
| `test_hub_snapshot_conflict_preserves_existing_bytes` | 1 | R | read, contract named by the test, cov 265 lines, uniq 1 |
| `test_a_hub_enqueue_that_times_out_after_the_commit_keeps_the_snapshot` | 1 | C | Row timeout-after-commit of test_a_failed_hub_enqueue_keeps_the_snapshot_unless_the_hub_holds_no_such_job. M4a kills. |
| `test_a_hub_enqueue_failure_keeps_the_snapshot_when_the_hub_cannot_answer` | 1 | C | Row hub-cannot-answer of the same table. |
| `test_a_hub_enqueue_failure_keeps_the_snapshot_when_the_listing_is_refused` | 1 | C | Row listing-refused of the same table. |
| `test_a_hub_enqueue_failure_keeps_the_snapshot_when_the_probe_raises` | 1 | C | Row probe-raises of the same table. |
| `test_a_duplicate_key_refusal_keeps_the_snapshot_without_probing_the_hub` | 2 | C | Rows duplicate-key-conflict and duplicate-key-operation-mismatch of the same table (probed=False). |
| `test_a_plain_refusal_removes_the_snapshot_when_the_hub_holds_no_such_job` | 1 | C | Row plain-refusal-hub-holds-no-such-job of the same table. M4b kills. |
| `test_a_refusal_keeps_the_snapshot_when_the_hub_still_holds_the_job` | 1 | C | Row hub-holds-job of the same table. M4a kills. |
| `test_configured_hub_enqueue_is_fail_closed_and_local_only_without_hub` | 1 | R | read, contract named by the test, cov 364 lines, uniq 2 |
| `test_authority_marker_rejects_active_legacy_and_fails_closed` | 1 | R | read, contract named by the test, cov 371 lines, uniq 2 |
| `test_hub_authority_tracker_rows_do_not_use_local_queue_cards` | 1 | R | read, contract named by the test, cov 290 lines, uniq 22 |
| `test_cli_grokbot_validates_target_before_queue_access` | 1 | R | read, contract named by the test, cov 1 lines, uniq 0 |
| `test_safe_artifact_metadata_includes_report_size` | 1 | R | read, contract named by the test, cov 12 lines, uniq 1 |
| `test_hub_validation_refusal_is_not_hub_unavailable` | 1 | F | Same assertions, five-line hub patch block moved into the hub_wire fixture shared with the claim test. |
| `test_hub_transport_timeout_is_local_and_preserves_job_timeout` | 6 | R | read, contract named by the test, cov 58 lines, uniq 0 |
| `test_hub_transport_timeout_rejects_invalid_values_before_network` | 7 | R | read, contract named by the test, cov 12 lines, uniq 0 |
| `test_hub_transport_timeout_enforces_a_real_deadline` | 1 | R | read, contract named by the test, cov 31 lines, uniq 2 |
| `test_hub_complete_report_accepts_claimed_and_cleans_failed_orphan_only` | 1 | R | read, contract named by the test, cov 362 lines, uniq 7 |
| `test_hub_complete_report_rejects_non_report_kind_without_writing` | 2 | R | read, contract named by the test, cov 272 lines, uniq 0 |
| `test_hub_complete_report_still_completes_report_jobs` | 1 | R | read, contract named by the test, cov 334 lines, uniq 0 |
| `test_hub_report_snapshot_requires_digest` | 1 | R | read, contract named by the test, cov 50 lines, uniq 6 |
| `test_hub_outage_projects_degraded_needs_you_marker` | 1 | R | read, contract named by the test, cov 25 lines, uniq 15 |
| `test_complete_report_roundtrip_keeps_snapshot_private` | 1 | R | read, contract named by the test, cov 536 lines, uniq 0 |
| `test_complete_report_writes_snapshot_before_the_terminal_record` | 1 | R | read, contract named by the test, cov 481 lines, uniq 0 |
| `test_complete_report_rejects_digest_mismatch_without_writing` | 1 | R | read, contract named by the test, cov 501 lines, uniq 1 |
| `test_complete_report_rejects_empty_and_oversize_utf8` | 3 | R | read, contract named by the test, cov 430 lines, uniq 1 |
| `test_complete_report_accepts_max_utf8_bytes` | 1 | R | read, contract named by the test, cov 502 lines, uniq 0 |
| `test_complete_report_rejects_wrong_role_or_kind` | 2 | R | read, contract named by the test, cov 444 lines, uniq 0 |
| `test_complete_report_shares_lease_state_cancel_and_artifact_rules` | 1 | R | read, contract named by the test, cov 486 lines, uniq 2 |
| `test_read_report_rejects_missing_legacy_snapshot` | 1 | R | read, contract named by the test, cov 500 lines, uniq 0 |
| `test_read_report_rejects_symlink_snapshot` | 1 | R | read, contract named by the test, cov 486 lines, uniq 2 |
| `test_read_report_rejects_oversize_invalid_utf8_and_digest_mismatch` | 1 | R | read, contract named by the test, cov 516 lines, uniq 3 |
| `test_read_report_rejects_wrong_role_kind_and_incomplete_jobs` | 1 | R | read, contract named by the test, cov 462 lines, uniq 2 |
| `test_read_report_rejects_corrupted_completed_record` | 1 | R | read, contract named by the test, cov 473 lines, uniq 0 |
| `test_metadata_only_report_completion_stays_compatible` | 1 | R | read, contract named by the test, cov 500 lines, uniq 0 |
| `test_metadata_only_complete_does_not_expose_orphan_snapshot` | 1 | R | read, contract named by the test, cov 534 lines, uniq 0 |
| `test_noncompleted_terminal_cleans_orphan_report_snapshot` | 3 | R | read, contract named by the test, cov 526 lines, uniq 0 |
| `test_completed_report_snapshot_survives_later_terminal_attempts` | 1 | R | read, contract named by the test, cov 531 lines, uniq 0 |
| `test_orphan_cleanup_failure_does_not_terminalize_job` | 1 | R | read, contract named by the test, cov 490 lines, uniq 2 |
| `test_read_report_keeps_reading_after_legal_short_os_read` | 1 | R | read, contract named by the test, cov 502 lines, uniq 0 |
| `test_worker_label_is_stored_and_echoed_on_status_renew_and_complete` | 1 | R | read, contract named by the test, cov 469 lines, uniq 0 |
| `test_claim_rejects_an_invalid_worker_label` | 5 | R | read, contract named by the test, cov 356 lines, uniq 0 |
| `test_validate_worker_label_accepts_the_bounded_pattern` | 1 | R | read, contract named by the test, cov 6 lines, uniq 0 |
| `test_hub_claim_forwards_worker_label_in_the_outbound_request_body` | 1 | C | Row label=builder-2 of test_hub_claim_sends_the_worker_label_only_when_one_is_given. M5a kills. |
| `test_hub_claim_omits_worker_label_from_the_body_when_none_is_sent` | 1 | C | Row label=None of the same test. M5b kills. |
| `test_hub_does_not_yet_persist_worker_label` | 1 | R | Tripwire for a pending hub column, but also pins that the hub ACCEPTS the field it will drop: a protocol-compatibility contract. Rewrite when #1501 lands. |
| `test_hub_projection_drops_a_worker_label_the_write_path_would_refuse` | 1 | R | read, contract named by the test, cov 30 lines, uniq 0 |
| `test_hub_projection_drops_every_label_shape_the_claim_path_refuses` | 4 | R | read, contract named by the test, cov 30 lines, uniq 0 |
| `test_reclaim_attaches_a_worker_label_to_a_row_claimed_without_one` | 1 | R | read, contract named by the test, cov 406 lines, uniq 0 |
| `test_reclaim_with_a_different_worker_label_replaces_the_stored_one` | 1 | R | read, contract named by the test, cov 407 lines, uniq 0 |
| `test_reclaim_without_a_worker_label_keeps_the_stored_one` | 1 | R | read, contract named by the test, cov 393 lines, uniq 0 |
| `test_reclaim_with_an_invalid_worker_label_refuses_before_writing` | 1 | R | read, contract named by the test, cov 398 lines, uniq 0 |
| `test_cli_status_prints_worker_label_next_to_the_claimant` | 1 | R | read, contract named by the test, cov 4 lines, uniq 0 |
| `test_claim_target_is_stored_once_on_the_lease_row_and_never_projected` | 1 | R | read, contract named by the test, cov 450 lines, uniq 1 |
| `test_claim_target_belongs_to_one_lease` | 1 | R | read, contract named by the test, cov 401 lines, uniq 0 |
| `test_claim_target_cannot_be_bound_before_a_claim` | 1 | R | read, contract named by the test, cov 318 lines, uniq 0 |
| `test_invalid_claim_target_is_refused_and_a_stored_one_is_validated` | 4 | R | read, contract named by the test, cov 390 lines, uniq 0 |
| `test_claim_target_on_an_unclaimed_row_is_corrupt` | 1 | R | read, contract named by the test, cov 295 lines, uniq 0 |
| `test_binding_a_claim_target_leaves_the_item_revision_alone` | 1 | R | read, contract named by the test, cov 404 lines, uniq 0 |
| `test_claim_stores_the_claim_target_in_the_same_write_that_grants_the_lease` | 1 | R | read, contract named by the test, cov 366 lines, uniq 0 |
| `test_a_retried_claim_never_overwrites_the_stored_claim_target` | 1 | R | read, contract named by the test, cov 400 lines, uniq 0 |
| `test_a_retried_claim_writes_a_target_into_a_row_that_has_none` | 1 | R | read, contract named by the test, cov 401 lines, uniq 2 |
| `test_an_invalid_claim_target_refuses_the_claim_before_writing` | 4 | R | read, contract named by the test, cov 359 lines, uniq 0 |
| `test_a_claim_that_carries_a_target_answers_with_the_stored_one_from_the_same_write` | 1 | R | read, contract named by the test, cov 405 lines, uniq 0 |

## L2 MCP listener and client identity (owner: grokbot_mcp, grokbot_ops identity paths)

Files: `test_grokbot_mcp.py`, `test_grokbot_client_identity.py`. Declarations at base: 126.

### test_grokbot_mcp.py (104 declarations, partial)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_grokbot_hub_failures_are_permissive_only_without_a_configured_hub` | 1 | R | partial or cov, cov 5 lines, uniq 1 |
| `test_exact_role_tool_inventories_and_hidden_direct_calls_are_indistinguishable` | 1 | R | partial or cov, cov 53 lines, uniq 0 |
| `test_workers_only_list_claim_and_read_their_configured_role` | 1 | R | partial or cov, cov 627 lines, uniq 1 |
| `test_worker_lifecycle_is_mirrored_to_the_fleet_without_queue_content` | 1 | R | partial or cov, cov 691 lines, uniq 0 |
| `test_complete_and_cancel_ack_release_the_fleet_claim` | 1 | R | partial or cov, cov 734 lines, uniq 0 |
| `test_fleet_holder_and_session_are_domain_separated_from_the_queue_lease` | 1 | R | partial or cov, cov 3 lines, uniq 0 |
| `test_same_lease_id_on_different_jobs_derives_distinct_fleet_identities` | 1 | R | partial or cov, cov 3 lines, uniq 0 |
| `test_same_lease_id_on_different_jobs_cannot_share_or_release_fleet_claims` | 1 | R | partial or cov, cov 682 lines, uniq 1 |
| `test_known_fleet_refusal_does_not_claim_or_emit` | 1 | R | partial or cov, cov 472 lines, uniq 0 |
| `test_best_effort_fleet_gaps_still_claim_the_queue` | 1 | R | partial or cov, cov 590 lines, uniq 0 |
| `test_known_fleet_renew_refusal_does_not_heartbeat_or_extend_the_lease` | 1 | R | partial or cov, cov 605 lines, uniq 0 |
| `test_queue_renew_failure_releases_a_granted_fleet_claim` | 1 | R | partial or cov, cov 635 lines, uniq 1 |
| `test_fleet_outage_then_missing_renew_recovers_via_acquire` | 1 | R | partial or cov, cov 643 lines, uniq 0 |
| `test_fleet_outage_then_missing_renew_allows_best_effort_acquire` | 1 | R | partial or cov, cov 639 lines, uniq 0 |
| `test_fleet_outage_then_missing_renew_refuses_known_acquire_refusal` | 1 | R | partial or cov, cov 610 lines, uniq 0 |
| `test_queue_claim_failure_releases_a_granted_fleet_claim` | 1 | R | partial or cov, cov 494 lines, uniq 0 |
| `test_first_claim_keeps_hub_ownership_after_replace` | 3 | R | partial or cov, cov 599 lines, uniq 5 |
| `test_fleet_outage_does_not_fail_queue_claim` | 1 | R | partial or cov, cov 604 lines, uniq 2 |
| `test_configured_hub_outage_fails_closed_and_does_not_advance_local_lifecycle` | 1 | R | partial or cov, cov 288 lines, uniq 4 |
| `test_workers_cannot_retrieve_reports_and_operator_can` | 1 | R | partial or cov, cov 582 lines, uniq 0 |
| `test_rejected_claims_return_generic_error_and_hide_execution_context` | 6 | R | partial or cov, cov 622 lines, uniq 0 |
| `test_worker_inputs_cannot_select_target_role_bot_identity_or_lease_duration` | 1 | R | partial or cov, cov 409 lines, uniq 0 |
| `test_claim_is_admitted_before_queue_mutation_and_binds_the_deterministic_hub_lease` | 1 | R | partial or cov, cov 584 lines, uniq 0 |
| `test_claim_denial_leaves_the_local_job_queued` | 1 | R | partial or cov, cov 440 lines, uniq 1 |
| `test_claim_releases_an_admitted_hub_lease_when_the_local_claim_fails` | 1 | R | partial or cov, cov 490 lines, uniq 0 |
| `test_renew_reconciles_a_legacy_running_job_and_normal_renewals_refresh_the_hub` | 1 | R | partial or cov, cov 613 lines, uniq 0 |
| `test_start_reconciles_a_missing_hub_lease_without_rolling_back_the_local_transition` | 1 | R | partial or cov, cov 551 lines, uniq 0 |
| `test_terminal_worker_transitions_release_hub_leases` | 3 | R | partial or cov, cov 596 lines, uniq 0 |
| `test_hub_mirror_failures_do_not_undo_a_local_terminal_transition_or_leak_private_values` | 1 | R | partial or cov, cov 561 lines, uniq 2 |
| `test_safe_error_projection_never_exposes_queue_error_or_private_content` | 1 | R | partial or cov, cov 160 lines, uniq 0 |
| `test_bearer_comparison_is_constant_time_and_header_shape_is_strict` | 1 | R | partial or cov, cov 25 lines, uniq 0 |
| `test_non_ascii_configured_bearer_is_rejected` | 1 | R | partial or cov, cov 9 lines, uniq 1 |
| `test_listener_configuration_defaults_to_loopback_and_requires_allowlists_for_real_hostnames` | 1 | R | partial or cov, cov 27 lines, uniq 1 |
| `test_security_gate_checks_host_origin_auth_and_bounded_request_size` | 1 | R | partial or cov, cov 41 lines, uniq 3 |
| `test_health_output_is_minimal_and_never_includes_config_or_queue_data` | 1 | R | partial or cov, cov 254 lines, uniq 0 |
| `test_streamable_http_app_enforces_bearer_origin_and_body_limit` | 1 | R | partial or cov, cov 157 lines, uniq 3 |
| `test_asgi_rejects_non_ascii_authorization_without_reaching_the_app` | 1 | R | partial or cov, cov 53 lines, uniq 0 |
| `test_gate_bounded_reads_every_http_method_and_preserves_bodyless_requests` | 1 | R | partial or cov, cov 73 lines, uniq 3 |
| `test_loopback_canary_path_keeps_sdk_loopback_hosts_with_a_public_allowlist` | 1 | R | partial or cov, cov 304 lines, uniq 1 |
| `test_mcp_asgi_rejects_extra_and_malformed_raw_tool_arguments` | 1 | R | partial or cov, cov 314 lines, uniq 1 |
| `test_complete_with_report_text_retrieves_via_operator_without_leaking` | 1 | R | partial or cov, cov 707 lines, uniq 0 |
| `test_metadata_only_scout_completion_operator_report_is_public_validation` | 1 | R | partial or cov, cov 634 lines, uniq 0 |
| `test_direct_complete_rejects_present_non_string_report_text` | 2 | R | partial or cov, cov 500 lines, uniq 0 |
| `test_direct_complete_treats_a_null_report_text_as_an_omitted_one` | 1 | R | partial or cov, cov 632 lines, uniq 0 |
| `test_mcp_raw_schema_accepts_report_text_only_on_complete` | 1 | R | partial or cov, cov 182 lines, uniq 3 |
| `test_asgi_admits_worst_case_json_escaped_max_report` | 1 | R | partial or cov, cov 98 lines, uniq 0 |
| `test_asgi_replay_delegates_to_original_receive_after_buffer` | 1 | R | partial or cov, cov 67 lines, uniq 0 |
| `test_cli_serve_reports_missing_optional_extra_without_printing_secret` | 1 | R | partial or cov, cov 55 lines, uniq 1 |
| `test_hub_authority_mcp_does_not_call_generic_fleet_paths` | 1 | R | partial or cov, cov 427 lines, uniq 8 |
| `test_hub_complete_with_report_text_on_draft_pr_returns_report_not_allowed` | 1 | R | partial or cov, cov 398 lines, uniq 0 |
| `test_local_complete_with_report_text_on_draft_pr_returns_report_not_allowed` | 1 | R | partial or cov, cov 690 lines, uniq 0 |
| `test_descriptor_safe_hub_token_reader_rejects_nul_path` | 1 | R | partial or cov, cov 12 lines, uniq 0 |
| `test_three_listeners_use_distinct_hub_node_tokens_and_public_args_cannot_override` | 1 | R | partial or cov, cov 119 lines, uniq 0 |
| `test_mismatched_hub_actor_credential_refuses_listener_service` | 1 | R | partial or cov, cov 74 lines, uniq 0 |
| `test_queue_list_honors_the_documented_filters_without_widening_the_role` | 1 | R | partial or cov, cov 503 lines, uniq 0 |
| `test_queue_list_limit_default_and_ceiling_stay_bounded` | 1 | R | partial or cov, cov 85 lines, uniq 0 |
| `test_queue_list_refusals_are_bounded_and_never_echo_the_argument` | 10 | R | partial or cov, cov 76 lines, uniq 0 |
| `test_queue_list_role_argument_cannot_reach_another_roles_jobs` | 1 | R | partial or cov, cov 409 lines, uniq 0 |
| `test_queue_claim_mints_a_lease_id_when_omitted_and_still_refuses_malformed` | 1 | R | partial or cov, cov 661 lines, uniq 0 |
| `test_queue_claim_returns_a_supplied_lease_id_unchanged` | 1 | R | partial or cov, cov 584 lines, uniq 0 |
| `test_tool_descriptions_state_the_contract_for_the_listener_role` | 1 | R | partial or cov, cov 34 lines, uniq 0 |
| `test_registered_tool_signatures_advertise_exactly_the_accepted_arguments` | 1 | R | partial or cov, cov 47 lines, uniq 0 |
| `test_one_bounded_journal_line_per_tool_call_never_holds_an_argument_value` | 1 | R | partial or cov, cov 430 lines, uniq 0 |
| `test_journal_configuration_is_idempotent_and_writes_message_only_lines` | 1 | R | partial or cov, cov 9 lines, uniq 1 |
| `test_advertised_input_schema_and_served_gate_accept_the_documented_list_filters` | 1 | R | partial or cov, cov 321 lines, uniq 1 |
| `test_raw_gate_admits_the_documented_list_filters_without_the_mcp_sdk` | 1 | R | partial or cov, cov 80 lines, uniq 0 |
| `test_an_explicit_null_optional_argument_is_treated_as_absent_everywhere` | 1 | R | partial or cov, cov 711 lines, uniq 0 |
| `test_a_non_null_wrong_type_on_an_optional_argument_is_still_refused` | 1 | F | Gained the lease_seconds/worker_label type assertions carried from the two deleted advertise tests. |
| `test_a_terminal_state_filter_with_include_all_false_is_refused_not_silently_empty` | 1 | R | partial or cov, cov 523 lines, uniq 0 |
| `test_consecutive_minted_lease_ids_differ` | 1 | R | partial or cov, cov 586 lines, uniq 0 |
| `test_journal_lines_do_not_propagate_to_ancestor_handlers` | 1 | R | partial or cov, cov 20 lines, uniq 0 |
| `test_listener_lease_defaults_to_fifteen_minutes_and_is_a_per_instance_setting` | 1 | R | partial or cov, cov 27 lines, uniq 4 |
| `test_out_of_bound_listener_lease_is_refused_by_configuration` | 1 | R | partial or cov, cov 6 lines, uniq 1 |
| `test_claim_grants_the_configured_lease_and_returns_its_deadline` | 1 | R | partial or cov, cov 584 lines, uniq 0 |
| `test_claim_accepts_a_requested_lease_within_the_bound` | 1 | R | partial or cov, cov 586 lines, uniq 2 |
| `test_claim_refuses_a_requested_lease_outside_the_bound` | 5 | R | partial or cov, cov 405 lines, uniq 0 |
| `test_claim_advertises_the_optional_lease_seconds_argument` | 1 | C | Private _TOOL_ARGUMENT_TYPES shape. Accept/refuse assertions folded into test_a_non_null_wrong_type_on_an_optional_argument_is_still_refused, required-ness proven by test_registered_tool_signatures_advertise_exactly_the_accepted_arguments and the served tools/list schema. M1a kills. |
| `test_renew_and_fail_after_expiry_answer_lease_expired` | 1 | R | partial or cov, cov 681 lines, uniq 2 |
| `test_renew_and_fail_past_the_execution_deadline_answer_job_expired` | 1 | R | partial or cov, cov 695 lines, uniq 7 |
| `test_a_late_claim_is_granted_and_expires_only_past_the_queue_ttl` | 1 | D | Layer replay of jobs-owner contracts (#1403). Mutation M12-base: counting the execution budget from queued_at SURVIVES this test. Remaining proof: jobs::test_a_late_claim_gets_its_whole_execution_budget (M12 kills), jobs::test_an_unclaimed_job_expires_at_its_queue_ttl, mcp::test_rejected_claims_return_generic_error_and_hide_execution_context[expired]. Helper _backdate_queue_wait removed with it. |
| `test_a_live_lease_conflict_is_still_not_reported_as_expiry` | 1 | R | partial or cov, cov 670 lines, uniq 2 |
| `test_claim_and_renew_journal_the_lease_deadline` | 1 | R | partial or cov, cov 638 lines, uniq 0 |
| `test_queue_claim_stores_and_echoes_a_bounded_worker_label` | 1 | R | partial or cov, cov 646 lines, uniq 0 |
| `test_queue_claim_refuses_an_invalid_worker_label_before_mutation` | 5 | R | partial or cov, cov 418 lines, uniq 0 |
| `test_queue_claim_treats_a_null_worker_label_as_omitted` | 1 | R | partial or cov, cov 595 lines, uniq 0 |
| `test_queue_claim_passes_worker_label_to_hub_claim_unchanged` | 1 | R | partial or cov, cov 119 lines, uniq 0 |
| `test_claim_advertises_the_optional_worker_label_argument` | 1 | C | Same as above for worker_label. M1b kills. |
| `test_retrying_the_same_lease_keeps_its_stored_target` | 1 | R | partial or cov, cov 655 lines, uniq 0 |
| `test_a_first_claim_stores_the_target_in_the_same_write_that_grants_the_lease` | 1 | R | partial or cov, cov 584 lines, uniq 0 |
| `test_a_failed_claim_write_grants_no_lease_and_gives_the_hub_claim_back` | 1 | R | partial or cov, cov 542 lines, uniq 0 |
| `test_an_idempotent_retry_never_resolves_and_never_releases_the_live_claim` | 2 | R | partial or cov, cov 665 lines, uniq 0 |
| `test_a_retry_the_queue_definitively_refuses_gives_the_claim_back` | 5 | R | partial or cov, cov 607 lines, uniq 0 |
| `test_a_retry_after_the_job_failed_gives_back_the_reacquired_claim` | 1 | R | partial or cov, cov 672 lines, uniq 0 |
| `test_a_race_across_an_origin_change_releases_only_the_acquired_key_and_refuses` | 2 | R | partial or cov, cov 612 lines, uniq 2 |
| `test_a_new_lease_resolves_the_target_afresh` | 1 | R | partial or cov, cov 688 lines, uniq 0 |
| `test_a_lost_renew_response_does_not_move_the_release_to_another_key` | 1 | R | partial or cov, cov 686 lines, uniq 2 |
| `test_concurrent_renew_and_release_stay_on_one_key` | 1 | R | partial or cov, cov 590 lines, uniq 0 |
| `test_a_restarted_listener_keeps_the_lease_target_and_never_re_resolves` | 1 | R | partial or cov, cov 684 lines, uniq 0 |
| `test_a_lease_row_from_before_the_migration_resolves_once_and_writes_back` | 1 | R | partial or cov, cov 618 lines, uniq 0 |
| `test_cli_dot_serve_without_bearer` | 1 | R | partial or cov, cov 1 lines, uniq 0 |
| `test_cli_dot_serve_passes_explicit_allowlists` | 1 | R | partial or cov, cov 1 lines, uniq 0 |
| `test_cli_dot_rejects_bearer` | 4 | R | partial or cov, cov 1 lines, uniq 0 |
| `test_cli_other_listeners_require_bearer` | 4 | R | partial or cov, cov 1 lines, uniq 0 |
| `test_cli_dot_setup_without_bearer` | 1 | R | partial or cov, cov 1 lines, uniq 0 |

### test_grokbot_client_identity.py (22 declarations, partial)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_legacy_connector_pack_names_remain_exact` | n | R | partial or cov, cov 11 lines, uniq 0 |
| `test_named_connector_pack_names_are_refused` | n | R | partial or cov, cov 8 lines, uniq 0 |
| `test_named_queue_role_service_result_uses_client_unit` | 3 | R | partial or cov, cov 16 lines, uniq 0 |
| `test_client_configs_and_rendered_services_coexist_and_roundtrip` | 2 | R | partial or cov, cov 293 lines, uniq 0 |
| `test_legacy_config_and_listener_identity_remain_exact` | 1 | R | partial or cov, cov 240 lines, uniq 0 |
| `test_invalid_client_identity_is_refused_before_paths_or_config_writes` | 10 | R | partial or cov, cov 27 lines, uniq 0 |
| `test_config_identity_mismatch_refuses_read_and_overwrite` | 1 | R | partial or cov, cov 184 lines, uniq 3 |
| `test_named_setup_atomic_publication_race` | 5 | R | partial or cov, cov 189 lines, uniq 2 |
| `test_named_setup_does_not_recover_other_publication_errors` | 1 | R | partial or cov, cov 162 lines, uniq 0 |
| `test_distinct_clients_cannot_use_each_others_local_leases` | 1 | R | partial or cov, cov 737 lines, uniq 0 |
| `test_hub_client_identity_must_match_authenticated_credential` | 3 | C | Absorbs the uuid twin as rows (configured_id x authenticated_id). M6 kills. |
| `test_uuid_client_identity_must_match_authenticated_credential` | 3 | C | Rows uuid-none, uuid-other, uuid-match of test_hub_client_identity_must_match_authenticated_credential. |
| `test_canary_refuses_same_role_different_client` | 1 | R | partial or cov, cov 210 lines, uniq 3 |
| `test_named_doctor_endpoint_requires_matching_identity` | 7 | R | partial or cov, cov 323 lines, uniq 0 |
| `test_legacy_doctor_endpoint_does_not_require_identity` | 1 | R | partial or cov, cov 269 lines, uniq 1 |
| `test_cli_resolves_only_selected_client_config` | 3 | R | partial or cov, cov 206 lines, uniq 3 |
| `test_hub_whoami_projects_only_authenticated_node_identity` | 1 | R | partial or cov, cov 141 lines, uniq 1 |
| `test_whoami_identity_option_refuses_non_booleans` | 6 | R | partial or cov, cov 28 lines, uniq 0 |
| `test_identity_option_is_accepted_only_for_read_only_whoami` | 1 | R | partial or cov, cov 22 lines, uniq 1 |
| `test_older_hub_fails_closed_for_identity_opt_in` | 2 | R | partial or cov, cov 86 lines, uniq 2 |
| `test_unnamed_hub_listener_uses_exact_legacy_whoami` | 1 | R | partial or cov, cov 77 lines, uniq 0 |
| `test_distinct_clients_keep_credential_attribution_through_hub_lifecycle` | 1 | R | partial or cov, cov 1120 lines, uniq 74 |

## L3 feed intake (owner: grokbot_feed, grokbot_build_feed, grokbot_scout_feed)

Files: `test_grokbot_feed.py`, `test_grokbot_build_feed.py`, `test_grokbot_scout_feed.py`. Declarations at base: 155.

### test_grokbot_feed.py (52 declarations, partial)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_windows_private_feed_reads_fail_closed_before_io` | 1 | R | partial or cov, cov 14 lines, uniq 1 |
| `test_windows_private_scout_policy_read_fails_closed_before_io` | 1 | R | partial or cov, cov 7 lines, uniq 1 |
| `test_preflight_accepts_a_valid_manifest_without_writing_queue_state` | 1 | R | partial or cov, cov 139 lines, uniq 0 |
| `test_preflight_counts_known_idempotency_records_without_writing` | 1 | R | partial or cov, cov 334 lines, uniq 0 |
| `test_preflight_rejects_malformed_manifests_without_writing_queue_state` | 5 | R | partial or cov, cov 55 lines, uniq 7 |
| `test_preflight_rejects_group_writable_manifest_without_writing` | 1 | R | partial or cov, cov 35 lines, uniq 0 |
| `test_preflight_rejects_world_writable_manifest_without_writing` | 1 | R | partial or cov, cov 35 lines, uniq 0 |
| `test_preflight_rejects_symlink_manifest_without_writing` | 1 | R | partial or cov, cov 28 lines, uniq 2 |
| `test_preflight_rejects_invalid_limits_without_writing` | 6 | R | partial or cov, cov 11 lines, uniq 0 |
| `test_preflight_rejects_invalid_later_job_without_writing` | 1 | C | Row preflight of test_an_invalid_later_entry_rejects_the_manifest_without_writing. M8 kills. |
| `test_preflight_rejects_idempotency_conflict_without_writing` | 1 | R | partial or cov, cov 339 lines, uniq 1 |
| `test_apply_limit_1_creates_only_the_first_unknown_job` | 1 | R | partial or cov, cov 362 lines, uniq 0 |
| `test_apply_repeated_run_skips_known_and_creates_next_unknown` | 1 | R | partial or cov, cov 449 lines, uniq 0 |
| `test_apply_hub_replay_is_skipped_without_duplicate_job_or_event` | 1 | R | partial or cov, cov 701 lines, uniq 13 |
| `test_apply_invalid_later_entry_prevents_all_writes` | 1 | C | Row apply of the same test. M8 kills. |
| `test_apply_stops_on_unexpected_queue_error_with_generic_reason` | 1 | R | partial or cov, cov 381 lines, uniq 0 |
| `test_apply_uses_one_in_memory_snapshot_when_the_manifest_is_replaced` | 1 | R | partial or cov, cov 362 lines, uniq 0 |
| `test_preflight_rejects_unsafe_idempotency_storage_without_leaking_paths` | 1 | R | partial or cov, cov 320 lines, uniq 2 |
| `test_apply_translates_oserror_from_enqueue_to_queue_error` | 1 | R | partial or cov, cov 178 lines, uniq 2 |
| `test_cli_feed_defaults_to_validation_only` | 1 | R | partial or cov, cov 140 lines, uniq 0 |
| `test_cli_feed_preview_needs_no_feed_identity_or_hub_call` | 1 | R | partial or cov, cov 140 lines, uniq 0 |
| `test_cli_feed_apply_binds_dedicated_feed_identity` | 1 | R | partial or cov, cov 236 lines, uniq 0 |
| `test_cli_feed_apply_rejects_unsafe_dedicated_identity_before_enqueue` | 5 | R | partial or cov, cov 22 lines, uniq 0 |
| `test_cli_feed_apply_limit_1_then_repeats_to_the_next_unknown` | 1 | R | partial or cov, cov 450 lines, uniq 0 |
| `test_cli_feed_json_stays_free_of_private_task_context` | 1 | R | partial or cov, cov 363 lines, uniq 0 |
| `test_cli_feed_rejects_invalid_later_entry_without_writing` | 1 | R | partial or cov, cov 168 lines, uniq 0 |
| `test_cli_feed_reports_generic_queue_error_with_index` | 1 | R | partial or cov, cov 390 lines, uniq 0 |
| `test_cli_feed_rejects_invalid_limit` | 1 | R | partial or cov, cov 14 lines, uniq 0 |
| `test_append_wake_status_fails_closed_without_nofollow` | 1 | R | partial or cov, cov 4 lines, uniq 1 |
| `test_append_wake_status_appends_and_refuses_symlink` | 1 | R | partial or cov, cov 25 lines, uniq 1 |
| `test_apply_wake_notify_posts_bounded_body_on_success` | 1 | R | partial or cov, cov 470 lines, uniq 0 |
| `test_apply_wake_notify_non_200_does_not_fail_enqueue` | 1 | R | partial or cov, cov 471 lines, uniq 0 |
| `test_apply_wake_notify_timeout_does_not_fail_enqueue` | 1 | R | partial or cov, cov 472 lines, uniq 0 |
| `test_apply_wake_notify_missing_config_skips_post` | 1 | R | partial or cov, cov 362 lines, uniq 0 |
| `test_cli_feed_apply_omits_wake_key_from_output` | 1 | R | partial or cov, cov 471 lines, uniq 0 |
| `test_wake_role_entry_wins_over_default` | 1 | R | partial or cov, cov 135 lines, uniq 0 |
| `test_wake_default_used_when_role_is_missing` | 1 | R | partial or cov, cov 133 lines, uniq 0 |
| `test_wake_invalid_map_ignored_default_still_used` | 1 | R | partial or cov, cov 133 lines, uniq 2 |
| `test_wake_per_role_sender_key_file_honored` | 1 | R | partial or cov, cov 142 lines, uniq 0 |
| `test_wake_role_list_posts_same_body_to_each_target` | 1 | R | partial or cov, cov 146 lines, uniq 1 |
| `test_wake_role_list_skips_invalid_target_and_posts_the_rest` | 1 | R | partial or cov, cov 144 lines, uniq 2 |
| `test_wake_role_list_caps_at_eight_targets` | 1 | R | partial or cov, cov 138 lines, uniq 0 |
| `test_wake_role_list_writes_per_target_status_lines` | 1 | R | partial or cov, cov 149 lines, uniq 0 |
| `test_worker_instructions_carry_the_five_swarm_contract_rules` | 1 | R | partial or cov, cov 11 lines, uniq 0 |
| `test_contract_asks_for_a_ready_pull_request_when_green` | 1 | R | partial or cov, cov 49 lines, uniq 0 |
| `test_contract_carries_the_stop_rule` | 1 | R | partial or cov, cov 49 lines, uniq 0 |
| `test_worker_instructions_name_every_verification_command` | 1 | R | partial or cov, cov 11 lines, uniq 0 |
| `test_apply_replays_terminal_hub_jobs_and_creates_the_next_unknown` | 3 | R | partial or cov, cov 872 lines, uniq 80 |
| `test_apply_surfaces_bounded_hub_error_detail` | 1 | R | partial or cov, cov 184 lines, uniq 0 |
| `test_cli_feed_reports_bounded_hub_error_detail` | 1 | R | partial or cov, cov 207 lines, uniq 0 |
| `test_apply_surfaces_the_refused_hub_action_on_queue_error` | 1 | R | partial or cov, cov 193 lines, uniq 0 |
| `test_cli_feed_prints_hub_queue_error_action_and_actor_without_tokens` | 2 | R | partial or cov, cov 231 lines, uniq 0 |

### test_grokbot_build_feed.py (58 declarations, partial)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_preflight_selects_the_lowest_open_approved_issue_without_creating_queue` | 1 | R | partial or cov, cov 291 lines, uniq 0 |
| `test_preflight_defaults_the_approval_label_and_lists_issues_with_number_only_argv` | 1 | R | partial or cov, cov 291 lines, uniq 0 |
| `test_apply_enqueues_one_bounded_draft_pr_implementation_worker_and_redacts_results` | 1 | R | partial or cov, cov 660 lines, uniq 0 |
| `test_apply_names_a_completed_scout_report_snapshot_for_the_same_issue` | 1 | R | partial or cov, cov 833 lines, uniq 0 |
| `test_a_scout_report_produced_by_a_retry_revision_is_named` | 1 | R | partial or cov, cov 852 lines, uniq 0 |
| `test_require_scout_report_accepts_a_report_produced_by_a_retry_revision` | 1 | R | partial or cov, cov 835 lines, uniq 0 |
| `test_a_scout_without_a_completed_report_snapshot_is_not_referenced` | 1 | R | partial or cov, cov 654 lines, uniq 0 |
| `test_require_scout_report_holds_issues_that_have_no_report_snapshot` | 1 | R | partial or cov, cov 315 lines, uniq 0 |
| `test_require_scout_report_skips_to_the_first_issue_with_a_report_snapshot` | 1 | R | partial or cov, cov 834 lines, uniq 0 |
| `test_preflight_and_apply_skip_issues_that_already_have_an_idempotency_record` | 1 | R | partial or cov, cov 540 lines, uniq 0 |
| `test_preflight_and_apply_report_all_known_without_writing` | 1 | R | partial or cov, cov 512 lines, uniq 0 |
| `test_preflight_and_apply_enforce_the_utc_daily_limit_including_failed_attempts` | 1 | R | partial or cov, cov 612 lines, uniq 0 |
| `test_a_scout_job_created_today_does_not_count_against_the_build_daily_limit` | 1 | R | partial or cov, cov 738 lines, uniq 0 |
| `test_preflight_and_apply_report_no_approved_issues_without_queue_state` | 1 | R | partial or cov, cov 245 lines, uniq 0 |
| `test_apply_reports_all_known_when_the_queue_answers_idempotently` | 1 | R | partial or cov, cov 358 lines, uniq 0 |
| `test_preflight_uses_an_aware_current_utc_time_when_now_is_omitted` | 1 | R | partial or cov, cov 291 lines, uniq 0 |
| `test_preflight_rejects_a_naive_now_without_queue_state` | 1 | R | partial or cov, cov 198 lines, uniq 1 |
| `test_preflight_rejects_every_invalid_policy_field_without_queue_state` | 21 | R | partial or cov, cov 155 lines, uniq 1 |
| `test_preflight_rejects_a_non_object_or_malformed_policy_document` | 1 | R | partial or cov, cov 33 lines, uniq 2 |
| `test_preflight_rejects_group_or_other_readable_policy_without_queue_state` | 4 | R | partial or cov, cov 37 lines, uniq 0 |
| `test_preflight_rejects_symlinked_policy_without_queue_state` | 1 | R | partial or cov, cov 30 lines, uniq 0 |
| `test_build_key_is_a_role_separated_sha256_digest` | 1 | R | partial or cov, cov 9 lines, uniq 0 |
| `test_preflight_rejects_missing_gh_without_queue_state` | 1 | R | partial or cov, cov 164 lines, uniq 0 |
| `test_preflight_redacts_gh_failure_details` | 1 | R | partial or cov, cov 189 lines, uniq 0 |
| `test_preflight_rejects_malformed_gh_output_without_queue_state` | 1 | R | partial or cov, cov 195 lines, uniq 0 |
| `test_public_selector_boundary_translates_queue_storage_errors` | 2 | R | partial or cov, cov 221 lines, uniq 1 |
| `test_apply_translates_enqueue_errors_at_the_public_boundary` | 1 | R | partial or cov, cov 360 lines, uniq 0 |
| `test_apply_surfaces_the_refused_hub_action_and_actor_kind` | 1 | R | partial or cov, cov 365 lines, uniq 0 |
| `test_apply_counts_hub_worker_jobs_when_the_hub_is_the_queue_authority` | 1 | R | partial or cov, cov 220 lines, uniq 0 |
| `test_build_feed_error_keeps_stable_reasons_for_non_hub_failures` | 1 | R | partial or cov, cov 8 lines, uniq 0 |
| `test_cli_build_feed_preview_json_selects_without_creating_queue` | 1 | R | partial or cov, cov 293 lines, uniq 0 |
| `test_cli_build_feed_apply_json_creates_one_queued_job` | 1 | R | partial or cov, cov 548 lines, uniq 0 |
| `test_cli_build_feed_text_reports_the_selection_and_scout_reference` | 1 | R | partial or cov, cov 728 lines, uniq 0 |
| `test_cli_build_feed_text_reports_no_work_without_private_policy_context` | 1 | R | partial or cov, cov 225 lines, uniq 0 |
| `test_cli_build_feed_apply_binds_dedicated_feed_identity` | 1 | R | partial or cov, cov 387 lines, uniq 0 |
| `test_cli_build_feed_prints_refused_action_without_paths_or_tokens` | 1 | R | partial or cov, cov 378 lines, uniq 1 |
| `test_cli_build_feed_reports_only_stable_malformed_policy_error` | 1 | R | partial or cov, cov 35 lines, uniq 0 |
| `test_cli_build_feed_reports_only_stable_missing_gh_error` | 1 | R | partial or cov, cov 169 lines, uniq 0 |
| `test_cli_build_feed_redacts_queue_errors` | 1 | R | partial or cov, cov 366 lines, uniq 0 |
| `test_cli_build_feed_reports_invalid_feed_configuration` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `test_a_refused_hub_enqueue_leaves_no_snapshot_that_reads_as_known` | 1 | R | partial or cov, cov 611 lines, uniq 0 |
| `test_an_in_doubt_hub_enqueue_keeps_the_snapshot_of_the_job_the_hub_committed` | 1 | R | partial or cov, cov 583 lines, uniq 0 |
| `test_a_snapshot_only_counts_as_known_while_the_queue_listing_still_carries_it` | 1 | R | partial or cov, cov 252 lines, uniq 0 |
| `test_a_hub_scout_report_snapshot_is_named_from_its_derived_job_id` | 1 | R | partial or cov, cov 270 lines, uniq 0 |
| `test_preflight_never_lists_the_hub_even_under_hub_authority` | 1 | R | partial or cov, cov 291 lines, uniq 0 |
| `test_apply_recounts_the_local_daily_limit_when_selection_read_a_stale_queue` | 1 | R | partial or cov, cov 757 lines, uniq 0 |
| `test_apply_refuses_a_second_worker_while_one_is_still_in_flight` | 1 | R | partial or cov, cov 629 lines, uniq 1 |
| `test_apply_relists_the_hub_daily_limit_before_it_enqueues` | 1 | R | partial or cov, cov 400 lines, uniq 0 |
| `test_concurrent_applies_for_distinct_issues_admit_one_job_under_the_local_daily_limit` | 1 | R | partial or cov, cov 745 lines, uniq 0 |
| `test_concurrent_applies_for_distinct_issues_admit_one_job_under_the_hub_daily_limit` | 1 | R | partial or cov, cov 598 lines, uniq 0 |
| `test_the_non_posix_queue_walk_refuses_a_symlinked_queue_component` | 1 | R | partial or cov, cov 22 lines, uniq 1 |
| `test_the_non_posix_queue_walk_reports_a_missing_component_as_absent` | 1 | R | partial or cov, cov 223 lines, uniq 15 |
| `test_build_apply_wake_notify_posts_bounded_body_on_success` | 1 | R | partial or cov, cov 663 lines, uniq 0 |
| `test_build_apply_wake_notify_non_200_does_not_fail_enqueue` | 1 | R | partial or cov, cov 664 lines, uniq 0 |
| `test_build_apply_wake_notify_timeout_does_not_fail_enqueue` | 1 | R | partial or cov, cov 665 lines, uniq 0 |
| `test_build_apply_wake_notify_missing_config_skips_post` | 1 | R | partial or cov, cov 547 lines, uniq 0 |
| `test_worker_envelope_carries_the_swarm_job_contract` | 1 | R | partial or cov, cov 546 lines, uniq 0 |
| `test_a_policy_whose_commands_overflow_the_instruction_bound_is_refused_at_load` | 1 | R | partial or cov, cov 141 lines, uniq 0 |

### test_grokbot_scout_feed.py (45 declarations, partial)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_preflight_discovers_sorted_unique_numbers_without_creating_queue` | 1 | R | partial or cov, cov 272 lines, uniq 0 |
| `test_preflight_invokes_gh_with_exact_number_only_argv_without_a_shell` | 1 | R | partial or cov, cov 272 lines, uniq 0 |
| `test_apply_enqueues_one_fixed_read_only_repository_scout_and_redacts_results` | 1 | R | partial or cov, cov 629 lines, uniq 0 |
| `test_preflight_and_apply_do_no_work_when_a_scout_is_active` | 1 | R | partial or cov, cov 501 lines, uniq 0 |
| `test_preflight_and_apply_enforce_the_utc_daily_limit_including_failed_and_expired_attempts` | 1 | R | partial or cov, cov 676 lines, uniq 1 |
| `test_preflight_and_apply_skip_completed_issues_without_writing` | 1 | R | partial or cov, cov 676 lines, uniq 0 |
| `test_a_terminal_non_completed_scout_is_selected_again_under_a_fresh_key` | n | R | partial or cov, cov 763 lines, uniq 0 |
| `test_a_second_expired_attempt_moves_to_the_next_revision` | 1 | R | partial or cov, cov 693 lines, uniq 0 |
| `test_retries_stop_at_the_revision_bound_and_report_retry_exhausted` | 1 | R | partial or cov, cov 568 lines, uniq 0 |
| `test_preflight_and_apply_prioritize_fewer_dead_attempts` | 3 | R | partial or cov, cov 841 lines, uniq 0 |
| `test_an_exhausted_issue_is_not_counted_as_known` | 1 | R | partial or cov, cov 702 lines, uniq 0 |
| `test_completed_scout_job_id_names_the_revision_that_produced_the_report` | 1 | R | partial or cov, cov 581 lines, uniq 0 |
| `test_completed_scout_job_id_skips_attempts_a_listing_no_longer_stands_behind` | 1 | R | partial or cov, cov 546 lines, uniq 1 |
| `test_preflight_and_apply_report_no_approved_issues_without_queue_state` | 1 | R | partial or cov, cov 212 lines, uniq 0 |
| `test_preflight_uses_an_aware_current_utc_time_when_now_is_omitted` | 1 | R | partial or cov, cov 272 lines, uniq 0 |
| `test_preflight_rejects_every_invalid_policy_field_without_queue_state` | 15 | R | partial or cov, cov 117 lines, uniq 2 |
| `test_preflight_rejects_writable_policy_without_queue_state` | 4 | R | partial or cov, cov 37 lines, uniq 0 |
| `test_scout_key_is_a_fixed_sha256_digest_for_an_extremely_large_issue_number` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `test_public_selector_boundary_translates_queue_storage_errors` | 2 | R | partial or cov, cov 181 lines, uniq 2 |
| `test_apply_translates_enqueue_errors_at_the_public_boundary` | 1 | R | partial or cov, cov 329 lines, uniq 0 |
| `test_apply_surfaces_the_refused_hub_action_and_actor_kind` | 1 | R | partial or cov, cov 334 lines, uniq 0 |
| `test_hub_preview_and_apply_both_select_an_issue_whose_hub_job_expired` | 1 | R | partial or cov, cov 616 lines, uniq 0 |
| `test_a_completed_hub_job_keeps_its_issue_known_but_hub_preview_still_reports_ready` | 1 | R | partial or cov, cov 489 lines, uniq 0 |
| `test_a_snapshot_the_hub_listing_omits_is_not_known` | 1 | R | partial or cov, cov 554 lines, uniq 1 |
| `test_a_refused_hub_listing_reports_the_bounded_reason_instead_of_all_known` | 1 | R | partial or cov, cov 319 lines, uniq 0 |
| `test_scout_feed_error_keeps_stable_reasons_for_non_hub_failures` | 1 | R | partial or cov, cov 8 lines, uniq 0 |
| `test_preflight_rejects_symlinked_policy_without_queue_state` | 1 | R | partial or cov, cov 30 lines, uniq 0 |
| `test_preflight_rejects_missing_gh_without_queue_state` | 1 | R | partial or cov, cov 118 lines, uniq 0 |
| `test_preflight_redacts_gh_failure_details` | 1 | R | partial or cov, cov 143 lines, uniq 0 |
| `test_preflight_rejects_malformed_gh_output_without_queue_state` | 7 | R | partial or cov, cov 151 lines, uniq 3 |
| `test_cli_scout_feed_preview_json_discovers_without_creating_queue` | 1 | R | partial or cov, cov 274 lines, uniq 0 |
| `test_cli_scout_feed_preview_reports_missing_listing_only` | 4 | R | partial or cov, cov 262 lines, uniq 0 |
| `test_cli_scout_feed_apply_json_creates_one_queued_job` | 1 | R | partial or cov, cov 517 lines, uniq 0 |
| `test_cli_scout_feed_apply_binds_dedicated_feed_identity` | 1 | R | partial or cov, cov 357 lines, uniq 0 |
| `test_cli_scout_feed_prints_refused_action_without_paths_or_tokens` | 1 | R | partial or cov, cov 348 lines, uniq 1 |
| `test_cli_scout_feed_text_reports_no_work_without_private_policy_context` | 1 | R | partial or cov, cov 206 lines, uniq 0 |
| `test_cli_scout_feed_text_reports_the_selection_counts` | 1 | R | partial or cov, cov 702 lines, uniq 0 |
| `test_cli_scout_feed_reports_only_stable_malformed_policy_error` | 1 | R | partial or cov, cov 35 lines, uniq 0 |
| `test_cli_scout_feed_reports_only_stable_missing_gh_error` | 1 | R | partial or cov, cov 123 lines, uniq 0 |
| `test_cli_scout_feed_redacts_queue_errors` | 1 | R | partial or cov, cov 335 lines, uniq 0 |
| `test_scout_apply_wake_notify_posts_bounded_body_on_success` | 1 | R | partial or cov, cov 632 lines, uniq 0 |
| `test_scout_apply_wake_notify_non_200_does_not_fail_enqueue` | 1 | R | partial or cov, cov 633 lines, uniq 0 |
| `test_scout_apply_wake_notify_timeout_does_not_fail_enqueue` | 1 | R | partial or cov, cov 634 lines, uniq 0 |
| `test_scout_apply_wake_notify_missing_config_skips_post` | 1 | R | partial or cov, cov 516 lines, uniq 0 |
| `test_scout_envelope_shares_the_untrusted_sentence_and_omits_the_worker_contract` | 1 | R | partial or cov, cov 515 lines, uniq 0 |

## L4 findings and report reconcile (owner: grokbot_findings, grokbot_findings_relay, grokbot_reconcile)

Files: `test_grokbot_findings.py`, `test_grokbot_findings_relay.py`, `test_grokbot_reconcile.py`. Declarations at base: 105.

### test_grokbot_findings.py (48 declarations, partial)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_windows_queue_read_fails_closed_before_io` | 1 | R | partial or cov, cov 6 lines, uniq 1 |
| `test_preview_counts_valid_entries_and_writes_nothing` | 1 | R | partial or cov, cov 243 lines, uniq 0 |
| `test_preview_rejects_malformed_manifests_without_writing` | 6 | R | partial or cov, cov 134 lines, uniq 2 |
| `test_preview_rejects_group_readable_manifest_without_writing` | 1 | R | partial or cov, cov 64 lines, uniq 1 |
| `test_preview_rejects_symlink_manifest_without_writing` | 1 | R | partial or cov, cov 57 lines, uniq 2 |
| `test_preview_rejects_oversized_manifest_without_writing` | 1 | R | partial or cov, cov 77 lines, uniq 2 |
| `test_preview_rejects_invalid_limits_without_writing` | 6 | R | partial or cov, cov 18 lines, uniq 0 |
| `test_apply_writes_one_quoted_handoff_and_a_private_marker` | 1 | R | partial or cov, cov 445 lines, uniq 0 |
| `test_same_revision_is_idempotent` | 1 | R | partial or cov, cov 534 lines, uniq 1 |
| `test_revision_change_writes_a_non_colliding_replacement_draft` | 1 | R | partial or cov, cov 453 lines, uniq 0 |
| `test_malformed_later_entry_fails_closed_before_any_write` | 1 | R | partial or cov, cov 134 lines, uniq 0 |
| `test_conflicting_marker_fails_closed_before_any_write` | 1 | R | partial or cov, cov 342 lines, uniq 1 |
| `test_existing_draft_with_different_bytes_fails_closed` | 1 | R | partial or cov, cov 323 lines, uniq 1 |
| `test_existing_handoff_symlink_fails_closed` | 1 | R | partial or cov, cov 323 lines, uniq 4 |
| `test_restart_after_draft_without_marker_does_not_duplicate` | 1 | R | partial or cov, cov 465 lines, uniq 0 |
| `test_cli_preview_defaults_to_write_nothing_and_omits_finding_text` | 1 | R | partial or cov, cov 244 lines, uniq 0 |
| `test_cli_apply_json_stays_free_of_finding_text` | 1 | R | partial or cov, cov 445 lines, uniq 0 |
| `test_cli_rejects_invalid_limit_without_writing` | 1 | R | partial or cov, cov 19 lines, uniq 0 |
| `test_reconcile_findings_appears_in_parser_inventory` | 1 | D | Copied command name, same remaining proof (M11 and findings CLI tests). |
| `test_concurrent_apply_creates_only_one_draft` | 1 | R | partial or cov, cov 3 lines, uniq 0 |
| `test_generic_manifest_accepts_live_compatible_values_without_leaking_text` | 1 | R | partial or cov, cov 240 lines, uniq 0 |
| `test_generic_manifest_accepts_live_and_backup_severities` | 7 | R | partial or cov, cov 240 lines, uniq 0 |
| `test_generic_manifest_rejects_content_digest_mismatch_without_writing` | 1 | R | partial or cov, cov 116 lines, uniq 0 |
| `test_adapt_live_fleet_preserves_unknown_severity_empty_time_and_revision_digest` | 1 | R | partial or cov, cov 80 lines, uniq 0 |
| `test_adapt_live_backup_preserves_warning_severity_and_revision_digest` | 1 | R | partial or cov, cov 83 lines, uniq 0 |
| `test_adapt_live_finding_rejects_unexpected_keys` | 1 | R | partial or cov, cov 7 lines, uniq 0 |
| `test_adapted_live_finding_loads_through_the_generic_manifest` | 1 | R | partial or cov, cov 475 lines, uniq 0 |
| `test_recovery_writes_count_against_the_same_limit` | 1 | R | partial or cov, cov 466 lines, uniq 0 |
| `test_selection_is_independent_of_manifest_order` | 1 | R | partial or cov, cov 479 lines, uniq 0 |
| `test_corrupt_marker_delivered_at_fails_closed` | 3 | R | partial or cov, cov 510 lines, uniq 4 |
| `test_preview_refuses_symlinked_storage_parents_and_writes_nothing` | 3 | R | partial or cov, cov 478 lines, uniq 0 |
| `test_adapt_live_fleet_accepts_max_reason_and_derives_proposal_title` | 1 | R | partial or cov, cov 80 lines, uniq 0 |
| `test_adapt_live_backup_accepts_max_summary_and_derives_proposal_title` | 1 | R | partial or cov, cov 83 lines, uniq 0 |
| `test_adapt_live_proposal_title_is_sliced_to_the_live_relay_limit` | 1 | R | partial or cov, cov 80 lines, uniq 0 |
| `test_adapt_live_rejects_title_over_the_live_maximum` | 1 | R | partial or cov, cov 19 lines, uniq 1 |
| `test_adapt_live_accepts_source_ref_with_consecutive_dots` | 1 | R | partial or cov, cov 80 lines, uniq 0 |
| `test_generic_manifest_accepts_source_ref_with_consecutive_dots` | 1 | R | partial or cov, cov 240 lines, uniq 0 |
| `test_source_ref_still_rejects_control_characters_and_overlength` | 4 | R | partial or cov, cov 67 lines, uniq 0 |
| `test_convert_live_findings_writes_0600_manifest_without_printing_text` | 1 | R | partial or cov, cov 206 lines, uniq 0 |
| `test_convert_live_findings_json_stays_free_of_finding_text` | 1 | R | partial or cov, cov 201 lines, uniq 0 |
| `test_convert_live_findings_fails_closed_on_invalid_later_entry` | 1 | R | partial or cov, cov 159 lines, uniq 0 |
| `test_convert_live_findings_refuses_symlink_output_and_writes_nothing` | 1 | R | partial or cov, cov 176 lines, uniq 1 |
| `test_convert_live_findings_refuses_symlinked_intermediate_input_parent_and_reads_nothing` | 1 | R | partial or cov, cov 46 lines, uniq 1 |
| `test_convert_live_findings_refuses_symlinked_intermediate_output_parent_and_writes_nothing` | 1 | R | partial or cov, cov 182 lines, uniq 0 |
| `test_convert_findings_appears_in_parser_inventory` | 1 | D | Copied command name, same remaining proof, convert-findings is invoked by the convert_live_findings CLI tests. |
| `test_apply_entries_matches_path_apply_without_a_temp_manifest` | 1 | R | partial or cov, cov 459 lines, uniq 0 |
| `test_apply_entries_validates_every_entry_before_storage_access` | 1 | R | partial or cov, cov 74 lines, uniq 1 |
| `test_apply_entries_delivers_in_producer_finding_revision_order` | 1 | R | partial or cov, cov 385 lines, uniq 0 |

### test_grokbot_findings_relay.py (21 declarations, partial)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_report_event_signature_stays_unchanged` | 1 | R | partial or cov, cov no context recorded (subprocess or class-scoped) |
| `test_preview_projects_only_relay_ids_and_writes_nothing` | 1 | R | partial or cov, cov 181 lines, uniq 0 |
| `test_apply_writes_full_untrusted_body_and_calls_unchanged_report_event` | 1 | R | partial or cov, cov 549 lines, uniq 0 |
| `test_report_false_keeps_pending_and_replay_retries_identical_event` | 1 | R | partial or cov, cov 596 lines, uniq 0 |
| `test_ready_outbox_retries_without_original_finding_in_next_batch` | 1 | R | partial or cov, cov 550 lines, uniq 0 |
| `test_pending_outbox_rejects_changed_entry_with_same_identity` | 1 | R | partial or cov, cov 380 lines, uniq 1 |
| `test_concurrent_bounded_relays_spool_the_entries_they_deliver` | 1 | R | partial or cov, cov 600 lines, uniq 3 |
| `test_existing_legacy_marker_remains_readable_after_upgrade` | 1 | R | partial or cov, cov 599 lines, uniq 5 |
| `test_crash_after_outbox_before_delivery_does_not_lose_the_event` | 1 | R | partial or cov, cov 555 lines, uniq 0 |
| `test_crash_after_delivery_before_ready_recovers_on_replay` | 1 | R | partial or cov, cov 596 lines, uniq 0 |
| `test_report_event_raise_keeps_pending_and_replay_is_duplicate_safe` | 1 | R | partial or cov, cov 595 lines, uniq 0 |
| `test_duplicate_delivery_does_not_write_a_second_draft_or_event` | 1 | R | partial or cov, cov 596 lines, uniq 0 |
| `test_interrupted_draft_recovery_rewrites_the_marker_without_a_second_event` | 1 | R | partial or cov, cov 572 lines, uniq 1 |
| `test_invalid_schema_fails_closed_before_any_write` | 1 | R | partial or cov, cov 23 lines, uniq 0 |
| `test_bounded_batch_rejects_oversized_records_without_writing` | 1 | R | partial or cov, cov 18 lines, uniq 1 |
| `test_pathlike_handles_never_leave_owner_review_draft` | 1 | R | partial or cov, cov 656 lines, uniq 0 |
| `test_hub_auth_and_server_errors_spool_without_finding_text` | 2 | R | partial or cov, cov 536 lines, uniq 0 |
| `test_cli_preview_defaults_to_write_nothing_and_omits_finding_text` | 1 | R | partial or cov, cov 252 lines, uniq 0 |
| `test_cli_apply_json_stays_free_of_finding_text` | 1 | R | partial or cov, cov 609 lines, uniq 0 |
| `test_relay_findings_help_is_preview_first_without_credential_options` | 1 | R | partial or cov, cov 1 lines, uniq 0 |
| `test_existing_findings_commands_remain_in_parser_inventory` | 1 | R | partial or cov, cov 1 lines, uniq 0 |

### test_grokbot_reconcile.py (36 declarations, partial)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_preview_counts_completed_scout_reports_and_writes_nothing` | 1 | R | partial or cov, cov 619 lines, uniq 0 |
| `test_apply_writes_one_quoted_handoff_and_a_private_marker` | 1 | R | partial or cov, cov 708 lines, uniq 0 |
| `test_default_apply_delivers_directly_to_owner_review_inbox` | 1 | R | partial or cov, cov 701 lines, uniq 0 |
| `test_custom_inbox_cannot_escape_owner` | 2 | R | partial or cov, cov 474 lines, uniq 0 |
| `test_existing_handoff_symlink_fails_closed` | 1 | R | partial or cov, cov 650 lines, uniq 1 |
| `test_parent_symlink_swap_after_resolution_fails_closed` | 1 | R | partial or cov, cov 628 lines, uniq 0 |
| `test_non_posix_apply_fails_closed_before_path_write` | 1 | R | partial or cov, cov 4 lines, uniq 1 |
| `test_repeated_apply_does_not_duplicate_drafts` | 1 | R | partial or cov, cov 775 lines, uniq 0 |
| `test_apply_respects_limit_and_continues_on_the_next_run` | 1 | R | partial or cov, cov 733 lines, uniq 0 |
| `test_preview_and_apply_skip_non_report_jobs` | 1 | R | partial or cov, cov 566 lines, uniq 1 |
| `test_missing_legacy_snapshot_does_not_block_a_retrievable_report` | 1 | R | partial or cov, cov 775 lines, uniq 2 |
| `test_malformed_marker_beside_missing_snapshot_still_fails_closed` | 1 | C | Row legacy-snapshot-missing of test_corrupt_marker_fails_closed. R2 kills. |
| `test_failed_atomic_publication_leaves_no_partial_final_or_marker` | 1 | R | partial or cov, cov 672 lines, uniq 1 |
| `test_corrupt_snapshot_fails_closed_without_writing` | 1 | R | partial or cov, cov 572 lines, uniq 1 |
| `test_conflicting_marker_fails_closed` | 1 | C | Row conflict=sha256 of test_a_marker_that_disagrees_with_the_job_fails_closed. R1a kills (R1a-base: the original dies too). |
| `test_marker_with_wrong_task_hash_fails_closed` | 1 | C | Row conflict=task_hash of the same test. R1b kills. |
| `test_corrupt_marker_fails_closed` | 1 | F | Absorbed the legacy-snapshot-missing case as a second row (snapshot-present, legacy-snapshot-missing). R2 kills. |
| `test_marker_with_unexpected_field_fails_closed` | 2 | R | partial or cov, cov 570 lines, uniq 0 |
| `test_marker_read_stays_anchored_when_queue_root_is_swapped` | 1 | R | partial or cov, cov 701 lines, uniq 0 |
| `test_preview_on_empty_target_does_not_create_queue_state` | 1 | R | partial or cov, cov 52 lines, uniq 1 |
| `test_preview_does_not_repair_or_create_an_incomplete_queue` | 1 | R | partial or cov, cov 73 lines, uniq 5 |
| `test_preview_does_not_chmod_existing_queue_directories` | 1 | R | partial or cov, cov 619 lines, uniq 0 |
| `test_cli_preview_defaults_to_write_nothing_and_omits_report_text` | 1 | R | partial or cov, cov 620 lines, uniq 0 |
| `test_cli_apply_json_stays_free_of_report_text` | 1 | R | partial or cov, cov 702 lines, uniq 0 |
| `test_cli_rejects_invalid_limit_without_writing` | 1 | R | partial or cov, cov 481 lines, uniq 0 |
| `test_concurrent_apply_creates_only_one_draft` | 1 | R | partial or cov, cov 461 lines, uniq 0 |
| `test_restart_after_draft_without_marker_does_not_duplicate` | 1 | R | partial or cov, cov 728 lines, uniq 1 |
| `test_hub_authority_preview_lists_completed_scout_without_local_job_record` | 1 | R | partial or cov, cov 674 lines, uniq 0 |
| `test_hub_authority_unavailable_is_missing_artifact_not_leftover_local_job` | 1 | R | partial or cov, cov 682 lines, uniq 4 |
| `test_hub_authority_apply_drafts_unmarked_hub_report` | 1 | R | partial or cov, cov 488 lines, uniq 0 |
| `test_hub_authority_known_marker_skips_hub_report` | 1 | R | partial or cov, cov 562 lines, uniq 0 |
| `test_cli_reconcile_preview_binds_operator_identity_for_hub_listing` | 1 | R | partial or cov, cov 424 lines, uniq 0 |
| `test_cli_reconcile_preview_binds_feed_identity_when_operator_token_absent` | 1 | R | partial or cov, cov 450 lines, uniq 0 |
| `test_hub_authority_apply_lists_hub_before_exclusive_lock` | 1 | R | partial or cov, cov 488 lines, uniq 0 |
| `test_hub_authority_same_task_hash_retries_pair_by_job_id` | 1 | R | partial or cov, cov 573 lines, uniq 0 |
| `test_hub_authority_snapshot_task_hash_mismatch_fails_closed` | 1 | R | partial or cov, cov 301 lines, uniq 1 |

## L5 packs, ops and relays (owner: grokbot_packs, grokbot_ops, grokbot_pack_relay, grokbot_operations_relay)

Files: `test_grokbot_packs.py`, `test_grokbot_ops.py`, `test_grokbot_pack_relay.py`, `test_grokbot_operations_relay.py`. Declarations at base: 128.

### test_grokbot_packs.py (58 declarations, partial)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_registry_is_closed_deterministic_and_exact_key_validated` | 1 | R | partial or cov, cov 129 lines, uniq 0 |
| `test_registry_ignores_user_supplied_pack_files` | 1 | R | partial or cov, cov 131 lines, uniq 0 |
| `test_registry_rejects_duplicate_pack_ids` | 1 | R | partial or cov, cov 62 lines, uniq 1 |
| `test_registry_rejects_duplicate_ports` | 1 | R | partial or cov, cov 64 lines, uniq 1 |
| `test_registry_rejects_overlapping_nonempty_public_routes` | 1 | R | partial or cov, cov 71 lines, uniq 5 |
| `test_registry_rejects_unsafe_or_non_loopback_default_binds` | 1 | R | partial or cov, cov 47 lines, uniq 4 |
| `test_registry_rejects_invalid_versions` | 1 | R | partial or cov, cov 21 lines, uniq 1 |
| `test_registry_rejects_declared_tool_inventory_drift` | 1 | R | partial or cov, cov 27 lines, uniq 1 |
| `test_registry_rejects_unexpected_pack_keys` | 1 | R | partial or cov, cov 12 lines, uniq 1 |
| `test_first_party_queue_packs_keep_isolated_ports_tools_and_credentials` | 1 | F | Dropped five port assertions that duplicate the default_bind pins in test_registry_is_closed_deterministic_and_exact_key_validated, queue ports, per-role tools and config paths kept. |
| `test_setup_preview_writes_nothing_and_apply_uses_mode_0600` | 1 | R | partial or cov, cov 461 lines, uniq 0 |
| `test_setup_rejects_missing_and_weak_secret_references` | 1 | R | partial or cov, cov 171 lines, uniq 4 |
| `test_setup_is_idempotent_and_keeps_role_default_ports` | 1 | R | partial or cov, cov 459 lines, uniq 0 |
| `test_setup_refuses_bind_owned_by_another_packaged_default` | 1 | R | partial or cov, cov 264 lines, uniq 0 |
| `test_setup_refuses_bind_owned_by_another_installed_custom_bind` | 1 | R | partial or cov, cov 454 lines, uniq 0 |
| `test_setup_same_instance_custom_bind_is_idempotent` | 1 | R | partial or cov, cov 459 lines, uniq 0 |
| `test_setup_refuses_unsafe_existing_config_paths_without_exposing_contents` | 1 | R | partial or cov, cov 256 lines, uniq 2 |
| `test_setup_first_install_failure_leaves_neither_config_store` | 1 | R | partial or cov, cov 464 lines, uniq 7 |
| `test_setup_update_failure_preserves_existing_bytes_and_modes` | 1 | R | partial or cov, cov 474 lines, uniq 0 |
| `test_setup_rollback_restore_failure_raises_rollback_failed` | 1 | R | partial or cov, cov 473 lines, uniq 2 |
| `test_setup_rollback_verification_mismatch_raises_rollback_failed` | 1 | R | partial or cov, cov 474 lines, uniq 0 |
| `test_setup_and_removal_refuse_descriptor_unsafe_paths` | 1 | R | partial or cov, cov 446 lines, uniq 3 |
| `test_update_preview_writes_nothing_and_apply_stays_compatible` | 1 | R | partial or cov, cov 463 lines, uniq 6 |
| `test_removal_preview_writes_nothing_and_apply_stays_in_owned_scope` | 1 | R | partial or cov, cov 576 lines, uniq 2 |
| `test_removal_refuses_unsafe_permissions_and_outside_paths` | 1 | R | partial or cov, cov 451 lines, uniq 3 |
| `test_doctor_and_canary_are_sanitized_and_non_mutating` | 1 | R | partial or cov, cov 643 lines, uniq 6 |
| `test_cli_preview_is_default_and_redacts_secrets` | 1 | R | partial or cov, cov 766 lines, uniq 0 |
| `test_parser_inventory_includes_pack_lifecycle_and_legacy_commands` | 1 | D | Copied command list, same remaining proof as above. |
| `test_legacy_lifecycle_stays_compatible_with_pack_config` | 1 | R | partial or cov, cov 592 lines, uniq 0 |
| `test_fleet_steward_pack_is_closed_connector_with_exact_inventory` | 1 | R | partial or cov, cov 132 lines, uniq 0 |
| `test_fleet_setup_preview_apply_and_rejects_foreign_keys` | 1 | R | partial or cov, cov 386 lines, uniq 1 |
| `test_fleet_setup_refuses_queue_default_ports` | 1 | R | partial or cov, cov 516 lines, uniq 0 |
| `test_obsidian_is_closed_connector_not_a_steward` | 1 | R | partial or cov, cov 132 lines, uniq 0 |
| `test_obsidian_setup_preview_apply_and_rejects_foreign_keys` | 1 | R | partial or cov, cov 434 lines, uniq 3 |
| `test_obsidian_setup_refuses_existing_default_ports` | 1 | R | partial or cov, cov 549 lines, uniq 0 |
| `test_pack_doctor_does_not_call_systemctl_without_service_result` | 1 | R | partial or cov, cov 579 lines, uniq 0 |
| `test_pack_doctor_service_result_uses_exact_service_argv` | n | R | partial or cov, cov 195 lines, uniq 13 |
| `test_pack_doctor_appends_service_result_after_early_config_failure` | 1 | R | partial or cov, cov 180 lines, uniq 0 |
| `test_pack_doctor_cli_json_and_plain_service_result_shape` | 1 | R | partial or cov, cov 602 lines, uniq 0 |
| `test_pack_doctor_cli_returns_zero_when_every_check_is_ok` | 1 | R | partial or cov, cov 491 lines, uniq 0 |
| `test_service_result_flag_is_pack_doctor_only` | 1 | R | partial or cov, cov 1 lines, uniq 0 |
| `test_first_party_pack_units_use_shared_listener_recovery_policy` | 1 | R | partial or cov, cov 495 lines, uniq 0 |
| `test_pack_doctor_cli_fails_only_on_non_allowlisted_statuses` | 5 | R | partial or cov, cov 1 lines, uniq 0 |
| `test_pack_list_reports_installed_bind_alongside_packaged_default` | 1 | R | partial or cov, cov 445 lines, uniq 0 |
| `test_parser_construction_performs_no_installed_pack_io` | 1 | R | partial or cov, cov 1 lines, uniq 0 |
| `test_pack_doctor_warns_on_installed_default_bind_drift` | 1 | R | partial or cov, cov 593 lines, uniq 0 |
| `test_pack_doctor_fails_ingress_port_mismatch_from_cloudflared_config` | 1 | R | partial or cov, cov 668 lines, uniq 55 |
| `test_pack_doctor_fails_allowed_host_missing_on_running_listener` | 1 | R | partial or cov, cov 626 lines, uniq 1 |
| `test_pack_doctor_reports_manual_no_public_route_for_connector` | 1 | R | partial or cov, cov 396 lines, uniq 0 |
| `test_pack_doctor_stays_quiet_when_connector_has_public_host` | 1 | R | partial or cov, cov 430 lines, uniq 0 |
| `test_pack_doctor_stays_quiet_when_connector_is_host_local` | 1 | R | partial or cov, cov 387 lines, uniq 1 |
| `test_pack_list_json_exposes_reachability` | 1 | R | partial or cov, cov 361 lines, uniq 0 |
| `test_t3_dispatch_setup_requires_runtime_path` | 1 | R | partial or cov, cov 440 lines, uniq 3 |
| `test_dot_setup_has_runtime_without_bearer` | 1 | R | partial or cov, cov 356 lines, uniq 1 |
| `test_dot_setup_rejects_unexpected_fields` | 5 | R | partial or cov, cov 181 lines, uniq 0 |
| `test_dot_setup_validates_runtime` | 1 | R | partial or cov, cov 193 lines, uniq 3 |
| `test_dot_lifecycle_delegates` | 1 | R | partial or cov, cov 173 lines, uniq 3 |
| `test_removed_packs_are_unknown` | 4 | R | partial or cov, cov 131 lines, uniq 0 |

### test_grokbot_ops.py (45 declarations, partial)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_setup_writes_role_scoped_config_without_secret_values` | 1 | R | partial or cov, cov 170 lines, uniq 0 |
| `test_setup_supports_all_roles_and_secret_file_references` | 1 | R | partial or cov, cov 179 lines, uniq 0 |
| `test_setup_stores_hub_token_file_reference_and_renders_role_environment` | 1 | R | partial or cov, cov 240 lines, uniq 1 |
| `test_setup_with_hub_authority_does_not_require_queue_status` | 1 | R | partial or cov, cov 268 lines, uniq 0 |
| `test_operator_setup_renders_its_own_hub_actor_token_reference` | 1 | R | partial or cov, cov 236 lines, uniq 0 |
| `test_setup_rejects_bad_instance_and_bad_secret_reference_shape` | 1 | R | partial or cov, cov 20 lines, uniq 1 |
| `test_setup_stores_secret_reference_even_when_bearer_is_not_yet_present` | 1 | R | partial or cov, cov 170 lines, uniq 0 |
| `test_doctor_reports_sanitized_checks_without_private_content` | 1 | R | partial or cov, cov 536 lines, uniq 0 |
| `test_doctor_uses_configured_listener_actor_for_hub_queue_status` | 3 | R | partial or cov, cov 300 lines, uniq 0 |
| `test_doctor_keeps_host_identity_without_configured_listener_token` | 1 | R | partial or cov, cov 241 lines, uniq 0 |
| `test_doctor_refuses_changed_unsafe_listener_token_file_without_secret_or_path` | 1 | R | partial or cov, cov 205 lines, uniq 0 |
| `test_doctor_reports_feed_authority_ok_when_feed_can_list` | 1 | R | partial or cov, cov 278 lines, uniq 0 |
| `test_doctor_reports_feed_authority_fail_when_feed_list_is_refused` | 1 | R | partial or cov, cov 276 lines, uniq 0 |
| `test_doctor_reports_feed_authority_fail_when_only_the_worker_listing_is_refused` | 1 | R | partial or cov, cov 276 lines, uniq 0 |
| `test_doctor_skips_feed_authority_without_a_configured_feed_token` | 1 | R | partial or cov, cov 235 lines, uniq 1 |
| `test_doctor_omits_feed_authority_without_hub_authority` | 1 | R | partial or cov, cov 227 lines, uniq 0 |
| `test_doctor_feed_authority_probe_lists_both_feed_roles_with_no_mutating_hub_action` | 1 | R | partial or cov, cov 278 lines, uniq 0 |
| `test_doctor_host_wide_feed_authority_forwards_timeout_to_every_transport` | 6 | R | partial or cov, cov 306 lines, uniq 0 |
| `test_doctor_feed_authority_distinguishes_unavailable_from_auth_refusal` | 6 | R | partial or cov, cov 54 lines, uniq 0 |
| `test_doctor_feed_authority_invalid_granted_identity_precedes_listing_outage` | 16 | R | partial or cov, cov 52 lines, uniq 0 |
| `test_doctor_command_treats_a_skipped_feed_authority_probe_as_non_failing` | 5 | R | partial or cov, cov 21 lines, uniq 0 |
| `test_doctor_fails_cleanly_on_missing_config` | 1 | R | partial or cov, cov 59 lines, uniq 0 |
| `test_canary_verifies_auth_rejection_and_exact_inventory` | 1 | R | partial or cov, cov 283 lines, uniq 1 |
| `test_canary_accepts_anonymous_http_auth_challenge` | 1 | R | partial or cov, cov 210 lines, uniq 0 |
| `test_anonymous_health_status_preserves_http_auth_rejection` | 1 | R | partial or cov, cov 5 lines, uniq 0 |
| `test_authenticated_health_requests_ignore_proxy_environment_and_refuse_redirects` | 1 | R | partial or cov, cov 10 lines, uniq 0 |
| `test_authenticated_health_redirect_never_reaches_redirect_target` | 1 | R | partial or cov, cov 10 lines, uniq 0 |
| `test_tools_list_uses_an_initialized_official_mcp_session` | 1 | R | partial or cov, cov 10 lines, uniq 2 |
| `test_mcp_client_components_use_the_sdk_http_transport` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `test_canary_fails_on_wrong_inventory` | 1 | R | partial or cov, cov 214 lines, uniq 2 |
| `test_canary_is_non_mutating_and_reports_missing_listener` | 1 | R | partial or cov, cov 276 lines, uniq 0 |
| `test_render_unit_is_role_scoped_with_protected_token_reference_only` | 1 | R | partial or cov, cov 91 lines, uniq 0 |
| `test_render_unit_quotes_space_and_quote_arguments_and_rejects_controls` | 1 | R | partial or cov, cov 88 lines, uniq 1 |
| `test_write_unit_refuses_unrelated_overwrites_and_honors_force` | 1 | R | partial or cov, cov 155 lines, uniq 1 |
| `test_write_unit_no_force_refuses_competing_file_created_before_publication` | 1 | R | partial or cov, cov 147 lines, uniq 2 |
| `test_config_operations_refuse_symlinked_config_file_and_directory` | 1 | R | partial or cov, cov 169 lines, uniq 0 |
| `test_posix_path_walker_uses_opath_root_and_keeps_read_write_nofollow` | 1 | R | partial or cov, cov 62 lines, uniq 0 |
| `test_write_unit_refuses_symlinked_paths_and_force_replaces_only_the_link` | 1 | R | partial or cov, cov 160 lines, uniq 4 |
| `test_write_unit_keeps_identical_content_and_mode_without_rewriting` | 1 | R | partial or cov, cov 157 lines, uniq 1 |
| `test_install_service_cli_prints_dry_run_by_default` | 1 | R | partial or cov, cov 245 lines, uniq 0 |
| `test_new_grokbot_commands_appear_in_parser_derived_inventory` | 1 | D | Copied command list. Remaining proof: tests/test_cli_inventory_contract.py::test_parser_inventory_and_missing_cli_agree_after_alias_normalization (parser equals the documented inventory) plus every command invoked through cli.main in the lifecycle tests. M11 kills the keepers. |
| `test_queue_role_units_use_shared_listener_recovery_policy` | 3 | R | partial or cov, cov 84 lines, uniq 0 |
| `test_inspect_service_result_uses_fixed_argv_and_succeeds_on_exact_success` | 1 | R | partial or cov, cov 21 lines, uniq 0 |
| `test_inspect_service_result_fails_closed_without_raw_output` | 9 | R | partial or cov, cov 21 lines, uniq 0 |
| `test_inspect_service_result_sanitizes_raw_stdout_and_stderr` | 1 | R | partial or cov, cov 21 lines, uniq 0 |

### test_grokbot_pack_relay.py (13 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_collect_manifests_adapts_steward_ledgers_in_memory` | 1 | R | cov, 600 lines, uniq 0 |
| `test_preview_relay_counts_without_writing` | 1 | R | cov, 774 lines, uniq 7 |
| `test_known_finding_does_not_starve_new_fleet_creation` | 1 | R | cov, 1188 lines, uniq 9 |
| `test_invalid_limit_is_rejected_before_writes` | 1 | R | cov, 396 lines, uniq 0 |
| `test_relay_setup_writes_owner_only_config_and_hides_paths` | 1 | R | cov, 108 lines, uniq 0 |
| `test_relay_setup_rejects_insecure_owner_and_config_symlink` | 1 | R | cov, 83 lines, uniq 2 |
| `test_render_relay_units_are_oneshot_timer_and_hide_owner_text` | 1 | R | cov, 190 lines, uniq 0 |
| `test_render_relay_units_preview_omits_private_python_executable` | 1 | R | cov, 156 lines, uniq 0 |
| `test_relay_preview_hides_package_path_but_written_unit_keeps_it` | 1 | R | cov, 190 lines, uniq 0 |
| `test_cli_pack_relay_commands_are_preview_first_and_omit_id` | 1 | R | cov, 227 lines, uniq 15 |
| `test_cli_install_relay_service_preview_omits_absolute_paths` | 1 | R | cov, 195 lines, uniq 2 |
| `test_write_relay_units_keeps_both_units_unchanged_when_second_write_fails` | 1 | R | cov, 209 lines, uniq 16 |
| `test_write_relay_units_rejects_service_symlink_with_force_before_either_write` | 1 | R | cov, 164 lines, uniq 1 |

### test_grokbot_operations_relay.py (12 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_operations_relay_pack_is_closed_on_8777_with_exact_inventory` | 1 | R | cov, 132 lines, uniq 0 |
| `test_setup_preview_apply_and_rejects_foreign_keys` | 1 | R | cov, 357 lines, uniq 1 |
| `test_setup_rejects_insecure_owner_and_sanitizes_errors` | 1 | R | cov, 210 lines, uniq 4 |
| `test_lifecycle_rejects_short_bearer` | 1 | R | cov, 411 lines, uniq 1 |
| `test_lifecycle_rejects_shared_dispatch_bearer` | 1 | R | cov, 413 lines, uniq 1 |
| `test_auth_gate_and_inventory` | 1 | R | cov, 30 lines, uniq 23 |
| `test_submit_rejects_secrets_paths_commands_urls_and_unexpected_keys` | 1 | R | cov, 34 lines, uniq 6 |
| `test_first_delivery_writes_one_draft_and_one_fleet_report` | 1 | R | cov, 585 lines, uniq 0 |
| `test_replay_is_idempotent` | 1 | R | cov, 632 lines, uniq 0 |
| `test_status_returns_digest_and_state_without_entry_content` | 1 | R | cov, 611 lines, uniq 6 |
| `test_status_degrades_when_findings_storage_is_unsafe` | 1 | R | cov, 66 lines, uniq 2 |
| `test_doctor_canary_unit_and_cli_hide_secrets` | 1 | R | cov, 698 lines, uniq 147 |

## L6 memory and inbox profiles (owner: grokbot_memory_credential, brigade.dot grok profiles)

Files: `test_grokbot_memory_credential.py`, `test_grokbot_memory_profile.py`, `test_grokbot_s26_profiles.py`. Declarations at base: 87.

### test_grokbot_memory_credential.py (23 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_a_private_user_level_token_file_turns_refs_on` | 1 | R | cov, 41 lines, uniq 0 |
| `test_refs_stay_off_while_the_key_is_absent` | 1 | R | cov, 22 lines, uniq 0 |
| `test_a_token_file_with_group_or_world_access_is_refused` | 5 | R | cov, 29 lines, uniq 0 |
| `test_a_symlinked_token_file_is_refused` | 1 | R | cov, 29 lines, uniq 0 |
| `test_a_missing_token_file_is_refused` | 1 | R | cov, 29 lines, uniq 0 |
| `test_a_token_file_that_is_not_one_bounded_token_is_refused` | 5 | R | cov, 29 lines, uniq 0 |
| `test_a_relative_path_is_refused_so_the_working_directory_cannot_choose` | 1 | R | cov, 25 lines, uniq 1 |
| `test_a_malformed_key_is_refused` | 4 | R | cov, 23 lines, uniq 0 |
| `test_an_unparsable_fleet_toml_is_refused` | 1 | R | cov, 21 lines, uniq 3 |
| `test_no_fleet_hub_means_no_refs` | 1 | R | cov, 31 lines, uniq 1 |
| `test_the_node_credential_cannot_double_as_the_bot_credential` | 1 | R | cov, 33 lines, uniq 1 |
| `test_workspace_files_never_set_or_override_the_path` | 1 | R | cov, 43 lines, uniq 0 |
| `test_the_token_never_reaches_output_logs_or_errors` | 1 | R | cov, 85 lines, uniq 0 |
| `test_doctor_reports_the_reason_and_fails_only_on_a_refused_key` | 1 | R | cov, 49 lines, uniq 0 |
| `test_pack_doctor_shape_is_unchanged` | 1 | R | cov, 38 lines, uniq 0 |
| `test_fleet_settings_for_other_principals_are_unchanged` | 1 | R | cov, 41 lines, uniq 0 |
| `test_a_relative_brigade_home_reads_and_sends_nothing` | 1 | R | cov, 14 lines, uniq 1 |
| `test_an_absolute_brigade_home_is_still_read` | 1 | R | cov, 41 lines, uniq 0 |
| `test_another_agents_token_with_memory_grants_leaves_refs_off` | 1 | R | cov, 41 lines, uniq 0 |
| `test_an_identity_that_cannot_be_established_leaves_refs_off` | 2 | R | cov, 42 lines, uniq 0 |
| `test_the_identity_request_goes_to_the_user_level_hub` | 1 | R | cov, 41 lines, uniq 0 |
| `test_windows_keeps_refs_off_without_opening_the_token_file` | 1 | R | cov, 29 lines, uniq 1 |
| `test_the_platform_gate_matches_the_real_platform` | 1 | R | cov, 1 lines, uniq 1 |

### test_grokbot_memory_profile.py (19 declarations, read in full)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_profile_exposes_exactly_the_memory_tools` | 1 | R | read, contract named by the test, cov no context recorded (subprocess or class-scoped) |
| `test_profile_reads_and_files_handoffs_for_review_only` | 1 | R | read, contract named by the test, cov no context recorded (subprocess or class-scoped) |
| `test_validation_refuses_every_tool_outside_the_profile` | 1 | C | Negative loop identical to s26::test_no_dispatch_claim_or_fleet_tool_is_reachable[grokbot-memory] (M2d-base: deleted loop and keeper both die). Positive default-limit assertion kept as test_validation_accepts_a_profile_tool_with_its_default_limit (M2b kills). |
| `test_runtime_parses_with_messaging_dispatch_and_events_off` | 1 | R | read, contract named by the test, cov no context recorded (subprocess or class-scoped) |
| `test_runtime_refuses_any_key_that_widens_the_profile` | 12 | R | read, contract named by the test, cov no context recorded (subprocess or class-scoped) |
| `test_runtime_documents_never_cross_schemas` | 1 | R | read, contract named by the test, cov no context recorded (subprocess or class-scoped) |
| `test_build_tools_has_no_fleet_and_refuses_other_tools_at_call_time` | 1 | F | Renamed test_build_tools_files_handoffs_in_the_grok_inbox_and_refuses_another_profile, call-by-name refusal loop dropped (s26::test_call_by_name_outside_the_profile_is_refused owns it), handoff inbox path and DUET mismatch kept (M2c kills). |
| `test_registered_tools_are_exactly_the_profile_with_dot_schemas` | 1 | C | Identical to s26::test_registered_discovery_is_exactly_the_profile[grokbot-memory], the memory_search/memory_card 'never instructions' description assertion moved into it (M2a kills). |
| `test_registry_lists_the_memory_pack` | 1 | R | read, contract named by the test, cov 129 lines, uniq 0 |
| `test_unit_names_the_pack` | 1 | R | read, contract named by the test, cov 193 lines, uniq 0 |
| `test_listener_starts_without_a_hub_inbox_poll` | 1 | R | read, contract named by the test, cov 194 lines, uniq 0 |
| `test_watch_runs_only_route_and_credential_checks` | 1 | R | read, contract named by the test, cov 194 lines, uniq 0 |
| `test_cli_serve_passes_its_pack_id_and_refuses_a_bearer` | 1 | R | read, contract named by the test, cov 1 lines, uniq 0 |
| `MemoryOutput.test_clean_card_and_match_carry_untrusted` | 1 | R | read, contract named by the test, cov 4 lines, uniq 0 |
| `MemoryOutput.test_scan_hit_returns_only_the_card_id_and_rule` | 1 | R | read, contract named by the test, cov 4 lines, uniq 0 |
| `MemoryOutput.test_scan_hit_in_any_card_field_blocks_the_match` | 1 | R | read, contract named by the test, cov 4 lines, uniq 0 |
| `MemoryOutput.test_a_card_authored_legacy_id_is_scanned_and_never_echoed` | 1 | R | read, contract named by the test, cov 4 lines, uniq 0 |
| `MemoryOutput.test_dot_and_duet_refuse_a_scan_hit` | 1 | R | read, contract named by the test, cov 4 lines, uniq 0 |
| `test_scan_rules_without_regex_for_invisible_characters` | 1 | R | read, contract named by the test, cov no context recorded (subprocess or class-scoped) |

### test_grokbot_s26_profiles.py (45 declarations, partial)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_each_profile_lists_exactly_its_tools` | 1 | R | partial or cov, cov no context recorded (subprocess or class-scoped) |
| `test_registered_discovery_is_exactly_the_profile` | 2 | F | Now also carries the description-notice assertion for memory_search and memory_card. |
| `test_no_dispatch_claim_or_fleet_tool_is_reachable` | 2 | R | partial or cov, cov no context recorded (subprocess or class-scoped) |
| `test_call_by_name_outside_the_profile_is_refused` | 2 | R | partial or cov, cov no context recorded (subprocess or class-scoped) |
| `test_inbox_runtime_parses_with_dispatch_events_and_memory_off` | 1 | R | partial or cov, cov no context recorded (subprocess or class-scoped) |
| `test_inbox_runtime_refuses_any_key_that_widens_the_profile` | 14 | R | partial or cov, cov no context recorded (subprocess or class-scoped) |
| `test_the_two_grok_runtimes_never_cross_schemas_or_principals` | 1 | R | partial or cov, cov no context recorded (subprocess or class-scoped) |
| `InboxProfile.test_send_outside_the_hub_grants_gets_the_v1_code_and_sends_nothing` | 1 | R | partial or cov, cov no context recorded (subprocess or class-scoped) |
| `InboxProfile.test_send_to_a_name_outside_the_allowlist_gets_the_same_code_without_a_hub_call` | 1 | R | partial or cov, cov no context recorded (subprocess or class-scoped) |
| `InboxProfile.test_a_granted_alias_sends` | 1 | R | partial or cov, cov no context recorded (subprocess or class-scoped) |
| `InboxProfile.test_reply_denial_keeps_the_hub_code_too` | 1 | R | partial or cov, cov no context recorded (subprocess or class-scoped) |
| `InboxProfile.test_messaging_off_refuses_send_and_reply` | 1 | R | partial or cov, cov no context recorded (subprocess or class-scoped) |
| `InboxProfile.test_inbox_responses_carry_no_memory_hits` | 1 | R | partial or cov, cov no context recorded (subprocess or class-scoped) |
| `test_server_result_carries_the_hub_code_beside_the_connector_name` | 1 | R | partial or cov, cov 6 lines, uniq 0 |
| `HandoffToHub.test_handoff_goes_to_the_hub_as_the_principal_and_writes_no_file` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `HandoffToHub.test_a_retry_in_the_same_window_reuses_the_idempotency_id` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `HandoffToHub.test_credential_shaped_text_never_reaches_the_hub` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `HandoffToHub.test_the_file_path_lint_runs_before_the_hub_and_a_failure_sends_nothing` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `HandoffToHub.test_hub_refusals_come_back_as_codes_and_nothing_is_written` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `HandoffToHub.test_a_swapped_token_file_is_refused_before_any_transport` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `HandoffToHub.test_a_moved_hub_url_is_refused_before_any_transport` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `HandoffToHub.test_a_missing_token_file_fails_closed_without_a_local_fallback` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `PrincipalReads.test_hub_reads_run_under_the_pinned_principal_token_and_replica_pin` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `PrincipalReads.test_dot_and_duet_search_the_hub_as_their_principal_never_through_the_cli` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `PrincipalReads.test_a_swapped_token_file_is_refused_before_the_child_runs` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `PrincipalReads.test_a_moved_hub_url_is_refused_before_the_child_runs` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `PrincipalReads.test_hub_memory_off_fails_closed_for_search_and_card` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `PrincipalReads.test_an_unreadable_hub_mode_never_falls_back_to_the_node_environment` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `PrincipalReads.test_search_drops_local_matches_in_every_mode` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `PrincipalReads.test_card_never_opens_a_local_file_and_reads_the_hub` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `PrincipalReads.test_hub_answers_get_the_prompt_load_scan` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `PrincipalReads.test_the_dot_and_duet_profiles_still_serve_local_cards` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `TokenCopies.test_a_call_removes_its_copy_and_a_crash_leftover_is_swept_on_start` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `StateDirFailures.test_the_hub_handoff_path_maps_a_state_dir_oserror_like_the_file_path` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `StateDirFailures.test_the_read_path_maps_a_state_dir_oserror_to_memory_unavailable` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `OfflineAnswers.test_the_fallback_never_lists_local_and_the_warning_names_the_hub` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `OfflineAnswers.test_dot_and_duet_report_an_unavailable_hub_without_a_fallback` | 1 | R | partial or cov, cov 4 lines, uniq 0 |
| `test_isolation_covers_every_installed_family_member` | 1 | R | partial or cov, cov 185 lines, uniq 0 |
| `test_a_listener_start_refuses_a_shared_family_identity` | 1 | R | partial or cov, cov 185 lines, uniq 0 |
| `test_doctor_fails_a_memory_workspace_with_local_cards_or_hub_off` | 1 | R | partial or cov, cov 202 lines, uniq 0 |
| `test_listener_start_sweeps_leftover_token_copies` | 1 | R | partial or cov, cov 194 lines, uniq 0 |
| `test_serve_never_falls_through_to_another_pack` | 1 | F | Dropped the vacuous issubclass(ConfigurationError, Exception) assertion and its import. |
| `test_runbook_grants_the_memory_principal_read_on_fleet_and_handoff` | 1 | R | partial or cov, cov no context recorded (subprocess or class-scoped) |
| `test_inbox_runbook_names_the_send_grant_step` | 1 | R | partial or cov, cov no context recorded (subprocess or class-scoped) |
| `test_vault_backed_tools_are_retired` | 1 | R | partial or cov, cov 131 lines, uniq 0 |

## L7 T3 dispatch bridge (owner: grokbot_t3/*)

Files: `test_grokbot_t3_contracts.py`, `test_grokbot_t3_fleet.py`, `test_grokbot_t3_inbox.py`, `test_grokbot_t3_intake.py`, `test_grokbot_t3_ledger.py`, `test_grokbot_t3_lifecycle.py`, `test_grokbot_t3_platform.py`, `test_grokbot_t3_runtime.py`, `test_grokbot_t3_tools.py`, `test_grokbot_t3_watch.py`. Declarations at base: 237.

### test_grokbot_t3_contracts.py (13 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_tool_set_is_closed` | 1 | R | cov, no context recorded (subprocess or class-scoped) |
| `test_submit_defaults` | 1 | R | cov, 36 lines, uniq 0 |
| `test_submit_optional_fields` | 1 | R | cov, 44 lines, uniq 0 |
| `test_submit_rejects` | 12 | R | cov, 48 lines, uniq 3 |
| `test_submit_rejects_missing_and_non_object` | 1 | R | cov, 20 lines, uniq 1 |
| `test_wait_bounds` | 1 | R | cov, 25 lines, uniq 0 |
| `test_answer_and_approve` | 1 | R | cov, 39 lines, uniq 5 |
| `test_recent_defaults_and_bounds` | 1 | R | cov, 27 lines, uniq 1 |
| `test_public_error_shape` | 1 | R | cov, 20 lines, uniq 1 |
| `test_lone_surrogates_are_rejected` | 1 | R | cov, 39 lines, uniq 2 |
| `test_error_fields_are_bounded_and_plain` | 1 | R | cov, 17 lines, uniq 0 |
| `test_hub_contract_bounds` | 12 | R | cov, 37 lines, uniq 0 |
| `test_hub_defaults_and_shared_send_fields` | 1 | R | cov, 25 lines, uniq 0 |

### test_grokbot_t3_fleet.py (15 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_submit_sends_prompt_on_stdin_only` | 1 | R | cov, 38 lines, uniq 0 |
| `test_override_reason_is_sent_when_given` | 1 | R | cov, 39 lines, uniq 1 |
| `test_error_envelope_raises_fleet_call_error` | 1 | R | cov, 35 lines, uniq 9 |
| `test_garbage_output_is_internal_error` | 1 | R | cov, 26 lines, uniq 2 |
| `test_undecodable_output_is_a_protocol_error` | 1 | R | cov, 24 lines, uniq 2 |
| `test_valid_result_with_nonzero_exit_keeps_request_id` | 1 | R | cov, 29 lines, uniq 2 |
| `test_missing_binary_is_definite_failure_not_protocol` | 1 | R | cov, 23 lines, uniq 1 |
| `test_timeout_raises_fleet_timeout` | 1 | R | cov, 18 lines, uniq 1 |
| `test_wait_passes_timeout_and_extends_subprocess_timeout` | 1 | R | cov, 27 lines, uniq 2 |
| `test_answer_approve_followup_interrupt_payloads` | 1 | R | cov, 36 lines, uniq 5 |
| `test_resolve_revision` | 1 | R | cov, 19 lines, uniq 1 |
| `test_resolve_revision_failures` | 3 | R | cov, 19 lines, uniq 3 |
| `test_runtime_and_followup_interaction_modes_are_configurable` | 1 | R | cov, 44 lines, uniq 0 |
| `test_child_env_preserves_fleet_config_without_unrelated_secrets` | 1 | R | cov, 1 lines, uniq 0 |
| `test_nested_result_envelope_is_flattened` | 1 | R | cov, 28 lines, uniq 3 |

### test_grokbot_t3_inbox.py (64 declarations, partial)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_missing_hub_config_is_closed_without_breaking_dispatch` | n | R | partial or cov, cov 191 lines, uniq 0 |
| `test_hub_tools_list_ack_reply_and_status` | 1 | R | partial or cov, cov 281 lines, uniq 0 |
| `test_status_hides_infrastructure_identities_and_preserves_metadata` | 6 | R | partial or cov, cov 228 lines, uniq 0 |
| `test_status_maps_only_exact_raw_destinations_in_mixed_history` | 1 | R | partial or cov, cov 206 lines, uniq 0 |
| `test_send_accepts_configured_alias_and_preserves_client_id` | 2 | R | partial or cov, cov 204 lines, uniq 0 |
| `test_security_message_scope_change_during_request_closes_result` | 27 | R | partial or cov, cov 223 lines, uniq 0 |
| `test_security_message_scope_checks_loaded_transport_pair` | 12 | R | partial or cov, cov 222 lines, uniq 0 |
| `test_security_status_transport_failure_still_checks_current_scope` | 2 | R | partial or cov, cov 146 lines, uniq 0 |
| `test_security_message_projection_scope_change_closes_complete_operation` | 42 | R | partial or cov, cov 224 lines, uniq 0 |
| `test_security_send_scope_is_captured_before_clock_and_recipient_resolution` | 12 | R | partial or cov, cov 217 lines, uniq 0 |
| `test_hub_tools_independent_of_dispatch_enabled` | n | R | partial or cov, cov 251 lines, uniq 0 |
| `test_grant_code_and_raw_secret_errors_are_safe` | 1 | R | partial or cov, cov 223 lines, uniq 0 |
| `test_body_byte_cap_and_unknown_fields_rejected_before_transport` | 1 | R | partial or cov, cov 186 lines, uniq 0 |
| `test_principal_credential_is_owner_only_and_errors_are_closed` | 3 | R | partial or cov, cov 198 lines, uniq 0 |
| `test_hub_wake_retries_failed_target_and_persists_only_success_cursor` | 1 | R | partial or cov, cov 303 lines, uniq 0 |
| `test_request_wakes_survive_hub_failure_and_use_selected_hub_target` | 1 | R | partial or cov, cov 372 lines, uniq 1 |
| `test_hub_poll_bounds_pagination_and_deadline` | 2 | R | partial or cov, cov 296 lines, uniq 0 |
| `test_overlapping_hub_wakes_cannot_regress_success_cursor` | 1 | R | partial or cov, cov 302 lines, uniq 0 |
| `test_malformed_hub_status_is_a_closed_error` | 1 | R | partial or cov, cov 215 lines, uniq 0 |
| `test_non_protocol_hub_code_is_omitted` | 1 | R | partial or cov, cov 222 lines, uniq 0 |
| `test_review_failed_wake_does_not_starve_other_target_and_survives_ack` | 1 | R | partial or cov, cov 305 lines, uniq 0 |
| `test_review_inbox_projection_fits_public_bytes_and_can_progress` | 3 | R | partial or cov, cov 262 lines, uniq 0 |
| `test_review_list_and_ack_totals_require_complete_scan` | 2 | R | partial or cov, cov 259 lines, uniq 0 |
| `test_review_incomplete_history_labels_retained_window` | 1 | R | partial or cov, cov 235 lines, uniq 2 |
| `test_review_partial_inbox_and_ack_survive_recount_failure` | 1 | R | partial or cov, cov 287 lines, uniq 1 |
| `test_review_watcher_drains_queued_mutations_only_when_messaging_enabled` | 8 | R | partial or cov, cov 303 lines, uniq 0 |
| `test_review_flush_and_log_failures_leave_inbox_and_request_targets_working` | 1 | R | partial or cov, cov 364 lines, uniq 2 |
| `test_hub_pending_metadata_survives_crash_and_ack` | 2 | R | partial or cov, cov 304 lines, uniq 0 |
| `test_hub_success_cursor_and_batch_retirement_are_atomic` | 1 | R | partial or cov, cov 163 lines, uniq 0 |
| `test_security_rotation_discards_foreground_scan` | 3 | R | partial or cov, cov 242 lines, uniq 0 |
| `test_security_rotation_keeps_wake_scope_consistent` | 8 | R | partial or cov, cov 307 lines, uniq 0 |
| `test_security_ack_deadline_preserves_confirmed_results` | 3 | R | partial or cov, cov 223 lines, uniq 2 |
| `test_security_ack_recount_shares_batch_deadline` | 2 | R | partial or cov, cov 254 lines, uniq 0 |
| `test_security_first_ack_refusal_stays_closed` | 2 | R | partial or cov, cov 229 lines, uniq 0 |
| `test_security_ack_rotation_closes_batch_after_request` | 8 | R | partial or cov, cov 228 lines, uniq 0 |
| `test_security_ack_checks_loaded_transport_credential` | 9 | R | partial or cov, cov 254 lines, uniq 0 |
| `test_security_ack_rotation_during_recount_closes_result` | 2 | R | partial or cov, cov 253 lines, uniq 0 |
| `test_notification_scope_matches_transport_endpoint_and_frames_identity` | 1 | R | partial or cov, cov 138 lines, uniq 0 |
| `test_notification_scope_settings_failure_is_closed` | 2 | R | partial or cov, cov 129 lines, uniq 0 |
| `test_security_ack_endpoint_switch_closes_batch` | 4 | R | partial or cov, cov 227 lines, uniq 0 |
| `test_security_inbox_checks_loaded_endpoint` | 10 | R | partial or cov, cov 253 lines, uniq 0 |
| `test_security_ack_endpoint_switch_during_recount` | 2 | R | partial or cov, cov 253 lines, uniq 0 |
| `test_security_endpoint_switch_discards_list_pages` | 3 | R | partial or cov, cov 242 lines, uniq 0 |
| `test_hub_wake_endpoint_and_legacy_histories_are_isolated` | 3 | R | partial or cov, cov 304 lines, uniq 0 |
| `test_pages_capture_at_invocation_before_iteration` | 1 | R | partial or cov, cov 155 lines, uniq 0 |
| `test_transient_authority_failure_ends_hub_wake_invocation_but_not_request_wakes` | 4 | R | partial or cov, cov 376 lines, uniq 0 |
| `test_identity_proof_precedes_messages_and_fails_closed` | n | R | partial or cov, cov 251 lines, uniq 0 |
| `test_every_operation_reproves_identity_after_between_request_rotation` | 2 | R | partial or cov, cov 188 lines, uniq 0 |
| `test_rotation_during_identity_proof_never_reaches_message_transport` | n | R | partial or cov, cov 214 lines, uniq 0 |
| `test_identity_and_every_message_call_share_exact_authority` | n | R | partial or cov, cov 251 lines, uniq 0 |
| `test_configured_uuid_alias_is_rejected_before_client` | 2 | R | partial or cov, cov 183 lines, uniq 0 |
| `test_enabled_startup_keeps_t3_available_while_hub_operations_fail_closed` | 5 | R | partial or cov, cov 270 lines, uniq 0 |
| `test_doctor_and_canary_still_report_unproved_hub_identity` | 3 | R | partial or cov, cov 236 lines, uniq 0 |
| `test_reads_and_identity_never_access_node_credentials_or_spool` | 2 | R | partial or cov, cov 235 lines, uniq 0 |
| `test_watch_lifecycle_identity_failure_preserves_request_wakes` | 1 | R | partial or cov, cov 304 lines, uniq 8 |
| `test_revoked_hub_authority_discards_partial_results` | 4 | R | partial or cov, cov 242 lines, uniq 0 |
| `test_public_inbox_cannot_accept_an_unproved_authority` | 4 | R | partial or cov, cov 127 lines, uniq 0 |
| `test_foreground_send_waits_boundedly_and_never_sends_after_timeout` | 1 | R | partial or cov, cov 222 lines, uniq 0 |
| `test_foreground_send_does_not_scan_other_spool_scopes` | 2 | R | partial or cov, cov 204 lines, uniq 0 |
| `test_send_lock_timeout_validated_before_authority_or_spool` | 5 | R | partial or cov, cov 127 lines, uniq 0 |
| `test_canary_proves_bounded_hub_identity_at_real_transport` | 8 | R | partial or cov, cov 167 lines, uniq 0 |
| `test_watch_private_nesting_reuses_one_proven_authority` | 1 | R | partial or cov, cov 296 lines, uniq 0 |
| `test_sdk_message_strings_reach_handler_and_transport_unchanged` | 5 | R | partial or cov, cov 382 lines, uniq 0 |
| `test_public_poll_deadline_includes_identity` | 4 | R | partial or cov, cov 186 lines, uniq 0 |

### test_grokbot_t3_intake.py (20 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_disabled_does_not_call_gh` | 2 | R | cov, 95 lines, uniq 0 |
| `test_no_headroom` | 1 | R | cov, 162 lines, uniq 2 |
| `test_headroom_rechecked_between_submissions` | 1 | R | cov, 457 lines, uniq 2 |
| `test_snapshot_filters_comments_using_latest_matching_approval` | 1 | R | cov, 456 lines, uniq 0 |
| `test_snapshot_refusal_releases_claim_and_continues` | 9 | R | cov, 465 lines, uniq 4 |
| `test_snapshot_failure_releases_claim` | 6 | R | cov, 229 lines, uniq 1 |
| `test_snapshot_requires_all_fields_and_types` | 60 | R | cov, 266 lines, uniq 6 |
| `test_oversized_snapshot_releases_claim_and_continues` | 1 | R | cov, 472 lines, uniq 0 |
| `test_happy_path_uses_snapshot_and_swaps_labels` | 1 | R | cov, 459 lines, uniq 0 |
| `test_prompt_contract` | 2 | R | cov, 9 lines, uniq 0 |
| `test_limit_and_ascending_issue_order` | 1 | R | cov, 456 lines, uniq 0 |
| `test_seat_label` | 1 | R | cov, 458 lines, uniq 1 |
| `test_bad_seat_labels_release_claim` | 3 | R | cov, 280 lines, uniq 0 |
| `test_held_claim_skips_issue` | 1 | R | cov, 200 lines, uniq 0 |
| `test_failed_label_swap_keeps_claim_on_next_pass` | 1 | R | cov, 457 lines, uniq 1 |
| `test_submit_errors_handle_claims_and_stop` | 14 | R | cov, 471 lines, uniq 1 |
| `test_repository_failure_continues_without_exposing_output` | 8 | R | cov, 460 lines, uniq 5 |
| `test_rename_in_approval_second_is_refused` | 1 | R | cov, 274 lines, uniq 0 |
| `test_deadline_stops_before_claiming_an_issue_that_cannot_finish` | 1 | R | cov, 457 lines, uniq 2 |
| `test_deadline_skips_listing_without_time_for_one_issue` | 1 | R | cov, 160 lines, uniq 2 |

### test_grokbot_t3_ledger.py (17 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_file_is_owner_only_and_round_trips` | 1 | R | cov, 54 lines, uniq 0 |
| `test_open_count_and_daily_count` | 1 | R | cov, 64 lines, uniq 3 |
| `test_state_change_clears_notification` | 1 | R | cov, 72 lines, uniq 0 |
| `test_pending_notifications` | 1 | R | cov, 65 lines, uniq 0 |
| `test_recent_projection` | 1 | R | cov, 63 lines, uniq 0 |
| `test_existing_requests_seed_attempt_count_once` | 1 | R | cov, 64 lines, uniq 0 |
| `test_existing_parent_mode_is_preserved` | 1 | R | cov, 47 lines, uniq 0 |
| `test_existing_writable_parent_is_rejected_without_chmod` | 1 | R | cov, 18 lines, uniq 0 |
| `test_existing_parent_must_be_owned_by_current_user` | 1 | R | cov, 18 lines, uniq 0 |
| `test_update_state_resets_status_failures_even_without_a_transition` | 1 | R | cov, 78 lines, uniq 0 |
| `test_stale_notification_cannot_mark_a_newer_state` | 1 | R | cov, 80 lines, uniq 0 |
| `test_existing_delivery_identifiers_are_hashed_once_on_open` | 1 | R | cov, 70 lines, uniq 5 |
| `test_legacy_requests_default_to_default_interaction_mode` | 1 | R | cov, 62 lines, uniq 0 |
| `test_pending_notifications_puts_terminal_rows_first_then_least_recently_polled` | 1 | R | cov, 65 lines, uniq 0 |
| `test_legacy_state_revision_migrates_and_survives_reopen` | 1 | R | cov, 74 lines, uniq 0 |
| `test_record_submit_converts_the_reservation_atomically` | 1 | R | cov, 68 lines, uniq 0 |
| `test_intake_claim_is_exclusive_and_releasable_across_connections` | 1 | R | cov, 57 lines, uniq 0 |

### test_grokbot_t3_lifecycle.py (17 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_gate_requires_bearer_and_allowed_host` | 1 | R | cov, 24 lines, uniq 4 |
| `test_repr_hides_bearer` | 1 | R | cov, 1 lines, uniq 1 |
| `test_invalid_tool_request` | 6 | R | cov, 30 lines, uniq 2 |
| `test_render_units` | 1 | R | cov, 143 lines, uniq 40 |
| `test_write_watch_units_refuses_overwrite` | 1 | R | cov, 70 lines, uniq 4 |
| `test_authenticated_mcp_submit_and_recent_cross_threads` | 1 | R | cov, 488 lines, uniq 18 |
| `test_doctor_checks_wake_ledger_and_explicit_write_paths` | 3 | R | cov, 189 lines, uniq 3 |
| `test_tools_factory_explicitly_selects_full_access` | 1 | R | cov, 146 lines, uniq 0 |
| `test_render_intake_units` | 1 | R | cov, 113 lines, uniq 14 |
| `test_write_intake_units_refuses_overwrite` | 1 | R | cov, 70 lines, uniq 0 |
| `test_intake_once_wires_bounded_gh_runner` | 3 | R | cov, 125 lines, uniq 4 |
| `test_intake_cli_prints_summary` | 2 | R | cov, 35 lines, uniq 11 |
| `test_install_intake_cli` | 1 | R | cov, 83 lines, uniq 4 |
| `test_canary_requires_exact_runtime_tool_inventory` | 6 | R | cov, 97 lines, uniq 0 |
| `test_registration_uses_runtime_inventory_and_alias_contract` | 4 | R | cov, 160 lines, uniq 0 |
| `test_installed_sdk_publishes_closed_constrained_hub_schemas` | 1 | R | cov, 277 lines, uniq 2 |
| `test_sdk_hub_calls_distinguish_null_missing_and_invalid_values` | 1 | R | cov, 308 lines, uniq 6 |

### test_grokbot_t3_platform.py (5 declarations, read in full)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_windows_storage_refuses_before_touching_disk` | 6 | R | read, contract named by the test, cov 13 lines, uniq 0 |
| `test_windows_cli_reports_platform_before_loading_config` | 4 | R | read, contract named by the test, cov 33 lines, uniq 0 |
| `test_windows_lifecycle_refuses_before_loading_instance` | 4 | R | read, contract named by the test, cov 14 lines, uniq 0 |
| `test_windows_public_cli_explains_platform` | 4 | R | read, contract named by the test, cov 217 lines, uniq 9 |
| `test_windows_install_service_keeps_the_platform_error` | 2 | R | read, contract named by the test, cov 163 lines, uniq 13 |

### test_grokbot_t3_runtime.py (16 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_loads_valid_runtime` | 1 | R | cov, 85 lines, uniq 0 |
| `test_rejects_group_readable_file` | 1 | R | cov, 24 lines, uniq 1 |
| `test_rejects_symlink` | 1 | R | cov, 20 lines, uniq 0 |
| `test_rejects_invalid_runtime` | 21 | R | cov, 95 lines, uniq 4 |
| `test_fallback_seat_may_be_null` | 1 | R | cov, 85 lines, uniq 0 |
| `test_validate_runtime_reference_returns_path` | 1 | R | cov, 100 lines, uniq 0 |
| `test_intake_is_optional` | 1 | R | cov, 69 lines, uniq 0 |
| `test_intake_policy_preserves_repository_order` | 1 | R | cov, 90 lines, uniq 0 |
| `test_rejects_invalid_intake` | 15 | R | cov, 101 lines, uniq 4 |
| `test_intake_rejects_non_github_remote` | 1 | R | cov, 92 lines, uniq 1 |
| `test_hub_aliases_default_empty_and_preserve_node_id` | 1 | R | cov, 78 lines, uniq 0 |
| `test_messaging_defaults_disabled` | 1 | R | cov, 69 lines, uniq 0 |
| `test_messaging_enable_requires_explicit_principal_and_token` | 1 | R | cov, 82 lines, uniq 2 |
| `test_messaging_runtime_rejects_unsafe_optional_configuration` | 6 | R | cov, 69 lines, uniq 0 |
| `test_runtime_rejects_uuid_alias_keys` | 2 | R | cov, 62 lines, uniq 0 |
| `test_hub_recipient_principals_are_exact_immutable_config_values` | 3 | R | cov, 76 lines, uniq 0 |

### test_grokbot_t3_tools.py (44 declarations, partial)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_submit_picks_host_default_seat_and_records` | 1 | R | partial or cov, cov 288 lines, uniq 0 |
| `test_explicit_host_and_base_ref` | 1 | R | partial or cov, cov 274 lines, uniq 1 |
| `test_enrolled_picks_allowlisted_host_and_override_reason` | 1 | R | partial or cov, cov 285 lines, uniq 0 |
| `test_fallback_seat_on_admission_denied` | 1 | R | partial or cov, cov 301 lines, uniq 0 |
| `test_named_seat_never_falls_back` | 1 | R | partial or cov, cov 291 lines, uniq 0 |
| `test_submit_refusals` | 5 | R | partial or cov, cov 202 lines, uniq 3 |
| `test_open_and_daily_limits` | 1 | R | partial or cov, cov 323 lines, uniq 0 |
| `test_no_eligible_host_and_unresolved_ref` | 1 | R | partial or cov, cov 241 lines, uniq 4 |
| `test_unreadable_submit_output_is_unknown_not_refused` | 1 | R | partial or cov, cov 281 lines, uniq 0 |
| `test_unreadable_submit_output_with_request_id_is_recorded` | 1 | R | partial or cov, cov 311 lines, uniq 0 |
| `test_submit_timeout_is_unknown` | 1 | R | partial or cov, cov 272 lines, uniq 0 |
| `test_status_requires_ledger_entry_and_projects` | 1 | R | partial or cov, cov 337 lines, uniq 1 |
| `test_followup_answer_approve_interrupt_wait_recent` | 1 | R | partial or cov, cov 393 lines, uniq 12 |
| `test_capacity_filters_to_allowlist` | 1 | R | partial or cov, cov 150 lines, uniq 0 |
| `test_unknown_tool_and_bad_args` | 1 | R | partial or cov, cov 162 lines, uniq 1 |
| `test_concurrent_submits_cannot_exceed_limits` | 2 | R | partial or cov, cov 303 lines, uniq 0 |
| `test_unknown_submit_with_handle_is_recorded` | 2 | R | partial or cov, cov 322 lines, uniq 0 |
| `test_timed_out_submit_consumes_daily_attempt` | 1 | R | partial or cov, cov 273 lines, uniq 0 |
| `test_refused_submit_and_fallback_each_consume_daily_attempt` | 1 | R | partial or cov, cov 292 lines, uniq 0 |
| `test_disabled_blocks_answer_and_approve_but_allows_interrupt` | 2 | R | partial or cov, cov 347 lines, uniq 0 |
| `test_followup_cannot_reopen_past_open_limit` | 1 | R | partial or cov, cov 331 lines, uniq 1 |
| `test_followup_consumes_daily_attempt_even_on_timeout` | 1 | R | partial or cov, cov 349 lines, uniq 0 |
| `test_admission_error_with_handle_never_falls_back` | 1 | R | partial or cov, cov 312 lines, uniq 0 |
| `test_contended_dispatch_returns_unavailable_without_launching` | 2 | R | partial or cov, cov 307 lines, uniq 2 |
| `test_followup_preserves_submitted_interaction_mode_after_reopen` | 2 | R | partial or cov, cov 350 lines, uniq 0 |
| `test_followup_timeout_on_terminal_request_reopens_as_unknown` | 1 | R | partial or cov, cov 350 lines, uniq 0 |
| `test_prefixed_title_is_capped_for_t3_fleet` | 1 | R | partial or cov, cov 285 lines, uniq 0 |
| `test_timed_out_submit_reserves_an_open_slot_until_expiry` | 1 | R | partial or cov, cov 273 lines, uniq 0 |
| `test_unreadable_submit_without_id_reserves_an_open_slot` | 1 | R | partial or cov, cov 282 lines, uniq 0 |
| `test_success_envelope_without_request_id_reserves_a_slot` | 1 | R | partial or cov, cov 272 lines, uniq 1 |
| `test_slot_is_reserved_while_t3_fleet_submits` | 1 | R | partial or cov, cov 285 lines, uniq 0 |
| `test_definite_refusal_releases_the_reservation` | 1 | R | partial or cov, cov 287 lines, uniq 0 |
| `test_stateless_followup_on_terminal_request_becomes_unknown` | 1 | R | partial or cov, cov 350 lines, uniq 2 |
| `test_daily_limit_on_fallback_releases_the_reservation` | 1 | R | partial or cov, cov 292 lines, uniq 0 |
| `test_terminal_followup_is_open_while_fleet_runs` | 1 | R | partial or cov, cov 366 lines, uniq 0 |
| `test_status_does_not_overwrite_a_newer_state` | 1 | R | partial or cov, cov 318 lines, uniq 0 |
| `test_status_during_followup_cannot_clobber_the_reopen` | 1 | C | Row prior_state=succeeded of test_status_during_followup_cannot_clobber_the_reopen_or_the_new_turn. M7 kills. |
| `test_status_during_running_followup_cannot_clobber_the_new_turn` | 1 | C | Row prior_state=running of the same test. M7 kills. |
| `test_refused_running_followup_restores_the_prior_state` | 1 | R | partial or cov, cov 366 lines, uniq 0 |
| `test_stale_reopening_marker_can_be_replaced_by_pollers` | 1 | R | partial or cov, cov 300 lines, uniq 0 |
| `test_followup_retries_the_reopen_marker_after_a_racing_poll` | 1 | R | partial or cov, cov 341 lines, uniq 0 |
| `test_followup_does_not_launch_without_the_reopen_marker` | 1 | R | partial or cov, cov 345 lines, uniq 1 |
| `test_disabled_messaging_excludes_hub_without_constructing_client` | 2 | R | partial or cov, cov 178 lines, uniq 0 |
| `test_invalid_alias_never_constructs_client_or_uses_clock_or_dispatch_budget` | 7 | R | partial or cov, cov 191 lines, uniq 0 |

### test_grokbot_t3_watch.py (26 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_wakes_on_terminal_state_once` | 1 | R | cov, 205 lines, uniq 0 |
| `test_no_wake_while_running` | 1 | R | cov, 142 lines, uniq 0 |
| `test_retry_after_non_200` | 1 | R | cov, 207 lines, uniq 0 |
| `test_second_question_wakes_again` | 1 | R | cov, 206 lines, uniq 0 |
| `test_unknown_caller_uses_default_and_status_failure_counts` | 1 | R | cov, 220 lines, uniq 0 |
| `test_rejects_bad_wake_config` | 4 | R | cov, 41 lines, uniq 4 |
| `test_post_wake_refuses_redirect_and_proxy` | 1 | R | cov, 12 lines, uniq 2 |
| `test_wake_config_requires_default_target` | 1 | R | cov, 29 lines, uniq 1 |
| `test_wake_gives_up_after_30_failures_across_restarts` | 1 | R | cov, 177 lines, uniq 0 |
| `test_30_consecutive_status_failures_wake_unknown_once` | 1 | R | cov, 234 lines, uniq 0 |
| `test_multiple_targets_retry_only_failed_delivery_and_reset_on_state_change` | 1 | R | cov, 217 lines, uniq 0 |
| `test_garbage_http_wake_is_bounded_and_later_rows_continue` | 1 | R | cov, 186 lines, uniq 2 |
| `test_unexpected_row_failure_is_counted_and_later_rows_continue` | 1 | R | cov, 177 lines, uniq 0 |
| `test_exhausted_unknown_rows_recover_every_fifteenth_tick_across_restarts` | 1 | R | cov, 251 lines, uniq 1 |
| `test_delivered_targets_store_only_url_hashes` | 1 | R | cov, 170 lines, uniq 0 |
| `test_unexpected_wake_failure_preserves_delivered_targets_and_retry_cap` | 1 | R | cov, 159 lines, uniq 0 |
| `test_row_log_failure_cannot_bypass_wake_retry_cap` | 1 | R | cov, 170 lines, uniq 0 |
| `test_pass_budget_defers_remaining_rows` | 1 | R | cov, 144 lines, uniq 0 |
| `test_polling_rotates_so_later_rows_are_reached` | 1 | R | cov, 144 lines, uniq 0 |
| `test_watcher_does_not_overwrite_a_newer_state` | 1 | R | cov, 142 lines, uniq 0 |
| `test_stale_terminal_poll_cannot_close_a_newer_running_request` | 2 | R | cov, 156 lines, uniq 0 |
| `test_reopened_terminal_row_gets_no_stale_wake` | 1 | R | cov, 142 lines, uniq 0 |
| `test_reopen_between_targets_stops_the_remaining_wakes` | 1 | R | cov, 152 lines, uniq 0 |
| `test_pass_budget_leaves_room_for_the_slowest_row` | 1 | R | cov, no context recorded (subprocess or class-scoped) |
| `test_flush_lock_wait_respects_unit_budget_after_request_wakes` | 5 | R | cov, 204 lines, uniq 0 |
| `test_disabled_messaging_makes_zero_background_hub_calls` | 2 | R | cov, 181 lines, uniq 0 |

## L8 Fleet Steward bridge (owner: grokbot_fleet/*)

Files: `test_grokbot_fleet_actions.py`, `test_grokbot_fleet_app.py`, `test_grokbot_fleet_contracts.py`, `test_grokbot_fleet_exec.py`, `test_grokbot_fleet_ledger.py`, `test_grokbot_fleet_lifecycle.py`, `test_grokbot_fleet_normalize.py`, `test_grokbot_fleet_probes.py`, `test_grokbot_fleet_registry.py`, `test_grokbot_fleet_remediation.py`, `test_grokbot_fleet_runtime_config.py`, `test_grokbot_fleet_tools.py`. Declarations at base: 100.

### test_grokbot_fleet_actions.py (16 declarations, partial)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_executable_remediation_is_narrow` | 1 | R | partial or cov, cov 27 lines, uniq 25 |
| `test_action_store_is_exclusive_and_replay_safe` | 1 | R | partial or cov, cov 223 lines, uniq 1 |
| `test_action_store_concurrency_serializes_claims` | 1 | R | partial or cov, cov 216 lines, uniq 0 |
| `test_restarter_observes_and_restarts_with_fixed_argv` | 1 | R | partial or cov, cov 95 lines, uniq 70 |
| `test_action_store_fails_closed_on_corrupt_proposal_and_symlink` | 1 | R | partial or cov, cov 157 lines, uniq 6 |
| `test_action_store_caps_proposals_and_preserves_live_claim` | 1 | R | partial or cov, cov 247 lines, uniq 0 |
| `test_action_store_caps_live_proposals_by_bytes` | 1 | R | partial or cov, cov 169 lines, uniq 0 |
| `test_action_store_caps_receipts_and_dedupes_identical_rejections` | 1 | R | partial or cov, cov 219 lines, uniq 1 |
| `test_action_store_partial_and_zero_writes_fail_and_do_not_consume` | 1 | R | partial or cov, cov 225 lines, uniq 13 |
| `test_action_store_caps_consumed_and_preserves_live_claim` | 1 | R | partial or cov, cov 252 lines, uniq 2 |
| `test_action_store_fails_closed_on_orphaned_consumed` | 1 | R | partial or cov, cov 205 lines, uniq 1 |
| `test_receipt_reservation_releases_and_does_not_pin_capacity` | 1 | R | partial or cov, cov 313 lines, uniq 3 |
| `test_live_reservation_is_exclusive_and_token_scoped` | 1 | R | partial or cov, cov 288 lines, uniq 6 |
| `test_receipt_reservation_rejects_a_changed_static_contract` | 1 | R | partial or cov, cov 229 lines, uniq 1 |
| `test_receipt_reservation_recovers_only_exact_partially_persisted_receipts` | 1 | R | partial or cov, cov 299 lines, uniq 2 |
| `test_claim_cleanup_replaces_a_provisional_receipt_with_the_terminal_set` | 1 | R | partial or cov, cov 165 lines, uniq 1 |

### test_grokbot_fleet_app.py (3 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_request_gate_rejects_missing_bearer_and_unknown_origin` | 1 | R | cov, 52 lines, uniq 34 |
| `test_invalid_fleet_tool_request_is_closed_and_exact` | 1 | R | cov, 36 lines, uniq 25 |
| `test_lifecycle_rejects_unsafe_runtime_before_tools` | 1 | R | cov, 45 lines, uniq 4 |

### test_grokbot_fleet_contracts.py (6 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_opaque_id_accepts_exactly_32_lowercase_hex` | 1 | R | cov, 2 lines, uniq 0 |
| `test_opaque_id_rejects_uppercase_short_long_and_non_hex` | 1 | R | cov, 5 lines, uniq 0 |
| `test_identifier_accepts_boundaries_and_catalog_names` | 1 | R | cov, 2 lines, uniq 0 |
| `test_identifier_rejects_unsafe_values` | 1 | R | cov, 5 lines, uniq 0 |
| `test_strict_tool_inputs_accept_and_reject_extra_keys` | 1 | R | cov, 25 lines, uniq 3 |
| `test_wazuh_finding_id_parser_accepts_128_and_rejects_129` | 1 | R | cov, 9 lines, uniq 0 |

### test_grokbot_fleet_exec.py (4 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_run_exec_rejects_out_of_range_bounds` | 1 | R | cov, 8 lines, uniq 2 |
| `test_run_exec_times_out_and_reaps_descendants` | 1 | R | cov, 98 lines, uniq 5 |
| `test_run_exec_overflows_without_leaking_output` | 1 | R | cov, 101 lines, uniq 7 |
| `test_run_exec_allowlists_environment` | 1 | R | cov, 21 lines, uniq 0 |

### test_grokbot_fleet_ledger.py (8 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_ledger_creates_owner_only_dir_and_file` | 1 | R | cov, 124 lines, uniq 0 |
| `test_windows_ensure_state_dir_fails_closed_before_io` | 1 | R | cov, 10 lines, uniq 0 |
| `test_ledger_marks_change_and_retains_only_the_bound` | 1 | R | cov, 132 lines, uniq 3 |
| `test_ledger_replaces_only_the_requested_scope` | 1 | R | cov, 110 lines, uniq 0 |
| `test_findings_returns_sorted_detached_copies` | 1 | R | cov, 115 lines, uniq 0 |
| `test_ledger_fails_closed_on_unsafe_permissions` | 1 | R | cov, 19 lines, uniq 1 |
| `test_ledger_fails_closed_on_corruption_and_symlink` | 1 | R | cov, 41 lines, uniq 3 |
| `test_ledger_partial_and_zero_writes_preserve_prior_file` | 1 | R | cov, 136 lines, uniq 9 |

### test_grokbot_fleet_lifecycle.py (16 declarations, partial)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_disjoint_paths_reject_overlap_dots_and_relative` | 1 | R | partial or cov, cov 37 lines, uniq 0 |
| `test_absolute_reference_accepts_posix_and_drive_roots_and_rejects_unc` | 1 | R | partial or cov, cov 33 lines, uniq 0 |
| `test_paths_overlap_detects_backslash_nesting` | 1 | C | Absorbed by test_paths_overlap_on_windows_nests_on_backslashes_and_ignores_case (rows backslash-parent, backslash-child). M9a kills. |
| `test_paths_overlap_treats_backslash_as_literal_on_posix` | 1 | R | partial or cov, cov 9 lines, uniq 0 |
| `test_paths_overlap_is_case_insensitive_on_windows` | 1 | C | Absorbed by the same table (rows case-differs-child, case-differs-parent). M9a kills. |
| `test_paths_overlap_handles_root_operand_and_rejects_trailing_alias` | 1 | R | partial or cov, cov 40 lines, uniq 2 |
| `test_disjoint_paths_reject_backslash_nested_drive_paths` | 1 | R | partial or cov, cov 12 lines, uniq 0 |
| `test_absolute_reference_rejects_non_ascii_drive_letter` | 1 | R | partial or cov, cov 17 lines, uniq 0 |
| `test_pack_setup_doctor_canary_and_unit_hide_secrets` | 1 | R | partial or cov, cov 698 lines, uniq 38 |
| `test_unit_rendering_does_not_resolve_secret_values` | 1 | R | partial or cov, cov 435 lines, uniq 0 |
| `test_successful_canary_does_not_mutate_ledger_findings_or_action_state` | 1 | R | partial or cov, cov 546 lines, uniq 11 |
| `test_runtime_and_state_paths_reject_symlinks` | 1 | R | partial or cov, cov 52 lines, uniq 7 |
| `test_fleet_unit_uses_shared_listener_recovery_policy` | 1 | R | partial or cov, cov 435 lines, uniq 0 |
| `test_windows_branch_rejects_drive_less_roots` | 1 | R | partial or cov, cov 31 lines, uniq 0 |
| `test_windows_disjoint_paths_reject_drive_less_alias_of_drive_path` | 1 | R | partial or cov, cov 55 lines, uniq 5 |
| `test_fleet_private_reads_fail_closed_with_the_read_error` | 1 | R | partial or cov, cov 33 lines, uniq 0 |

### test_grokbot_fleet_normalize.py (2 declarations, read in full)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_sanitize_detail_redacts_secrets_uris_addresses_and_paths` | 1 | R | read, contract named by the test, cov 22 lines, uniq 0 |
| `test_normalize_host_observation_maps_classes_and_filters_fields` | 1 | R | read, contract named by the test, cov 69 lines, uniq 6 |

### test_grokbot_fleet_probes.py (4 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_local_host_probe_uses_fixed_systemctl_argv_and_cwd` | 1 | R | cov, 65 lines, uniq 44 |
| `test_ssh_probe_uses_fixed_argv_and_parses_summary` | 1 | R | cov, 71 lines, uniq 45 |
| `test_service_probe_maps_active_states` | 1 | R | cov, 67 lines, uniq 33 |
| `test_service_probe_rejects_unknown_unit_without_leaking_it` | 1 | R | cov, 12 lines, uniq 2 |

### test_grokbot_fleet_registry.py (3 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_registry_rejects_duplicate_and_inconsistent_records` | 1 | R | cov, 49 lines, uniq 5 |
| `test_registry_looks_up_targets_services_and_scopes` | 1 | R | cov, 63 lines, uniq 1 |
| `test_authorization_and_public_fields` | 1 | R | cov, 62 lines, uniq 0 |

### test_grokbot_fleet_remediation.py (21 declarations, partial)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_proposal_requires_escalated_current_finding_and_operator_approval` | 1 | R | partial or cov, cov 545 lines, uniq 5 |
| `test_execute_claims_rechecks_verifies_and_records_receipts` | 1 | R | partial or cov, cov 686 lines, uniq 10 |
| `test_current_wazuh_binding_must_match_every_catalogued_field` | 1 | R | partial or cov, cov 542 lines, uniq 2 |
| `test_reservation_contract_and_recovery_journal_precede_terminal_commit` | 1 | R | partial or cov, cov 655 lines, uniq 0 |
| `test_non_catalogued_wazuh_findings_remain_review_only` | 1 | R | partial or cov, cov 372 lines, uniq 4 |
| `test_observer_findings_remain_non_executable_without_wazuh_source` | 1 | R | partial or cov, cov 367 lines, uniq 0 |
| `test_legacy_actionable_ledger_finding_never_creates_or_executes_proposals` | 1 | R | partial or cov, cov 401 lines, uniq 0 |
| `test_missing_wazuh_source_or_finding_fails_closed` | 1 | R | partial or cov, cov 545 lines, uniq 3 |
| `test_capacity_exhaustion_before_mutation_skips_claim_and_execute` | 1 | R | partial or cov, cov 563 lines, uniq 2 |
| `test_executor_failure_writes_terminal_receipt_before_release` | 1 | R | partial or cov, cov 640 lines, uniq 6 |
| `test_verifier_failure_writes_terminal_receipt_before_release` | 1 | R | partial or cov, cov 645 lines, uniq 8 |
| `test_receipt_commit_failure_writes_terminal_receipt_before_release` | 1 | R | partial or cov, cov 617 lines, uniq 0 |
| `test_claim_release_false_preserves_receipt_and_surfaces_unavailable` | 1 | R | partial or cov, cov 654 lines, uniq 0 |
| `test_claim_release_exception_preserves_receipt_and_surfaces_unavailable` | 1 | R | partial or cov, cov 657 lines, uniq 2 |
| `test_policy_and_action_records_use_128_character_wazuh_finding_id` | 1 | R | partial or cov, cov 517 lines, uniq 0 |
| `test_failed_claim_release_retries_after_restart_without_reexecuting` | 1 | R | partial or cov, cov 882 lines, uniq 0 |
| `test_recovery_accepts_only_the_same_missing_fence_after_a_post_release_write_failure` | 1 | R | partial or cov, cov 889 lines, uniq 3 |
| `test_failed_claim_release_writes_mode_0600_cleanup_with_holder` | 1 | R | partial or cov, cov 1067 lines, uniq 3 |
| `test_consume_failure_keeps_terminal_evidence_when_reservation_release_fails` | 1 | R | partial or cov, cov 640 lines, uniq 5 |
| `test_receipt_commit_failure_keeps_terminal_evidence_when_reservation_release_fails` | 1 | R | partial or cov, cov 618 lines, uniq 0 |
| `test_terminal_recovery_journal_replays_exact_receipts_before_fenced_claim_release` | 1 | R | partial or cov, cov 859 lines, uniq 11 |

### test_grokbot_fleet_runtime_config.py (7 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_private_runtime_parses_and_projects_public_registry` | 1 | R | cov, 112 lines, uniq 0 |
| `test_private_runtime_rejects_unknown_fields_and_duplicate_aliases` | 1 | R | cov, 26 lines, uniq 3 |
| `test_runtime_env_requires_loopback_token_and_disjoint_paths` | 1 | R | cov, 56 lines, uniq 25 |
| `test_secure_runtime_read_requires_owner_only_regular_file` | 1 | R | cov, 62 lines, uniq 2 |
| `test_secure_runtime_read_rejects_foreign_owner` | 1 | R | cov, 32 lines, uniq 1 |
| `test_secure_runtime_read_rejects_path_swap_to_alternate_unit` | 1 | R | cov, 61 lines, uniq 3 |
| `test_probe_environment_is_allowlisted` | 1 | R | cov, 7 lines, uniq 0 |

### test_grokbot_fleet_tools.py (10 declarations, partial)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_overview_preserves_order_and_bounds_unavailable_targets` | 1 | R | partial or cov, cov 348 lines, uniq 4 |
| `test_host_status_and_service_health_authorize_registered_targets` | 1 | R | partial or cov, cov 434 lines, uniq 88 |
| `test_incident_bundle_catalogs_supported_findings` | 1 | R | partial or cov, cov 391 lines, uniq 0 |
| `test_propose_and_execute_follow_fail_closed_replay_rules` | 1 | R | partial or cov, cov 463 lines, uniq 0 |
| `test_unsupported_findings_propose_without_execution` | 1 | R | partial or cov, cov 313 lines, uniq 0 |
| `test_repeated_rejected_execution_does_not_unbounded_receipts` | 1 | R | Tool-level rejection-receipt dedupe through a real FleetActionStore, store-level twin lives in fleet_actions and covers the cap, this covers the tool wiring. |
| `test_execute_remediation_denies_saturated_live_receipts_before_claim_or_restart` | 1 | D | False positive: observer-only proposal is denied before any capacity guard, assertion accepts denied or unavailable. Control: same assertions pass with zero receipts. M3a-base and M3b-base both SURVIVE. Remaining proof: remediation::test_capacity_exhaustion_before_mutation_skips_claim_and_execute (M3a kills) and fleet_actions capacity tests (M3b2 kills). |
| `test_execute_remediation_releases_stale_reservation_capacity` | 1 | D | Never creates a reservation or saturates capacity (MAX_RECEIPTS patched, never reached), trailing write_receipt-exists assert is a plain store write. M3c-base survives. Remaining proof: fleet_actions::test_receipt_reservation_releases_and_does_not_pin_capacity (M3b2 kills). |
| `test_replay_loser_cannot_drop_winner_reservation_and_winner_commits_after_one_restart` | 1 | D | Name promises a replay race, body denies one observer-only leftover proposal (no second caller, no reservation). Mutation M3c-base: disabling reservation exclusivity SURVIVES this test. Remaining proof: fleet_actions::test_live_reservation_is_exclusive_and_token_scoped (M3c kills). |
| `test_private_store_fixture_preserves_capability_refusal` | 2 | R | Name reads like a fixture test, but the helper it runs asserts the production Windows/secure-owner refusal before any I/O: a platform contract. |

## L9 Obsidian Operator bridge (owner: grokbot_obsidian/*)

Files: `test_grokbot_obsidian_actions.py`, `test_grokbot_obsidian_adapters.py`, `test_grokbot_obsidian_capabilities.py`, `test_grokbot_obsidian_client.py`, `test_grokbot_obsidian_contracts.py`, `test_grokbot_obsidian_excalidraw_client.py`, `test_grokbot_obsidian_lifecycle.py`, `test_grokbot_obsidian_operator_install.py`, `test_grokbot_obsidian_operator_plugin.py`, `test_grokbot_obsidian_phase2.py`, `test_grokbot_obsidian_policy.py`, `test_grokbot_obsidian_runtime.py`. Declarations at base: 104.

### test_grokbot_obsidian_actions.py (12 declarations, partial)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_windows_ensure_directory_fails_closed_before_io` | 1 | R | partial or cov, cov 6 lines, uniq 0 |
| `test_windows_hex_json_listing_fails_closed_before_io` | 1 | R | partial or cov, cov 6 lines, uniq 0 |
| `test_proposal_is_single_use_and_replay_is_denied` | 1 | R | partial or cov, cov 393 lines, uniq 2 |
| `test_execute_requires_out_of_band_approval_and_denies_cross_bind` | 1 | R | partial or cov, cov 713 lines, uniq 15 |
| `test_expired_proposal_is_rejected_without_mutation` | 1 | R | partial or cov, cov 400 lines, uniq 3 |
| `test_one_mutation_and_four_reads_are_gated` | 1 | R | partial or cov, cov 163 lines, uniq 10 |
| `test_active_proposal_capacity_is_bounded_before_create` | 1 | R | partial or cov, cov 329 lines, uniq 1 |
| `test_expired_proposals_and_associated_state_are_pruned` | 1 | R | partial or cov, cov 429 lines, uniq 6 |
| `test_unexpired_proposals_survive_prune_and_replay_stays_denied` | 1 | R | partial or cov, cov 392 lines, uniq 0 |
| `test_concurrent_claims_are_single_use` | 1 | R | partial or cov, cov 387 lines, uniq 0 |
| `test_invoke_executor_accesses_only_the_selected_method` | 1 | R | partial or cov, cov 88 lines, uniq 0 |
| `test_tools_filter_and_deny_allowed_paths_with_sensitive_tags` | 1 | F | Inline TagAwareClient (a second implementation of the scripted client) replaced by the shared ScriptedClient. Same verdict as the original under M10a/M10b/M10c on both trees. Observation: M10c (tools.py check removed alone) survives on both, so the tools-layer tag check lacks isolated proof. |

### test_grokbot_obsidian_adapters.py (17 declarations, partial)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_native_search_and_trash_never_use_permanent_delete` | 1 | R | partial or cov, cov 162 lines, uniq 3 |
| `test_search_omits_limit_and_derives_snippet_from_bounded_matches` | 1 | R | partial or cov, cov 137 lines, uniq 1 |
| `test_sensitive_tags_filter_search_and_deny_read_patch_move_trash` | 1 | R | partial or cov, cov 165 lines, uniq 0 |
| `test_private_workflow_gateway_methods_use_fixed_tools_and_filter_sensitive_omnisearch_hits` | 1 | R | partial or cov, cov 164 lines, uniq 18 |
| `test_private_path_workflows_authorize_sensitive_files_before_the_private_call` | 1 | R | partial or cov, cov 99 lines, uniq 0 |
| `test_private_workflow_gateway_result_drift_fails_closed` | 5 | R | partial or cov, cov 123 lines, uniq 3 |
| `test_excalidraw_four_call_order_and_allowlisted_env` | 1 | R | partial or cov, cov 254 lines, uniq 1 |
| `test_scene_walk_rejects_executable_markdown` | 1 | R | partial or cov, cov 38 lines, uniq 2 |
| `test_allowlisted_env_pins_browser` | 1 | R | partial or cov, cov 3 lines, uniq 0 |
| `test_native_mcp_allowlist_rejects_command_execute` | 1 | R | partial or cov, cov 8 lines, uniq 0 |
| `test_targeted_read_uses_target_type_target_and_scope` | 1 | R | partial or cov, cov 93 lines, uniq 11 |
| `test_targeted_patch_uses_exact_top_level_fields_and_never_vault_write` | 1 | R | partial or cov, cov 110 lines, uniq 10 |
| `test_copy_and_move_use_path_destination_and_forbid_overwrite` | 1 | R | partial or cov, cov 114 lines, uniq 11 |
| `test_vault_read_exact_missing_file_envelope_returns_none` | 1 | R | partial or cov, cov 47 lines, uniq 3 |
| `test_vault_read_inexact_error_envelope_is_protocol_error` | 11 | R | partial or cov, cov 51 lines, uniq 4 |
| `test_vault_read_malformed_legacy_type_text_wrapper_is_protocol_error` | 4 | R | partial or cov, cov 53 lines, uniq 2 |
| `test_excalidraw_embed_is_a_staged_heading_patch` | 1 | R | partial or cov, cov 282 lines, uniq 20 |

### test_grokbot_obsidian_capabilities.py (3 declarations, read in full)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_projection_is_generic_and_sorted` | 1 | R | read, contract named by the test, cov 64 lines, uniq 0 |
| `test_fingerprint_mismatch_fails_closed` | 1 | R | read, contract named by the test, cov 63 lines, uniq 2 |
| `test_matching_fingerprint_projects_phase1` | 1 | R | read, contract named by the test, cov 133 lines, uniq 0 |

### test_grokbot_obsidian_client.py (4 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_client_performs_initialize_session_handshake_then_fixed_tools_call` | 1 | R | cov, 87 lines, uniq 0 |
| `test_client_rejects_unknown_tools_without_calling_fetch` | 1 | R | cov, 23 lines, uniq 0 |
| `test_client_close_is_idempotent_and_blocks_later_calls` | 1 | R | cov, 96 lines, uniq 2 |
| `test_client_errors_are_neutral_and_omit_secrets` | 1 | R | cov, 54 lines, uniq 3 |

### test_grokbot_obsidian_contracts.py (6 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_pack_identity_and_closed_tool_inventory` | 1 | R | cov, no context recorded (subprocess or class-scoped) |
| `test_accepts_minimal_public_inputs_and_rejects_extras` | 1 | R | cov, 48 lines, uniq 4 |
| `test_create_note_is_accepted_and_forbidden_kinds_are_rejected` | 1 | R | cov, 47 lines, uniq 0 |
| `test_revision_token_and_opaque_ids_are_strict` | 1 | R | cov, 33 lines, uniq 1 |
| `test_public_errors_never_include_paths_or_child_output` | 1 | R | cov, 13 lines, uniq 2 |
| `test_capabilities_projection_is_generic_and_phase1_only` | 1 | R | cov, 53 lines, uniq 2 |

### test_grokbot_obsidian_excalidraw_client.py (5 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_stdio_client_uses_absolute_executable_empty_argv_and_fixed_tools` | 1 | R | cov, 191 lines, uniq 3 |
| `test_stdio_factory_ignores_arbitrary_command_args_and_kills_on_close` | 1 | R | cov, 188 lines, uniq 1 |
| `test_stdio_client_enforces_deadline_and_output_cap` | 1 | R | cov, 178 lines, uniq 9 |
| `test_replaced_executable_after_validation_is_never_executed` | 1 | R | cov, 182 lines, uniq 0 |
| `test_helper_must_be_regular_executable` | 1 | R | cov, 57 lines, uniq 6 |

### test_grokbot_obsidian_lifecycle.py (20 declarations, partial)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_disjoint_paths_reject_overlap_and_relative` | 1 | R | partial or cov, cov 77 lines, uniq 0 |
| `test_absolute_reference_accepts_posix_and_drive_roots_and_rejects_unc` | 1 | R | partial or cov, cov 31 lines, uniq 3 |
| `test_paths_overlap_detects_backslash_nesting` | 1 | C | Absorbed by test_paths_overlap_on_windows_nests_on_backslashes_and_ignores_case in this file. M9b kills. Separate production copy from the fleet pack, so the proof stays per copy. |
| `test_paths_overlap_treats_backslash_as_literal_on_posix` | 1 | R | partial or cov, cov 9 lines, uniq 0 |
| `test_paths_overlap_is_case_insensitive_on_windows` | 1 | C | Absorbed by the same table. M9b kills. |
| `test_paths_overlap_handles_root_operand_and_rejects_trailing_alias` | 1 | R | partial or cov, cov 39 lines, uniq 2 |
| `test_disjoint_paths_reject_backslash_nested_drive_paths` | 1 | R | partial or cov, cov 12 lines, uniq 0 |
| `test_absolute_reference_rejects_non_ascii_drive_letter` | 1 | R | partial or cov, cov 19 lines, uniq 0 |
| `test_pack_setup_doctor_canary_and_unit_hide_secrets` | 1 | R | partial or cov, cov 748 lines, uniq 38 |
| `test_pack_setup_rejects_raw_upstream_key_and_non_loopback_url` | 1 | R | partial or cov, cov 283 lines, uniq 4 |
| `test_unit_readwrite_paths_exclude_approval_and_secrets` | 1 | R | partial or cov, cov 484 lines, uniq 0 |
| `test_configured_service_reaches_live_client_factory_and_allowlist` | 1 | R | partial or cov, cov 392 lines, uniq 7 |
| `test_build_tools_passes_runtime_sensitive_tags_into_native_port` | 1 | R | partial or cov, cov 376 lines, uniq 0 |
| `test_canary_requires_live_capabilities_and_fails_when_native_unavailable` | 1 | R | partial or cov, cov 493 lines, uniq 9 |
| `test_configured_service_reaches_excalidraw_stdio_factory` | 1 | R | partial or cov, cov 448 lines, uniq 5 |
| `test_default_factory_builds_streamable_client` | 1 | R | partial or cov, cov 391 lines, uniq 3 |
| `test_upstream_key_must_differ_from_bearer_and_peer_tokens` | 1 | R | partial or cov, cov 296 lines, uniq 1 |
| `test_jsonrpc_batches_and_unknown_tools_are_rejected` | 1 | R | partial or cov, cov 21 lines, uniq 16 |
| `test_obsidian_unit_uses_shared_listener_recovery_policy` | 1 | R | partial or cov, cov 484 lines, uniq 0 |
| `test_windows_branch_rejects_drive_less_roots` | 1 | R | partial or cov, cov 29 lines, uniq 0 |

### test_grokbot_obsidian_operator_install.py (5 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_sha256sums_match_checked_in_plugin_bytes` | 1 | R | cov, no context recorded (subprocess or class-scoped) |
| `test_installer_copies_only_adapter_files_mode_0644` | 1 | R | cov, no context recorded (subprocess or class-scoped) |
| `test_installer_rejects_symlink_and_non_owner_vault` | 1 | R | cov, no context recorded (subprocess or class-scoped) |
| `test_installer_rejects_relative_and_nested_destination` | 1 | R | cov, no context recorded (subprocess or class-scoped) |
| `test_rollback_removes_only_adapter_directory` | 1 | R | cov, no context recorded (subprocess or class-scoped) |

### test_grokbot_obsidian_operator_plugin.py (3 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_plugin_sources_are_checked_in_commonjs` | 1 | R | cov, no context recorded (subprocess or class-scoped) |
| `test_node_check_and_vm_harness` | 1 | R | cov, no context recorded (subprocess or class-scoped) |
| `test_bundle_zod_check_is_network_free_and_detects_generated_drift` | 1 | R | cov, no context recorded (subprocess or class-scoped) |

### test_grokbot_obsidian_phase2.py (14 declarations, partial)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_phase2_action_schemas_are_strict_and_keep_six_hex_ifmatch` | 1 | R | partial or cov, cov 190 lines, uniq 3 |
| `test_capabilities_demote_without_adapter_and_promote_on_fingerprint_match` | 1 | R | partial or cov, cov 191 lines, uniq 14 |
| `test_native_allowlist_includes_only_fixed_private_adapter_tools` | 1 | R | partial or cov, cov 92 lines, uniq 0 |
| `test_private_adapter_inventory_pins_each_tool_schema_and_result_shape` | 1 | R | partial or cov, cov 38 lines, uniq 1 |
| `test_native_replace_methods_parse_exact_hash_result_and_reject_unknown_tools` | 1 | R | partial or cov, cov 34 lines, uniq 13 |
| `test_phase2_updates_are_rejected_before_mutation_when_adapter_absent` | 1 | R | partial or cov, cov 187 lines, uniq 1 |
| `test_patch_canvas_uses_full_sha_and_private_cas` | 1 | R | partial or cov, cov 162 lines, uniq 1 |
| `test_update_excalidraw_exports_then_cas_and_rereads` | 1 | R | partial or cov, cov 285 lines, uniq 78 |
| `test_gateway_execute_patch_base_is_verified` | 1 | R | partial or cov, cov 794 lines, uniq 13 |
| `test_outbound_cap_covers_max_replacement_plus_envelope_below_hard_ceiling` | 1 | R | partial or cov, cov 7 lines, uniq 0 |
| `test_streamable_client_accepts_maximum_valid_outbound_and_rejects_one_byte_over` | 1 | R | partial or cov, cov 98 lines, uniq 1 |
| `test_propose_rejects_unsendable_phase2_replacement_before_approval` | 1 | R | partial or cov, cov 360 lines, uniq 6 |
| `test_stale_plugin_and_impostor_same_name_schema_demote_to_phase1` | 1 | R | partial or cov, cov 180 lines, uniq 3 |
| `test_update_excalidraw_bind_rechecks_embed_revision_before_claim` | 1 | R | partial or cov, cov 777 lines, uniq 40 |

### test_grokbot_obsidian_policy.py (4 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_normalize_accepts_vault_relative_posix_and_rejects_escapes` | 1 | R | cov, 25 lines, uniq 3 |
| `test_static_readable_denies_attachments_memory_daily_and_sensitive` | 1 | R | cov, 48 lines, uniq 5 |
| `test_static_writable_roots_and_phase1_kinds` | 1 | R | cov, 69 lines, uniq 1 |
| `test_content_policy_rejects_executable_markdown` | 1 | R | cov, 36 lines, uniq 6 |

### test_grokbot_obsidian_runtime.py (11 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_runtime_env_defaults_to_loopback_8773_and_requires_https_upstream` | 1 | R | cov, 98 lines, uniq 23 |
| `test_runtime_env_requires_disjoint_state_paths` | 1 | R | cov, 66 lines, uniq 0 |
| `test_private_runtime_rejects_phase2_plugin_actions` | 1 | R | cov, 48 lines, uniq 8 |
| `test_spki_pin_extracts_from_generated_certificate` | 1 | R | cov, 55 lines, uniq 2 |
| `test_adapter_environment_is_allowlisted` | 1 | R | cov, 1 lines, uniq 0 |
| `test_pinned_fetch_rejects_wrong_ca_ip_name_pin_redirect_and_oversize` | 1 | R | cov, 82 lines, uniq 0 |
| `test_pinned_fetch_rotation_pins_accept_old_and_new_then_reject_old` | 1 | R | cov, 81 lines, uniq 0 |
| `test_pinned_fetch_rejects_redirects_and_oversize_response` | 1 | R | cov, 79 lines, uniq 2 |
| `test_secure_runtime_read_rejects_world_readable_symlink_foreign_owner_and_oversize` | 1 | R | cov, 78 lines, uniq 4 |
| `test_secure_runtime_read_rejects_path_swap_malformed_and_never_echoes` | 1 | R | cov, 77 lines, uniq 3 |
| `test_wrong_pin_never_sends_authorization_bearing_http` | 1 | R | cov, 73 lines, uniq 0 |

## L10 Wazuh triage bridge (owner: grokbot_wazuh/*)

Files: `test_grokbot_wazuh_contracts.py`, `test_grokbot_wazuh_normalize.py`, `test_grokbot_wazuh_policy.py`. Declarations at base: 12.

### test_grokbot_wazuh_contracts.py (3 declarations, read in full)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_public_tool_inventory_is_exact` | 1 | R | read, contract named by the test, cov no context recorded (subprocess or class-scoped) |
| `test_ingest_rejects_unbounded_or_secret_fields` | 1 | R | read, contract named by the test, cov 41 lines, uniq 3 |
| `test_identifiers_and_tool_inputs_are_strict` | 1 | R | read, contract named by the test, cov 31 lines, uniq 18 |

### test_grokbot_wazuh_normalize.py (7 declarations, cov only)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_normalized_record_has_required_finding_fields_and_redacted_body` | 1 | R | cov, 128 lines, uniq 0 |
| `test_fingerprints_are_deterministic_and_ignore_volatile_detail` | 1 | R | cov, 126 lines, uniq 0 |
| `test_sanitize_detail_never_raises_and_truncates` | 1 | R | cov, 31 lines, uniq 1 |
| `test_title_and_common_secret_forms_are_sanitized_before_storage` | 1 | R | cov, 126 lines, uniq 0 |
| `test_semantic_revision_is_deterministic_and_changes_with_sanitized_content` | 1 | R | cov, 128 lines, uniq 2 |
| `test_composed_finding_id_accepts_maximum_agent_id` | 1 | R | cov, 129 lines, uniq 2 |
| `test_sensitive_assignments_are_redacted_in_equals_colon_and_json_forms` | 1 | R | cov, 126 lines, uniq 0 |

### test_grokbot_wazuh_policy.py (2 declarations, read in full)

| Test | Rows | Mark | Evidence |
|---|---|---|---|
| `test_known_noise_expires_to_watch_and_high_confidence_event_escalates` | 1 | R | read, contract named by the test, cov 163 lines, uniq 1 |
| `test_malformed_unknown_and_explicit_suppression_follow_precedence` | 1 | R | read, contract named by the test, cov 162 lines, uniq 1 |

## Totals

Declarations at base: 1172. R 1130, F 8, C 26, D 8.

