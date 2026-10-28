# Required Fleet claim implementation plan

Goal: provide one explicit run path that cannot begin shared work without a
confirmed Hub repo claim.

Architecture: strengthen the existing `repo_claim` context manager with an
opt-in admission requirement, then pass it through the existing run CLI and
detached child construction. Preserve bounded retry, holder cleanup and existing
abort-on-loss machinery.

Tech: Python standard library, existing pytest fixtures, existing Brigade gates.
No runtime dependency or schema change.

Spec: `docs/phase-trusted-team.md`, first-slice contract.

Global constraints:
- `require_claim` defaults to `False`; existing callers retain behavior.
- Required mode never yields an ungranted decision and always uses abort-on-loss.
- No shared or production Hub is used by tests.
- No runtime dependency, configuration format, schema, release or deployment.
- Preserve unrelated dirty files and the existing mesh/parallel-dispatch work.

Execution: one bounded implementer, test-first; parent owns final verification
and review. No transcribe tasks. Execute the checkboxes in order.

## Impact and file map

The code graph was synced on 2026-10-28. `repo_claim` resolves to
`src/brigade/fleet_client.py:1570`; its production caller is
`src/brigade/cli/run.py:767`. The impact query returned 106 edges at depth 2.
The broad file-level attribution returned 288 test files and 118 impacted
production files, including dynamic CLI consumers. This does not require
running every attributed test during iteration, nor prove unlisted dynamic
callers are unaffected.

Primary existing test owners are `tests/test_fleet_claims.py`,
`tests/test_fleet_claim_generations.py`, `tests/test_fleet_claim_release.py`,
`tests/test_fleet_claim_loss_cancellation.py` and `tests/test_run_cli.py`.

Files:
- Modify `src/brigade/fleet_client.py`: required admission and exception.
- Modify `src/brigade/cli/run.py`: parser, admission call and detached argv.
- Create `tests/test_fleet_claim_admission.py`: required/default boundary checks.
- Modify `tests/test_run_cli.py`: CLI and detach transport checks.
- Modify `docs/fleet-sync.md`: describe explicit required admission accurately.
- Modify `CHANGELOG.md`: one user-visible Unreleased entry after verification.

Review focus:
1. Ambiguous acceptance: a refused local start must still clean up a possible
   remote claim using the same holder and bound connection.
2. An unavailable identity/configuration must not become permission to start.
3. Continue-on-loss must not defeat an explicitly required claim.
4. Detached argv and parser conflicts must preserve the same policy.
5. A failed required start must release its local lock and terminalize artifacts.

## Task 1: required claim admission

Follow `src/brigade/fleet_client.py:1571` and existing real-Hub fixtures in
`tests/test_fleet_claims.py` and `tests/test_fleet_nodes.py`.

Interfaces:
- Consumes existing `ClaimDecision`, claim retry, orphan cleanup and loss policy.
- Produces keyword-only `require_claim: bool = False` on `repo_claim` and
  `FleetClaimRequiredError(FleetClaimHeldError)` for degradable refusal reasons.
- Error reasons are fixed labels: `no-hub`, `configuration-error`, `no-identity`,
  `hub-unavailable`, `missing` and `lease-expired` as applicable. Retain existing
  specific exceptions where they already refuse entry.

- [x] Record trim's authoring rationale for each new boundary test.
- [x] Write and run `test_required_claim_refuses_real_hub_outage`: configure a
  temporary enrolled client, stop its real Hub, enter with `require_claim=True`,
  assert `FleetClaimRequiredError` and `entered is False`. Observe RED before
  implementation. The default control must still enter with
  `granted is False` and `reason == "hub-unavailable"`.
- [x] Add parameterized no-Hub, configuration-error, no-identity and repeated
  ownerless-refusal cases; assert no guarded entry and the fixed refusal label.
- [x] Add real-Hub success and revoked-token controls. Existing auth refusal
  remains `FleetClaimAuthError`; a valid grant permits entry and releases on exit.
