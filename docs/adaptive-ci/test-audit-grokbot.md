# Grokbot test audit, pilot

Pilot of the OpenClaw `test-audit` method on one Brigade subsystem. Base: `origin/main` at `3510da921d64d1c0f5a531ec5eb8347a125e784d`. Scope: `tests/test_grokbot*.py` against the grokbot source modules. The per-declaration ledger is in [test-audit-grokbot-ledger.md](test-audit-grokbot-ledger.md).

## Result

The evidence supports removing 444 of 33,044 test lines (1.3 percent), not the 20 percent the pilot aimed for. Grokbot source line coverage stayed at 78.62 percent (13,689 of 17,412 statements, with the covered-line count unchanged in all 65 modules). Every contract whose only proof moved or was consolidated has a mutation of its production owner that the keeper catches, and the deleted tests that the mutations show to be false positives were checked against `origin/main` to confirm they did not catch the same mutation. No baseline test failed.

| Measure | Before (`3510da921`) | After | Change |
|---|---|---|---|
| Test files | 53 | 53 | 0 |
| Test declarations (AST) | 1,172 | 1,147 | -25 |
| Tests collected (with `[dev,grokbot]`) | 1,999 | 1,992 | -7 |
| Test LOC (`wc -l`) | 33,044 | 32,600 | -444 (-1.3%) |
| Test LOC, non-blank non-comment | 27,729 | 27,378 | -351 |
| Support LOC dedicated to grokbot tests | 0 | 0 | 0 |
| Grokbot source LOC (65 files, counted separately) | 29,316 | 29,316 | 0 |
| Grokbot test runtime, run 1 (seconds, pytest summary line) | 73.98 | 74.57 | +0.59 |
| Grokbot test runtime, run 2 | 81.41 | 66.96 | -14.45 |
| Sum of per-test call times from junit, run 1 / run 2 (seconds) | 68.3 / 72.5 | 69.4 / 62.4 | |
| Grokbot line coverage (statements covered) | 78.618% (13,689 of 17,412) | 78.618% (13,689 of 17,412) | +0.000 points |

Definitions and conditions:

- Test LOC counts every line of the 53 `tests/test_grokbot*.py` files. Support LOC is 0 because the suite has no grokbot-specific support module. Shared support (`tests/dot_support.py`, `tests/conftest.py`) is not counted.
- Source LOC counts every `src/brigade/**/*.py` path containing `grokbot`, including the `grokbot_fleet`, `grokbot_obsidian`, `grokbot_t3` and `grokbot_wazuh` packages and `fleet_client_grokbot.py` and `fleet_hub_grokbot.py`. No source file changed.
- Runtime: the same command before and after, `python -m pytest <53 files> -q --durations=0 --junitxml=...`, serial, no coverage, through `brigade work verify run`. Two runs each side. The machine was shared with other agents' pytest runs, and the second baseline run (81.4 s) overlapped a CPU-heavy analysis script of mine that I stopped partway. The four runs span 67 to 81 seconds with no change that survives that spread, so this PR makes no runtime claim. Runtime is not a lever here: the whole set runs in about 75 seconds.
- Coverage: one run each side with `--cov=brigade`, filtered to the 65 grokbot source files. The venv had the `grokbot` extra installed (`pip install -e ".[dev,grokbot]"`), the way CI's `tests/test_grokbot_mcp.py` step does. Without the extra, 22 tests skip silently and coverage reads 77.94 percent.

## Why not 20 percent

Three findings explain the gap.

1. Line coverage cannot pick deletions here. A per-test coverage-context run (`--cov-context=test`, 1,933 tests with grokbot lines) shows 1,480 tests (77 percent) execute no grokbot line that another grokbot test does not also execute. Almost every test is line-redundant and almost none is contract-redundant. The coverage floor is therefore not the binding constraint. The retention bar is.
2. The mirrored tests mirror mirrored source. 26 test declarations share a name with a declaration in a sibling file (about 534 LOC beyond the first copy): 10 pairs in `build_feed` and `scout_feed`, 9 in `fleet_lifecycle` and `obsidian_lifecycle`, 5 in `findings` and `reconcile`, 2 in `findings` and `findings_relay`. In the first three groups each side exercises a separately maintained production copy (the feed policy loader, the Windows path helpers, the handoff writers). I did not trace the `findings_relay` pair. A scan of grokbot source finds 227 lines of identical function bodies across modules. Deleting one side of a pair loses real proof for one copy. Consolidating the production code first is the change that would let these tests collapse, and that is outside this pilot.
3. Most of the suite sits inside the retention bar. The grokbot subsystem is job validation, untrusted input, MCP tools, tokens, private storage, Windows refusal and credential-rotation matrices. The campaign keeps those contracts even when they are slow or static, and so did this pilot.

