# Fleet Model Roster Authority Implementation Plan

Goal: make Fleet Hub the versioned model authority used by `brigade run` and
the pending `t3-fleet` guard, with exact provider/model/reasoning resolution,
permanent retired-family denial, node-bound LKG validation, and pre-mutation
audit.

Architecture: shared protocol validation lives in `fleet_model_roster.py`.
Hub persistence and transactions live in `fleet_hub_model_roster.py`. Client
transport, cache, admission, and doctor logic live in
`fleet_model_admission.py`. Existing Hub, client, CLI, and aboyeur modules remain
compatibility facades. Workers execute tasks in order, keep tests red before
production edits, commit each completed task, and update these checkboxes.

## File map

- Create `src/brigade/fleet_model_roster.py`: canonical JSON, schemas, model
  normalization, permanent retired-family matching, safe validators.
- Create `src/brigade/fleet_hub_model_roster.py`: schema migration helpers,
  revisioned mutations, roster projection/MAC, admission and audit transaction.
- Modify `src/brigade/fleet_hub.py`: install the next available schema version
  and delegate existing model policy and lease operations.
- Modify `src/brigade/fleet_hub_http.py`: pass caller identity/raw node bearer
  to roster reads and route admission actions.
- Create `src/brigade/fleet_model_admission.py`: bounded client transport,
  node-token MAC verification, descriptor-safe LKG, admission error classes,
  audit spool, doctor and reconciliation projection.
- Modify `src/brigade/fleet_client_cloud.py`: preserve cloud/model lease APIs and
  delegate versioned model snapshot/admission behavior.
- Modify `src/brigade/fleet_client.py`: compatibility exports.
- Modify `src/brigade/cli/fleet.py`: versioned list/set, retire/default/admit,
  doctor, and reconcile subcommands with stable exit codes.
- Modify `src/brigade/aboyeur_model_policy.py`: consume the validated versioned
  roster, reject permanent retirements, and persist admission provenance.
- Modify `src/brigade/aboyeur/orchestrator.py` only if the resolution type needs
  an admission field that cannot stay inside the existing receipt.
- Create `tests/test_fleet_model_roster.py`: Hub schema/API/MAC/revision/audit
  contract.
- Modify `tests/test_fleet_cloud.py`: compatibility and CLI mutation coverage.
- Modify `tests/test_fleet_model_admission.py`: cache, denial, and `brigade run`
  integration.
- Modify `tests/test_run_transport.py`: preflight terminalization and receipt
  compatibility.
- Modify `docs/phase-fleet-model-roster-authority.md` only when implementation
  proves a documented signature or field needs correction.

### Task 1: Hub roster schema and transactional API

**Files:**

- Create: `src/brigade/fleet_model_roster.py`
- Create: `src/brigade/fleet_hub_model_roster.py`
- Modify: `src/brigade/fleet_hub.py`
- Modify: `src/brigade/fleet_hub_http.py`
- Create: `tests/test_fleet_model_roster.py`
- Modify: `tests/test_fleet_cloud.py`

- [ ] Write failing tests for the shared retired-family matcher:

```python
@pytest.mark.parametrize(
    "model",
    ["gpt-5.4", "openai/gpt-5.4", "gpt-5.4-high", "gpt-5.5:preview"],
)
def test_permanent_retired_openai_families_match_structurally(model):
    assert fleet_model_roster.retired_reason("openai", model) == "permanently-retired"


def test_retired_family_match_does_not_catch_gpt_5_40():
    assert fleet_model_roster.retired_reason("openai", "gpt-5.40") is None
```

- [ ] Write failing Hub tests that initialize an old database and assert the
  next schema migration preserves existing model rows, seeds permanent
  `openai/gpt-5.4` and `openai/gpt-5.5`, creates revision `1`, and never stores
  a raw node token.
- [ ] Write a failing admin mutation test. `action=set` must require
  `expected_revision`, set exact `reasoning`, `brigade_cli`,
  `t3_instance_id`, and optional `t3_service_tier`, then increase the revision
  once inside `BEGIN IMMEDIATE`. Reusing the stale revision must return HTTP
  409 and leave every table byte-for-byte unchanged.
- [ ] Write failing tests for `action=set-default` and `action=retire`. A
  permanent retired row must reject removal, disable, or narrowing attempts.
- [ ] Write a failing node GET test. The response must use schema
  `brigade.fleet_model_roster.v1`, include exact seats/defaults/retirements,
  audience node ID, digest, and `hmac-sha256-node-bearer-v1`. Recompute the MAC
  in the test with sorted compact ASCII-safe JSON and `hmac.compare_digest`.
  Admin GET must omit audience and MAC and therefore be non-cacheable.
