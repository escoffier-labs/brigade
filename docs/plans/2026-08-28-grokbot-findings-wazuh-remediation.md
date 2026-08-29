# Grok Bot findings relay and Wazuh remediation

## Goal

Turn normalized Grok Bot, Fleet Steward, Backup Steward, and Wazuh findings into a single Brigade-owned path that is visible in Fleet Hub, reviewable by Rocinante, and capable of bounded, approval-gated remediation without exposing a generic shell.

## Architecture and execution rules

`brigade.grokbot.findings.v1` remains the private producer boundary. Brigade validates and fingerprints records, persists one deterministic opaque Fleet event in a durable Grok Bot outbox, reports it through unchanged `fleet_client.report_event`, and writes the complete untrusted finding body as a Memory Handoff draft into Rocinante's canonical-owner review inbox. Brigade never edits canonical memory and never calls the Fleet Hub HTTP API except through `report_event`. The Wazuh pack is a first-party producer and exposes only bounded alert ingest, triage, and review tools. Fleet Steward consumes classified findings through a closed action catalog; every mutation is target-bound, current-state checked, claimed, verified, receipted, and operator-approved.

Execute these three slices in order. Each slice is an independently shippable PR. Do not combine slices or cut over the existing external sidecar until all three slices and their acceptance checks pass.

## File map

### Slice 1, shared findings relay

- Modify `src/brigade/grokbot_findings.py`: retain strict manifest validation and owner-review draft writing; add `apply_entries(target, owner, entries, limit=..., now=...)` so validated in-memory entries use the existing atomic writer without a temporary manifest.
- Create `src/brigade/grokbot_findings_relay.py`: preflight entries, then select, spool, and deliver one bounded batch under the existing Grok Bot queue lock. Persist private descriptor-safe mode-0600 opaque events before draft delivery, mark them ready after delivery, and call unchanged `fleet_client.report_event`. Do not use `report_external_event`; it regenerates `ts`, `sequence`, and `digest`.
- Modify `src/brigade/cli/run_cloud.py`: add the flat `brigade run cloud grokbot relay-findings` command with preview as the default and `--apply` as the only write switch. Public stdout/JSON is counts plus irreversible relay IDs only. Keep existing `reconcile-findings` and `convert-findings` commands unchanged.
- Create `tests/test_grokbot_findings_relay.py`: outbox crash/replay, identical-event retry, `report_event` call capture, truthful pending counts, pathlike-handle leak coverage, and review-inbox tests. Do not add `tests/test_fleet_client.py`.
- Modify `tests/test_grokbot_findings.py`: lock compatibility with the existing manifest and the new in-memory apply API.

### Slice 2, first-party Wazuh triage pack

- Create `src/brigade/grokbot_wazuh/__init__.py`: pack exports and lifecycle entry points.
- Create `src/brigade/grokbot_wazuh/contracts.py`: strict alert, fingerprint, classification, and public-result schemas.
- Create `src/brigade/grokbot_wazuh/normalize.py`: bounded Wazuh JSON normalization, secret/detail redaction, and deterministic fingerprinting.
- Create `src/brigade/grokbot_wazuh/policy.py`: explicit suppress/watch/escalate rules, expiry, and action eligibility.
- Create `src/brigade/grokbot_wazuh/store.py`: private bounded dedupe/classification state with atomic writes and mode checks.
- Create `src/brigade/grokbot_wazuh/tools.py`: `wazuh_ingest`, `wazuh_alert_status`, `wazuh_classify`, `wazuh_incident_bundle`, `wazuh_propose_remediation`, and `wazuh_action_status`.
- Create `src/brigade/grokbot_wazuh/lifecycle.py`: preview-first pack setup, doctor, canary, and service rendering using the existing pack lifecycle conventions.
- Modify `src/brigade/grokbot_packs.py` and `src/brigade/cli/run_cloud.py`: register `wazuh-triage`, bind it to a non-colliding default port, and preserve existing pack commands.
- Create `tests/test_grokbot_wazuh_contracts.py`, `tests/test_grokbot_wazuh_normalize.py`, `tests/test_grokbot_wazuh_policy.py`, `tests/test_grokbot_wazuh_store.py`, `tests/test_grokbot_wazuh_tools.py`, and `tests/test_grokbot_wazuh_lifecycle.py`.
- Modify `tests/test_grokbot_packs.py` and `tests/test_run_cloud.py`: registry, CLI, collision, and rollback coverage.