## Method and its limits

The work follows `CAMPAIGN.md` from the OpenClaw test-audit skill, adapted to pytest and done by one agent. Two things differ from the campaign as written.

- **No per-lane read-only agents.** One agent read the tests. The ledger records how deeply each file was read: `read in full` (7 files), `partial` (21 files, the named tests read against their production owners), and `cov only` (the rest, retained on the coverage-context evidence described above). A `cov only` retention is a default, not a finding that the test is good. A later pass with per-lane reviewers is the natural next step and could find more.
- **Mutation instead of live proof.** Preservation was checked with deliberate mutations of the production owner, run on full copies of `src`, `tests`, `docs` and `pyproject.toml` (Brigade's conftest pins the copy's own `src`). The worktree's `src/` was never edited, and `git diff --exit-code src/` stays clean.

## Lanes

Lanes follow production owners. Every one of the 53 files belongs to exactly one lane. Two files named for grokbot (`memory_profile`, `s26_profiles`) actually test `brigade.dot` profile code and sit in L6.

| Lane | Files | Test LOC before | After |
|---|---|---|---|
| L1 jobs queue | 1 | 2,614 | 2,565 |
| L2 MCP listener and client identity | 2 | 3,534 | 3,474 |
| L3 feed intake | 3 | 4,372 | 4,362 |
| L4 findings and report reconcile | 3 | 2,971 | 2,916 |
| L5 packs, ops and relays | 4 | 3,844 | 3,807 |
| L6 memory and inbox profiles | 3 | 1,719 | 1,692 |
| L7 T3 dispatch bridge | 10 | 6,024 | 6,007 |
| L8 Fleet Steward bridge | 12 | 3,835 | 3,672 |
| L9 Obsidian Operator bridge | 12 | 3,771 | 3,745 |
| L10 Wazuh triage bridge | 3 | 360 | 360 |

Ledger totals over 1,172 declarations: R (retain) 1,130, F (fix) 8, C (consolidate) 26, D (delete) 8.

## What changed, by category

Declaration counts are exact (they come from the ledger marks). Net LOC per file is in the next table, because most files mix categories.

| Category | Mark | Declarations | Where |
|---|---|---|---|
| Name promises behavior the body never exercises (false positives) | D | 3 | `fleet_tools` |
| Copied CLI command inventory | D | 4 | `ops`, `packs`, `findings` (2) |
| Layer replay of a jobs-owner contract at the MCP layer | D | 1 | `mcp` |
| Same test with one constant changed, merged into a parametrized test | C | 23 declarations into 9 tests | `jobs` (hub enqueue 7 into 1, hub claim 2 into 1), `client_identity` 2 into 1, `t3_tools` 2 into 1, `feed` 2 into 1, `reconcile` 4 into 2, `fleet_lifecycle` 2 into 1, `obsidian_lifecycle` 2 into 1 |
| Private-shape or cross-file duplicate folded into the owning test | C | 4 | `mcp` (2 advertise tests), `memory_profile` (2) |
| Setup or assertion repaired without changing the contract | F | 8 | `mcp`, `jobs`, `memory_profile`, `s26_profiles` (2), `packs`, `obsidian_actions`, `reconcile` |