- [ ] Write a failing `action=admit` test. The Hub derives node ID from the
  bearer, resolves an explicit seat or consumer default, rejects incompatible
  bindings and retired models, records one idempotent audit row, and returns
  `brigade.model_admission.v1`. Reusing a request ID with different content
  returns 409. Expected revision or digest drift returns 409 before audit.
- [ ] Run RED through Brigade:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_fleet_model_roster.py","tests/test_fleet_cloud.py"]' --capture brigade-work
```

  Expect failure because the modules/schema/actions do not exist.
- [ ] Implement the shared protocol constants and validators. Canonical JSON is
  `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=True)`.
  The MAC message prefix is
  `b"brigade.fleet-model-roster.lkg.v1\0"`. Family-prefix matching permits only
  exact family or `-`, `/`, and `:` separators.
- [ ] Implement Hub tables `model_roster_meta`, `model_consumer_defaults`,
  `retired_models`, and `model_admission_audit`. Add exact reasoning and binding
  columns to `model_policy` using additive migration or deterministic table
  recreation that preserves old rows. Existing rows receive `reasoning=none`
  and empty consumer bindings until the operator updates them, so they cannot
  silently authorize T3.
- [ ] Implement online admission and audit in one `BEGIN IMMEDIATE`
  transaction. Audit fields are bounded safe metadata only. Denials use fixed
  reason codes and never echo request bodies.
- [ ] Keep the legacy `models` list in GET output and existing model lease
  semantics. Old clients continue to parse the response while new clients use
  the versioned fields.
- [ ] Run GREEN with the same Brigade command. Expect exit 0.
- [ ] Commit:

```bash
git add src/brigade/fleet_model_roster.py src/brigade/fleet_hub_model_roster.py src/brigade/fleet_hub.py src/brigade/fleet_hub_http.py tests/test_fleet_model_roster.py tests/test_fleet_cloud.py
git commit -m "feat(fleet): version the model roster"
```

### Task 2: Client admission, LKG, and operator CLI

**Files:**

- Create: `src/brigade/fleet_model_admission.py`
- Modify: `src/brigade/fleet_client_cloud.py`
- Modify: `src/brigade/fleet_client.py`
- Modify: `src/brigade/cli/fleet.py`
- Modify: `tests/test_fleet_model_admission.py`
- Modify: `tests/test_fleet_cloud.py`

- [ ] Write failing tests for a valid node-audience roster response. The client
  verifies schema, digest, audience, expiry, highest revision, and Hub MAC before
  atomically replacing the LKG.
- [ ] Write failing descriptor-safety tests. LKG and high-water files reject
  symlinks, FIFOs, non-owner files, group/world permissions, paths replaced
  between validation/open, and encoded content over 1 MiB. Windows without a
  safe no-follow primitive fails closed.
- [ ] Write failing fallback tests. A valid cache younger than 900 seconds is
  accepted only after timeout, connection error, or HTTP 5xx. HTTP 401/403/409,
  malformed authoritative JSON, bad digest/MAC, token rotation, audience drift,
  future timestamp, expiry, or revision rollback returns a typed denial and
  never rewrites the cache.
- [ ] Write failing admission transport tests for exact Hub success, LKG
  success, policy denial, and revision conflict. LKG success appends one safe
  owner-only audit-spool row without prompt, path, token, or provider body.
- [ ] Write failing CLI tests for:

```text
brigade fleet models admit --consumer t3-fleet --request-id ID --phase controller --json
brigade fleet models doctor --consumer t3-fleet --json
brigade fleet models reconcile --consumer t3-fleet --json
brigade fleet models retire openai gpt-5.4 --permanent --expect-revision N
brigade fleet models default set t3-fleet cursor_grok --expect-revision N
```

  Assert exit 0 success, 1 transport/auth/cache, 2 invocation/schema, 3 policy
  denial, and 4 revision/idempotency conflict.
- [ ] Run RED through Brigade:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_fleet_model_admission.py","tests/test_fleet_cloud.py"]' --capture brigade-work
```

- [ ] Implement a frozen `ModelAdmissionDecision` containing `ok`, `exit_code`,
  bounded `reason`, and safe payload. Never expose the bearer or raw Hub body.
- [ ] Implement cache writes using the existing Brigade owner-only directory,
  lock, descriptor, fsync, and atomic-replace patterns. The current node token
  verifies the Hub MAC. Token rotation invalidates the old file.