### Slice 3, Fleet Steward bounded remediator

- Modify `src/brigade/grokbot_fleet/contracts.py`: add typed action/proposal/result fields for approved Wazuh-derived findings without accepting command, path, credential, or host input.
- Modify `src/brigade/grokbot_fleet/probes.py`: add fixed verification probes for the first approved remediation targets.
- Modify `src/brigade/grokbot_fleet/actions.py`: define the closed action catalog and rollback metadata.
- Modify `src/brigade/grokbot_fleet/policy.py`: bind Wazuh finding fingerprints, Fleet targets, maintenance windows, and approval state.
- Modify `src/brigade/grokbot_fleet/tools.py`: implement proposal, current-state recheck, Fleet claim, execution, verification, rollback receipt, and replay rejection in that order.
- Modify `src/brigade/grokbot_fleet/app.py` and `src/brigade/grokbot_fleet/lifecycle.py`: compose the remediator while retaining the observer-only canary and existing private state paths.
- Create `tests/test_grokbot_fleet_remediation.py`: tests for stale findings, wrong target, missing approval, lost claim, failed verification, rollback, replay, and successful bounded action.
- Modify existing Fleet contract, tool, app, and lifecycle tests to prove observer behavior remains unchanged for non-actionable findings.

## Dependencies and fixed decisions

Slice 1 is the shared transport and memory boundary. Slice 2 depends on it for sanitized Fleet Hub status and full-content Rocinante review delivery. Slice 3 depends on Slice 2's classification and fingerprint contract so only `escalate` findings with an action catalog entry can produce proposals. A Wazuh alert never directly executes a Fleet action.

The first Wazuh rules are fixed to known, bounded classes: service failure, disconnected agent, critical storage, and selected high-confidence security events. SCA compliance repeats, expected Windows installer/logon noise, known LXC pseudo-file checks, and port-change observations without a registered action remain `watch` or `suppress`, never automatic execution. Suppression entries require a reason, source fingerprint, scope, creation time, and expiry. Unknown or malformed alerts are retained only as redacted review records and classified `watch`.

Fleet Hub receives only the persisted opaque `report_event` object: irreversible `run_id` digest, fixed `seat`/`harness` labels, allowlisted `finding.<severity>` state, and stable `ts`/`sequence`/`digest`. It must not receive producer, finding ID, `source_ref`, `source_digest`, title, body, usernames, addresses, paths, commands, credentials, or raw Wazuh payloads. The full body is written to the canonical owner's review inbox with an explicit untrusted-content header. Rocinante/OpenClaw performs canonical ingestion and user notification; no pack edits `MEMORY.md` or a memory card.

## Slice 1 test-first recipe

- [x] Write `tests/test_grokbot_findings_relay.py::test_preview_projects_only_relay_ids_and_writes_nothing`; assert preview returns counts plus irreversible relay IDs and creates no draft, marker, outbox, or spool file.
- [x] Run `pytest -q tests/test_grokbot_findings_relay.py::test_preview_projects_only_relay_ids_and_writes_nothing`; expect failure because the opaque preview contract is absent.
- [x] Implement `relay_preview(records, target, owner)` using the existing exact-key manifest validator and return only counts plus irreversible relay IDs.
- [x] Run the same command; expect one passing test.
- [x] Write `test_apply_writes_full_untrusted_body_and_calls_unchanged_report_event`; use a body containing a fake token-shaped string and assert the draft preserves it while the captured `report_event` object does not.
- [x] Run that test; expect failure because the durable outbox and `report_event` path are absent.
- [x] Keep `grokbot_findings.apply_entries(target, owner, entries, limit=DEFAULT_LIMIT, now=None)` as the shared delivery API. The function validates every entry before opening storage, sorts by `(producer, finding_id, revision)`, and never serializes entries to a temporary manifest.
- [x] Implement `relay_apply`: preflight, then select, spool, and deliver the bounded batch under one queue lock. Persist one event whose `run_id` is the irreversible digest, with fixed seat/harness labels, allowlisted severity state, and stable `ts`/`sequence`/`digest`. Mark the outbox reported only when `report_event` returns True.
- [x] Run `pytest -q tests/test_grokbot_findings_relay.py`; expect all relay tests to pass.
- [x] Add tests for crash-before-delivery, crash-before-ready, contention, `report_event` False/raise replay of the identical event, replay without the original batch, duplicate delivery, interrupted draft recovery, invalid schema, bounded batch size, pathlike-handle leakage, and Fleet Hub 401/5xx spool behavior.
- [x] Add the flat CLI parser and run its focused parser coverage; expect preview/apply options and no credential-valued options.
- [x] Verify through Brigade. Receipt `20260828-212534-work-verify-74793f` completed with 97 focused tests passing.
- [x] Complete independent Critical/Important review and sendback.