- [x] Add `test_required_ambiguous_acquire_keeps_holder_cleanup`: a lost response
  refuses entry while scheduling existing cleanup with the original holder and
  bound connection. Reuse established cleanup fixtures; do not add a test-only
  production seam.
- [x] Preserve terminal auth/held refusal cleanup behavior after an ambiguous
  first response. In particular, auth refusal must not start background release
  or retry rejected credentials. Required admission strengthens local entry,
  not the authority or scope of cleanup. Any uncertain remote residue remains
  subject to the existing TTL/recovery contract.
- [x] Add `test_required_claim_enforces_abort_loss_policy`: required mode cannot
  continue after loss with either the environment or explicit continuation
  option. Reuse confirmed-lease controls to avoid sleeping for TTLs.
- [x] Cover active credential rejection and unclassified heartbeat refusal in
  required mode. Use the existing bounded abort publisher, deliver credential
  detail once without duplicate generic loss publication, and test absent and
  blocking credential callbacks. Preserve default behavior and initial terminal
  refusal cleanup. The required CLI credential callback must request cancellation
  and leave its single interrupt to the claim publisher.
- [x] Implement the minimal admission checks; keep the module below its existing
  2000-line cap and avoid unrelated extraction or signature churn.
- [x] Run through Brigade with `./scripts/verify-focused` and selectors
  `tests/test_fleet_claim_admission.py`, `tests/test_fleet_claim_generations.py`,
  `tests/test_fleet_claim_release.py` and `tests/test_fleet_claims.py`.

## Task 2: run CLI and documentation

Follow parser and detached transport patterns in `src/brigade/cli/run.py` and
the guarded fake-worker fixtures in `tests/test_run_cli.py`.

Interfaces:
- Consumes Task 1's optional `require_claim` and existing claim error handling.
- Produces the planned `--require-fleet-claim` run flag. It is mutually exclusive
  with `--no-fleet-claim` and retained in detached child argv.

You see: required starts either hold a confirmed Fleet claim before agent
execution, or exit 2 with a fixed claim refusal and a terminal failure receipt.

- [x] Write `test_run_required_fleet_claim_refuses_without_worker_start`: the
  required admission path refuses, worker-call count remains 0, return code is
  2, the local lock is inactive, and enabled artifacts contain terminal failure.
  Observe RED before implementation.
- [x] Write parser conflict and detached-flag preservation checks at their real
  CLI/child-construction boundaries. Keep the ordinary no-flag adapter path.
- [x] Register the mutually exclusive flag, pass `require_claim=True` only when
  selected, and forward it to detached children. Reuse existing exception and
  terminal-receipt owners rather than inventing a second failure writer.
- [x] Update `docs/fleet-sync.md` to distinguish default local fallback from
  explicit required admission. Fix conflicting old auth-refusal prose only in
  the touched claim section, citing the current contract.
- [x] Add one conventional Unreleased changelog entry for the final behavior.
- [x] Run the focused gate through Brigade with
  `tests/test_fleet_claim_admission.py`, `tests/test_run_cli.py` and
  `tests/test_fleet_claim_loss_cancellation.py`.
- [x] Parent reviews the complete diff and runs the required full gate before
  PR delivery, with no full-slot timeout retries or bypasses. Initial receipt
  `20261028-051555-work-verify-ed2145`: 48127 passed, 0 failed, 269 skipped;
  combined coverage 85%. Earlier failed runs remain recorded. The final run
  used Python 3.12, four shards and a short private temporary root.
- [ ] Commit reviewed changes with a conventional message and Codex co-author.
  Parent retains in-house PR ownership through the existing gates and merge.

## Growth trigger

After named membership and project policy have an approved contract, that
shared-project path can select required admission automatically. This slice
does not silently label existing solo workspaces as shared, nor implement
membership, project ACLs, prepared-review automation or a new knowledge store.