- [ ] Implement `admit_model`, `doctor_model_roster`, and
  `reconcile_model_roster`. Reconcile is read-only and reports drift. It does not
  mutate Hub or T3 configuration.
- [ ] Preserve `fetch_model_policy`, `set_model_policy`, model leases, and legacy
  monkeypatch seams through compatibility wrappers.
- [ ] Run GREEN with the same Brigade command. Expect exit 0.
- [ ] Commit:

```bash
git add src/brigade/fleet_model_admission.py src/brigade/fleet_client_cloud.py src/brigade/fleet_client.py src/brigade/cli/fleet.py tests/test_fleet_model_admission.py tests/test_fleet_cloud.py
git commit -m "feat(fleet): admit models from the hub roster"
```

### Task 3: `brigade run` admission provenance

**Files:**

- Modify: `src/brigade/aboyeur_model_policy.py`
- Modify if required: `src/brigade/aboyeur/orchestrator.py`
- Modify: `tests/test_fleet_model_admission.py`
- Modify: `tests/test_run_transport.py`

- [ ] Write a failing direct-worker test using a versioned Hub snapshot. Assert
  `model-policy.json`, `run.json`, and `roster.json` record roster revision,
  digest, exact provider/model/reasoning, and source.
- [ ] Write a failing permanent-denial test proving every `gpt-5.4`/`gpt-5.5`
  spelling stops in preflight before `agents.run_agent`, model lease acquisition,
  or worker process creation.
- [ ] Write a failing outage/LKG test. Configured Hub outage accepts a valid
  900-second LKG and rejects expired or invalid LKG with
  `failure.kind=fleet-model-policy`.
- [ ] Write a failing fallback-routing test proving a denied or retired seat can
  never return through seat-health fallback after admission pruning.
- [ ] Run RED through Brigade:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_fleet_model_admission.py","tests/test_run_transport.py"]' --capture brigade-work
```

- [ ] Update `resolve_fleet_model_policy` to consume the validated versioned
  snapshot while preserving standalone `unconfigured` behavior. Apply the local
  permanent retired floor before any Hub or local model can authorize work.
- [ ] Keep model leases invocation-scoped. Admission chooses the exact model.
  Lease acquisition still fences concurrency only when that seat is invoked.
- [ ] Persist the same `brigade.model_admission.v1` fields in the existing policy
  receipt. Do not add prompt text, provider output, or absolute paths.
- [ ] Run GREEN with the same Brigade command. Expect exit 0.
- [ ] Commit:

```bash
git add src/brigade/aboyeur_model_policy.py src/brigade/aboyeur/orchestrator.py tests/test_fleet_model_admission.py tests/test_run_transport.py
git commit -m "feat(run): persist fleet model admission"
```

### Task 4: Cross-repo adapter handoff, review, and completion gate

**Files:**

- Modify: `docs/phase-fleet-model-roster-authority-plan.md`
- Create: `.claude/memory-handoffs/2026-08-30-fleet-model-roster-authority.md`

- [ ] Run the combined focused gate through Brigade:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_fleet_model_roster.py","tests/test_fleet_cloud.py","tests/test_fleet_model_admission.py","tests/test_run_transport.py"]' --capture brigade-work
```

- [ ] Dispatch one independent Opus review of the complete diff against
  `docs/phase-fleet-model-roster-authority.md`. Fix verified Critical and
  Important findings through test-first sendback work.
- [ ] Run the full gate once through Brigade:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify"]' --timeout 3600 --capture brigade-work
```

- [ ] Write and lint the Memory Handoff. Include API schema, exact commands,
  cache rules, migration rules, receipts, and remaining live-deployment steps.
- [ ] Send the final `brigade fleet models admit` JSON and exit-code contract to
  the held `t3-fleet` conductor. It must remove project-default authority,
  revalidate at the target before mutation, and preserve its local permanent
  retired-family guard only as defense in depth.
- [ ] After the conductor's branch passes its own tests, verify one exact-model
  canary. A provider-only label cannot pass.
- [ ] Mark every completed checkbox in this plan and commit the plan/handoff.

## Completion conditions

- Hub schema/API, client cache/admission, CLI, and `brigade run` are committed and
  green through the serial full gate.
- Independent review has no unresolved Critical or Important findings.
- The held `t3-fleet` branch consumes the CLI contract and no longer reads the
  project default as authority.
- The canary records exact provider, model, reasoning, instance ID, roster
  revision, and digest before any real coding task is dispatched.