## Slice 2 test-first recipe

- [x] Write `tests/test_grokbot_wazuh_contracts.py::test_ingest_rejects_unbounded_or_secret_fields`; assert unknown keys, oversized body, raw credentials, and caller-supplied command/path values are rejected.
- [x] Run `pytest -q tests/test_grokbot_wazuh_contracts.py::test_ingest_rejects_unbounded_or_secret_fields`; expect failure because the pack does not exist.
- [x] Implement the strict contracts and bounded normalizer. The normalized record must contain producer, finding ID, revision, observed time, severity, title, redacted body, source reference, source digest, and content digest.
- [x] Run the test; expect it to pass.
- [x] Write `tests/test_grokbot_wazuh_policy.py::test_known_noise_expires_to_watch_and_high_confidence_event_escalates`; assert SCA repeats and known installer events classify as suppress/watch with expiry, while a cataloged high-confidence event classifies as escalate.
- [x] Run that test; expect failure because classification is absent.
- [x] Implement deterministic classification precedence: malformed/unknown -> watch, expired suppression -> watch, explicit suppression -> suppress, matching high-confidence rule and scope -> escalate, otherwise watch.
- [x] Run `pytest -q tests/test_grokbot_wazuh_contracts.py tests/test_grokbot_wazuh_normalize.py tests/test_grokbot_wazuh_policy.py`; expect all tests to pass.
- [x] Write `tests/test_grokbot_wazuh_tools.py::test_ingest_dedupes_and_emits_one_review_finding`; assert repeated identical fingerprints produce one current finding, one sanitized relay event, and one review draft.
- [x] Run it; expect failure because storage/tools are absent.
- [x] Implement the private atomic store and six public tools. `wazuh_ingest` accepts only bounded normalized alert batches, `wazuh_alert_status` returns counts and last-seen timestamps, `wazuh_classify` returns category and reason, `wazuh_incident_bundle` returns grouped public findings, `wazuh_propose_remediation` creates a proposal only for `escalate`, and `wazuh_action_status` returns opaque lifecycle state.
- [x] Run `pytest -q tests/test_grokbot_wazuh_tools.py tests/test_grokbot_wazuh_store.py`; expect all tests to pass with no raw body in public results.
- [x] Write lifecycle tests for default bind collision, preview-only setup, mode-0600 state, canary read path, failed second-write rollback, and exact tool inventory; run `pytest -q tests/test_grokbot_wazuh_lifecycle.py tests/test_grokbot_packs.py` (no `tests/test_run_cloud.py` in this tree; CLI pack/serve coverage lives in the pack and lifecycle tests) and expect all tests to pass.
- [x] Verify through Brigade: `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_grokbot_wazuh_contracts.py","tests/test_grokbot_wazuh_normalize.py","tests/test_grokbot_wazuh_policy.py","tests/test_grokbot_wazuh_store.py","tests/test_grokbot_wazuh_tools.py","tests/test_grokbot_wazuh_lifecycle.py","tests/test_grokbot_packs.py"]' --capture grokbot-wazuh-triage-pack`; receipt `20260828-224612-work-verify-9e7954` completed with 62 focused tests passing.
- [x] Commit only the Slice 2 files with `git add src/brigade/grokbot_wazuh src/brigade/grokbot_packs.py src/brigade/cli/run_cloud.py tests/test_grokbot_wazuh* tests/test_grokbot_packs.py && git commit -m "feat: add first-party Wazuh triage pack"`.