| File | LOC before | After | Net |
|---|---|---|---|
| `tests/test_grokbot_fleet_tools.py` | 516 | 356 | -160 |
| `tests/test_grokbot_jobs.py` | 2,614 | 2,565 | -49 |
| `tests/test_grokbot_mcp.py` | 3,039 | 2,994 | -45 |
| `tests/test_grokbot_reconcile.py` | 1,021 | 978 | -43 |
| `tests/test_grokbot_packs.py` | 1,528 | 1,499 | -29 |
| `tests/test_grokbot_memory_profile.py` | 418 | 391 | -27 |
| `tests/test_grokbot_obsidian_actions.py` | 472 | 449 | -23 |
| `tests/test_grokbot_t3_tools.py` | 713 | 696 | -17 |
| `tests/test_grokbot_client_identity.py` | 495 | 480 | -15 |
| `tests/test_grokbot_findings.py` | 1,219 | 1,207 | -12 |
| `tests/test_grokbot_feed.py` | 1,653 | 1,643 | -10 |
| `tests/test_grokbot_ops.py` | 1,319 | 1,311 | -8 |
| `tests/test_grokbot_fleet_lifecycle.py` | 445 | 442 | -3 |
| `tests/test_grokbot_obsidian_lifecycle.py` | 647 | 644 | -3 |

## Keepers