## Slice 3 test-first recipe

- [x] Write `tests/test_grokbot_fleet_remediation.py::test_proposal_requires_escalated_current_finding_and_operator_approval`; assert suppress/watch, stale revision, wrong target, missing approval, and expired approval all reject without invoking an executor.
- [x] Run `pytest -q tests/test_grokbot_fleet_remediation.py::test_proposal_requires_escalated_current_finding_and_operator_approval`; expect failure because the Wazuh binding and action catalog are absent.
- [x] Add the fixed typed catalog and policy binding. The first action must name one registered service/target, one verification ID, one rollback ID, one maintenance window, and one maximum blast radius. No input may contain a command, shell, path, username, address, credential, or environment value.
- [x] Run the test; expect it to pass.
- [x] Write `test_execute_claims_rechecks_verifies_and_records_receipts`; assert the exact call order is approval read, finding revision read, live-state recheck, Fleet claim, executor, verification, and receipt. Assert a failed verification executes the fixed rollback policy or returns a failed receipt when rollback is not permitted.
- [x] Run that test; expect failure because the execution path is absent.
- [x] Implement the typed proposal/execution path with single-use approval, current-state binding, Fleet claim/renew/release, replay protection, bounded executor arguments, post-action verification, and sanitized receipts.
- [x] Run `pytest -q tests/test_grokbot_fleet_remediation.py tests/test_grokbot_fleet_tools.py tests/test_grokbot_fleet_app.py tests/test_grokbot_fleet_lifecycle.py`; expect all tests to pass.
- [x] Write `test_non_catalogued_wazuh_findings_remain_review_only`; assert every suppress/watch/unknown finding and every protected, appliance, family, container, or indirect target remains proposal-ineligible.
- [x] Run the test; expect failure if any broad path is accidentally executable; repair policy until it passes.
- [x] Verify through Brigade: `brigade work verify run --target . --command "pytest -q tests/test_grokbot_fleet_remediation.py tests/test_grokbot_fleet_tools.py tests/test_grokbot_fleet_app.py tests/test_grokbot_fleet_lifecycle.py" --capture grokbot-fleet-bounded-remediator`; expect a successful receipt. Receipt `20260828-233645-work-verify-88fa05` completed with 46 focused tests passing.
- [x] Run the full repository verification through Brigade after the focused receipt, then perform independent security review for command injection, target substitution, secret leakage, replay, stale approvals, claim loss, and rollback behavior. Receipts `20260829-160756-work-verify-bbfb45` and `20260829-161747-work-verify-2fa1de` covered 10,479 tests with 82.72% coverage; the xdist-unsafe cases were rerun serially. Final focused remediation receipt `20260829-162423-work-verify-3c22ec` passed 67 tests after the independent review findings were closed.
- [x] Commit only the Slice 3 files with `git add src/brigade/grokbot_fleet tests/test_grokbot_fleet_remediation.py tests/test_grokbot_fleet_*.py && git commit -m "feat: add approval-gated fleet remediation"` (`bcf53231`).

## End-to-end acceptance

Use one synthetic Wazuh alert and one real read-only ingest before enabling any live action. The alert must be normalized by `wazuh-triage`, classified, emitted through `grokbot_findings_relay`, visible as sanitized Fleet Hub coordination, and present in Rocinante's review inbox with its full untrusted body. A second identical ingest must produce no second draft or event. Only after that evidence exists may an operator approve one cataloged action, observe the claim, verify, and confirm the sanitized receipt. The test must prove no alert body or credential entered Fleet Hub, Brigade receipts, action state, or canonical memory.

The first production canary remains approval-only. No standing approval, automatic Wazuh active response, package update, reboot, Windows mutation, protected-host mutation, or generic shell execution is part of these slices.