| Contract | Keeper |
|---|---|
| Receipt-capacity refusal before claim or execute | `fleet_remediation::test_capacity_exhaustion_before_mutation_skips_claim_and_execute`, `fleet_actions::test_receipt_reservation_releases_and_does_not_pin_capacity`, `fleet_actions::test_action_store_caps_receipts_and_dedupes_identical_rejections` |
| One live reservation per proposal | `fleet_actions::test_live_reservation_is_exclusive_and_token_scoped` |
| Execution budget counts from `claimed_at` (#1403) | `jobs::test_a_late_claim_gets_its_whole_execution_budget`, `jobs::test_an_unclaimed_job_expires_at_its_queue_ttl`, `mcp::test_rejected_claims_return_generic_error_and_hide_execution_context[expired]` |
| MCP claim argument types and required-ness | `mcp::test_a_non_null_wrong_type_on_an_optional_argument_is_still_refused`, `mcp::test_registered_tool_signatures_advertise_exactly_the_accepted_arguments`, the served `tools/list` schema test |
| CLI command registration | `tests/test_cli_inventory_contract.py::test_parser_inventory_and_missing_cli_agree_after_alias_normalization` plus the per-command `cli.main` tests |
| Profile tool gating and discovery for the grok profiles | `s26_profiles::test_no_dispatch_claim_or_fleet_tool_is_reachable`, `s26_profiles::test_registered_discovery_is_exactly_the_profile` |

## Test-only production seams

An identifier scan over `src/` and `tests/` finds 16 grokbot symbols with no non-test reference: `grokbot_findings.apply_entries`, `grokbot_jobs.admit_hub_authority`, `grokbot_reconcile._marker_path`, `grokbot_packs.LIST_PACK_KEYS`, `grokbot_wazuh.contracts.FINDING_KEYS`, `parse_alert_status_input`, `parse_classify_input`, `parse_ingest_input` (22 test references), `grokbot_wazuh.normalize.normalize_alert`, `grokbot_fleet.actions.is_executable_remediation`, `write_fleet_approval_file`, `grokbot_fleet.runtime_config.load_fleet_action_paths_env`, `load_fleet_runtime_env`, `grokbot_obsidian.runtime_config.load_runtime_env`, `grokbot_obsidian.store.write_approval_file` and `grokbot_obsidian.tls.pin_of_pem`.

None was removed. The deletions in this pilot did not make any of them removable, and the tests that exercise these symbols are their only proof. `admit_hub_authority` is documented in its docstring as the operator action that writes the authority marker. The approval-file writers produce the out-of-band approval record the tools later consume, so an external operator process may call them. I did not trace the wazuh `parse_*` helpers any further. Each needs an owner decision, so this PR records them as follow-ups instead of editing source. The scan is by identifier, so a caller that builds a name at runtime would be missed.

## Retained false positives and observations

- `test_private_store_fixture_preserves_capability_refusal` reads like a test of a test helper. The helper it runs asserts the production secure-owner refusal before any I/O, so it is a platform contract. Kept.
- `jobs::test_status_reads_each_job_file_once` counts calls to a private reader. It is the only proof of the single-traversal property, so it stays. A behavioural replacement would be better.
- `jobs::test_hub_does_not_yet_persist_worker_label` is a tripwire for pending hub work (#1501), and it also pins that the hub accepts the field it drops. Kept, to be rewritten when the column lands.
- `findings_relay::test_report_event_signature_stays_unchanged` pins another module's signature. It is a public-API compatibility pin (`report_event` is called with one positional argument), so it stays.
- Mutation M10c: removing the tools-layer sensitive-tag check in `grokbot_obsidian/tools.py` alone survives the tools-level test on both `origin/main` and this branch. The port-level check in `path_policy.py` is what the test exercises. The tools-layer check has no isolated proof. Recorded as a follow-up. This PR does not change source.

## Preservation review: mutations

Each row makes one change to the production owner on a copy, runs the named tests, and restores the file byte for byte. `Control` was green for every row before the mutation. Rows on `origin/main` run the tests this PR deleted or merged against the same mutation: `SURVIVED` there means the deleted test did not protect the contract, and `killed` means the deleted test and the keeper catch the same break.

| ID | Tree | Contract | Mutated file | Tests run | Result |
|---|---|---|---|---|---|
| M1a | head | mcp claim optional arg types: lease_seconds is int (folded from deleted advertise test) | `grokbot_mcp.py` | `mcp.py::test_a_non_null_wrong_type_on_an_optional_argument_is_still_refused` | killed |
| M1b | head | mcp claim optional arg types: worker_label is str (folded) | `grokbot_mcp.py` | `mcp.py::test_a_non_null_wrong_type_on_an_optional_argument_is_still_refused` | killed |
| M1c | head | mcp: explicit null optional arguments are dropped before validation (lease_seconds null folded into the wrong-type test) | `grokbot_mcp.py` | `mcp.py::test_a_non_null_wrong_type_on_an_optional_argument_is_still_refused` | killed |
| M2a | head | grok memory profile: memory_search description carries the untrusted-data notice | `dot/profile.py` | `s26_profiles.py::test_registered_discovery_is_exactly_the_profile` | killed |
| M2b | head | grok memory profile: memory_search default limit 10 | `dot/contracts.py` | `memory_profile.py::test_validation_accepts_a_profile_tool_with_its_default_limit` | killed |
| M2c | head | grok memory profile: handoff inbox is .grok/memory-handoffs | `dot/profile.py` | `memory_profile.py::test_build_tools_files_handoffs_in_the_grok_inbox_and_refuses_another_profile` | killed |
| M2d | head | profile tool gate: tools outside the profile are refused (keeper: s26 param[grokbot-memory]) | `dot/adapter.py` | `s26_profiles.py::test_no_dispatch_claim_or_fleet_tool_is_reachable` | killed |
| M2d-base | origin/main | same mutation, deleted memory_profile loop test on origin/main (redundancy evidence) | `dot/adapter.py` | `memory_profile.py::test_validation_refuses_every_tool_outside_the_profile` | killed |
| M3a | head | fleet remediation: receipt-capacity refusal before claim/execute (keeper: remediation capacity test) | `grokbot_fleet/tools.py` | `fleet_remediation.py::test_capacity_exhaustion_before_mutation_skips_claim_and_execute` | killed |
| M3a-base | origin/main | same mutation vs the deleted fleet_tools saturated-receipts test (not protective) | `grokbot_fleet/tools.py` | `fleet_tools.py::test_execute_remediation_denies_saturated_live_receipts_before_claim_or_restart` | SURVIVED |
| M3b2 | head | fleet action store: live receipt capacity refusal (keepers: caps test + reservation-does-not-pin-capacity test) | `grokbot_fleet/actions.py` | `fleet_actions.py::test_action_store_caps_receipts_and_dedupes_identical_rejections + fleet_actions.py::test_receipt_reservation_releases_and_does_not_pin_capacity` | killed |
| M3b-base | origin/main | capacity cap removed vs the same deleted test (not protective) | `grokbot_fleet/actions.py` | `fleet_tools.py::test_execute_remediation_denies_saturated_live_receipts_before_claim_or_restart` | SURVIVED |
| M3c | head | fleet action store: one live reservation per proposal (keeper: fleet_actions exclusivity test) | `grokbot_fleet/actions.py` | `fleet_actions.py::test_live_reservation_is_exclusive_and_token_scoped` | killed |
| M3c-base | origin/main | reservation exclusivity removed vs the deleted replay-loser and stale-reservation tests (not protective) | `grokbot_fleet/actions.py` | `fleet_tools.py::test_replay_loser_cannot_drop_winner_reservation_and_winner_commits_after_one_restart + fleet_tools.py::test_execute_remediation_releases_stale_reservation_capacity` | SURVIVED |
| M4a | head | hub enqueue failure never discards a snapshot the hub may hold (keeper: jobs table) | `grokbot_jobs.py` | `jobs.py::test_a_failed_hub_enqueue_keeps_the_snapshot_unless_the_hub_holds_no_such_job` | killed |
| M4b | head | hub enqueue refusal discards the snapshot when the hub holds no such job (keeper: jobs table) | `grokbot_jobs.py` | `jobs.py::test_a_failed_hub_enqueue_keeps_the_snapshot_unless_the_hub_holds_no_such_job` | killed |
| M5a | head | hub claim forwards the worker label in the request body (keeper: merged jobs test) | `grokbot_jobs.py` | `jobs.py::test_hub_claim_sends_the_worker_label_only_when_one_is_given` | killed |
| M5b | head | hub claim omits the worker label when none is given (keeper: merged jobs test) | `grokbot_jobs.py` | `jobs.py::test_hub_claim_sends_the_worker_label_only_when_one_is_given` | killed |
| M6 | head | listener hub credential must match the configured client identity (keeper: merged identity table) | `grokbot_mcp.py` | `client_identity.py::test_hub_client_identity_must_match_authenticated_credential` | killed |
| M7 | head | status poll cannot clobber a fresh reopening marker (keeper: merged t3 test) | `grokbot_t3/ledger.py` | `t3_tools.py::test_status_during_followup_cannot_clobber_the_reopen_or_the_new_turn` | killed |
| M8 | head | feed validates every entry before any write, preflight and apply (keeper: merged feed test) | `grokbot_feed.py` | `feed.py::test_an_invalid_later_entry_rejects_the_manifest_without_writing` | killed |
| M9a | head | fleet paths_overlap is case-insensitive on Windows (keeper: merged table) | `grokbot_fleet/runtime_config.py` | `fleet_lifecycle.py::test_paths_overlap_on_windows_nests_on_backslashes_and_ignores_case` | killed |
| M9b | head | obsidian paths_overlap is case-insensitive on Windows (keeper: merged table) | `grokbot_obsidian/runtime_config.py` | `obsidian_lifecycle.py::test_paths_overlap_on_windows_nests_on_backslashes_and_ignores_case` | killed |
| M10a | head | obsidian port denies sensitive-tag files (keeper: adapters test) | `grokbot_obsidian/path_policy.py` | `obsidian_adapters.py::test_sensitive_tags_filter_search_and_deny_read_patch_move_trash` | killed |
| M10b-head | head | obsidian tools-layer sensitive-tag denial, port check and tools check both removed (shared ScriptedClient version) | `grokbot_obsidian/path_policy.py, grokbot_obsidian/tools.py` | `obsidian_actions.py::test_tools_filter_and_deny_allowed_paths_with_sensitive_tags` | killed |
| M10b-base | origin/main | same double mutation vs the original inline TagAwareClient version | `grokbot_obsidian/path_policy.py, grokbot_obsidian/tools.py` | `obsidian_actions.py::test_tools_filter_and_deny_allowed_paths_with_sensitive_tags` | killed |
| M10c-head | head | tools.py check removed alone (shared ScriptedClient version) | `grokbot_obsidian/tools.py` | `obsidian_actions.py::test_tools_filter_and_deny_allowed_paths_with_sensitive_tags` | SURVIVED |
| M10c-base | origin/main | tools.py check removed alone (original inline TagAwareClient version) | `grokbot_obsidian/tools.py` | `obsidian_actions.py::test_tools_filter_and_deny_allowed_paths_with_sensitive_tags` | SURVIVED |
| M11 | head | reconcile-findings command stays registered (keepers: generic inventory contract + CLI behaviour) | `cli/run_cloud.py` | `test_cli_inventory_contract.py::test_parser_inventory_and_missing_cli_agree_after_alias_normalization + findings.py::test_cli_preview_defaults_to_write_nothing_and_omits_finding_text` | killed |
| M11-base | origin/main | same mutation vs the deleted copied-inventory test (also fails: redundant with the keepers) | `cli/run_cloud.py` | `findings.py::test_reconcile_findings_appears_in_parser_inventory` | killed |
| M12 | head | execution budget counts from claimed_at, not queued_at (keeper: jobs late-claim test) | `grokbot_jobs_expiry.py` | `jobs.py::test_a_late_claim_gets_its_whole_execution_budget` | killed |
| M12-base | origin/main | same mutation vs the deleted mcp late-claim test (not protective: the test passes under the mutation) | `grokbot_jobs_expiry.py` | `mcp.py::test_a_late_claim_is_granted_and_expires_only_past_the_queue_ttl` | SURVIVED |
| M13 | head | hub 400 validation refusal is not reported as hub-unavailable (jobs test now on the hub_wire fixture) | `fleet_client_grokbot.py` | `jobs.py::test_hub_validation_refusal_is_not_hub_unavailable` | killed |
| M14 | head | fleet-steward default bind 127.0.0.1:8771 (the port pins dropped from the queue-pack test live here) | `grokbot_packs.py` | `packs.py::test_registry_is_closed_deterministic_and_exact_key_validated` | killed |
| M14-base | origin/main | same mutation vs the original queue-pack test that carried the duplicate port pins (kills too) | `grokbot_packs.py` | `packs.py::test_first_party_queue_packs_keep_isolated_ports_tools_and_credentials` | killed |
| R1a | head | reconcile: marker sha256 must match the report (keeper: merged marker table) | `grokbot_reconcile.py` | `reconcile.py::test_a_marker_that_disagrees_with_the_job_fails_closed` | killed |
| R1a-base | origin/main | same sha256 mutation vs the original two tests (equivalence) | `grokbot_reconcile.py` | `reconcile.py::test_conflicting_marker_fails_closed` | killed |
| R1b | head | reconcile: marker task_hash must match the job (keeper: merged marker table) | `grokbot_reconcile.py` | `reconcile.py::test_a_marker_that_disagrees_with_the_job_fails_closed` | killed |
| R1b-base | origin/main | same task_hash mutation vs the original two tests (equivalence) | `grokbot_reconcile.py` | `reconcile.py::test_marker_with_wrong_task_hash_fails_closed` | killed |
| R2 | head | reconcile: unreadable marker fails closed with or without the snapshot (keeper: merged table) | `grokbot_reconcile.py` | `reconcile.py::test_corrupt_marker_fails_closed` | killed |

After the last mutation, `git diff --exit-code src/` in the worktree returned 0.

An extra signal from `uvx mutmut` was not attempted. The hand-written mutations above cover each moved contract directly, and a whole-module mutmut run on the fleet or jobs modules would take hours against this suite.

## Baseline failures and defects

No test failed at the baseline (1,999 passed, 0 skipped with the `grokbot` extra). Two observations survive into the follow-up list.

- Local runs without the `grokbot` extra skip 22 tests silently (`mcp` 5, `memory_profile` 1, `s26_profiles` 4, `t3_lifecycle` 7, `t3_inbox` 5). CI installs the extra for its `test_grokbot_mcp.py` step. A bare `pytest tests/test_grokbot*.py` reports green with fewer tests, and the coverage it reports is 0.7 points lower.
- The tools-layer sensitive-tag check lacks isolated proof (M10c above).

## What we learned for an authoring gate

The junk found falls into four shapes. Each maps to a question in the authoring gate.

1. **A name that promises more than the body does.** Three `fleet_tools` tests claim replay races, stale-reservation release and receipt saturation. Their bodies deny one observer-only proposal that is refused before any of those guards. None of the three catches the guard its name describes: mutating the capacity refusal (M3a-base, M3b-base) or the reservation exclusivity (M3c-base) leaves all three green. Gate question 2, "what credible regression makes this fail," catches this. A reviewer who has to write the one-line answer, or who runs the mutation, sees it at once.
2. **A copied inventory.** Four tests list CLI command names that a generic parser-versus-docs contract already compares. Gate question 3 (existing proof at the strongest boundary) catches it.
3. **A layer replay.** The MCP late-claim test restates a jobs-owner contract and survives the mutation that breaks it (M12-base). Gate question 3 again.
4. **Siblings that differ by one constant.** Nine groups differed by a literal, a state, or one assertion. They are tables now. Gate question 3 asks for extending a table before adding a test.

Practical points for adopting a gate in Brigade:

- Ask for a one-line `fails when:` in the PR description of any new test, and spot-check it with a mutation. This pilot's driver (copy the tree, apply one text replacement, run the keeper, restore) takes under a minute per mutation.
- Use per-test coverage contexts to order the reading, never to choose deletions. 77 percent of tests had no unique line.
- Mutation runs need `PYTHONDONTWRITEBYTECODE=1`. A same-size edit inside the same second reuses the stale `.pyc`, and the first mutation run reported a survivor that was actually a cache hit.
- A coverage JSON with contexts for `--cov=brigade` was 3.5 GB. Read the `.coverage` SQLite file directly instead.
- Ownership by file name misleads. `test_grokbot_memory_profile.py` and `test_grokbot_s26_profiles.py` test `brigade.dot`, and their grokbot-source coverage is entirely redundant for that reason. Lane maps and a future ownership note should follow the production owner.
- Add a check that fails, or at least warns, when the `grokbot` extra is missing for the grokbot lane, so a green local run cannot hide 22 skips.

## Follow-ups

- Consolidate the duplicated production helpers across `grokbot_build_feed` and `grokbot_scout_feed`, `grokbot_fleet` and `grokbot_obsidian` lifecycle, and `grokbot_findings` and `grokbot_reconcile`. Then collapse the 26 mirrored test declarations.
- Decide the 16 test-only symbols above.
- Add isolated proof for the tools-layer sensitive-tag check, or remove it as defence in depth the tests do not need.
- Replace `test_status_reads_each_job_file_once` with a behavioural proof.
- Run per-lane read-only reviewers over the `partial` and `cov only` files in the ledger.

## Verification

Every run went through `brigade work verify run --target . --argv-json ... --capture brigade-work`. The receipt ids below are local to the authoring worktree (`.brigade/work` is not committed).

| Run | Receipt | Result |
|---|---|---|
| Baseline timing, run 1 and run 2 (`[dev,grokbot]`) | `20261109-151509-work-verify-98458b`, `20261109-151652-work-verify-a0df5b` | 1,999 passed, 73.98 s and 81.41 s |
| Baseline coverage | `20261109-151840-work-verify-aba307` | 1,999 passed, 78.618 percent of grokbot statements |
| Baseline without the `grokbot` extra (discarded) | `20261109-151212-work-verify-64e83d` | 1,977 passed, 22 skipped, 77.935 percent |
| Per-test coverage contexts | `20261109-152135-work-verify-50b1be` | 1,999 passed |
| Controls for the three fleet_tools false positives | `20261109-152546-work-verify-94d155` | 2 passed (same assertions pass with no saturation, no replay, no reservation) |
| After timing, run 1 and run 2 | `20261109-153724-work-verify-b95041`, `20261109-153912-work-verify-31d042` | 1,992 passed, 74.57 s and 66.96 s |
| After coverage | `20261109-154053-work-verify-d2c7e1` | 1,992 passed, 78.618 percent, covered lines unchanged in all 65 modules |
| Final focused gate over the 15 changed test files | `20261109-154343-work-verify-98c9a6` | `./scripts/verify-focused`: ruff, ruff format, mypy, version sync, managed and template snapshots, 943 passed |
| Mutations, three batches (40 rows) | `20261109-153407-work-verify-ba789c`, `20261109-153613-work-verify-f26664`, `20261109-154416-work-verify-760c0c` | worktree `src/` diff exit code 0 after each batch |

An earlier mutation attempt (`20261109-153323-work-verify-b7b81a`) was interrupted on purpose after it showed a same-size `.pyc` cache hit, and rerun with `PYTHONDONTWRITEBYTECODE=1`.
