# Fleet Cloud Workers implementation plan

## Goal

Make the beta fleet hub authoritative for cloud-agent admission and honest
about active work. Cursor Cloud, Codex Cloud, Claude Code Cloud, and Grok Bot
must share one safe status contract. Physical station tiles must contain only
recent non-terminal work. Execute the tasks in order and do not cut a release.

Approved design: `docs/phase-fleet-cloud-workers.md`.

Historical evidence: Brigade evidence `d6471048624c3e6b28cdba49` records that
the current `.brigade/cloud/registry.json` is local, Codex registers on dispatch,
Cursor is only wired/unwired, GitHub is branch/PR evidence, and sweep never
deletes.

## Architecture

- `fleet_hub.py` owns atomic cloud leases and provider circuit state in SQLite.
- `fleet_client.py` is the only client of the new authenticated hub surface.
- `cloud_tracker.py` preserves the local registry and normalizes provider and
  GitHub evidence. It never receives or persists provider credentials.
- `cursor_cloud.py` owns bounded read-only Cursor API calls using stdlib HTTP.
- `codex_cloud.py` acquires and releases shared admission around the existing
  Codex submit/poll/diff flow.
- `fleet_command_deck.py` projects physical runs and cloud leases separately.
- Claude joins the contracts but stays disabled at limit zero. No live Claude
  call is allowed in this slice.

The hub never calls a model provider. Dispatching nodes call providers locally,
then report sanitized state through `fleet_client.py`.

## Blast radius established before edits

- `src/brigade/cloud_tracker.py` feeds `cli/run_cloud.py`, Codex Cloud,
  Center activity, readiness, health, and reports. Direct regression ownership:
  `tests/test_cloud_tracker.py` and `tests/test_codex_cloud.py`.
- `src/brigade/fleet_command_deck.py` reads the shared event/claim database and
  feeds `/deck` and `/deck/repos`. Direct regression ownership:
  `tests/test_fleet_command_deck.py` and `tests/test_fleet_dashboard.py`.
- `src/brigade/fleet_hub.py` affects `src/brigade/cli/fleet.py`; its HTTP,
  schema, identity, and concurrency contracts are exercised by
  `tests/test_fleet_claims.py`, `tests/test_fleet_nodes.py`,
  `tests/test_fleet_sync.py`, and dashboard tests.
- `src/brigade/fleet_client.py` owns all authenticated node-side hub traffic and
  is covered by `tests/test_fleet_sync.py` and `tests/test_fleet_claims.py`.

## Task 1: honest physical station projection

**Owner:** one implementation worker.

**Files:**

- Modify `src/brigade/fleet_command_deck.py`
- Modify `tests/test_fleet_command_deck.py`
- Modify `tests/test_fleet_dashboard.py` only when an HTTP assertion is needed

- [ ] Add failing tests proving `run.dispatch.failed`, `.cancelled`, and
  `.timed_out` latest rows never appear in `fetch_live_runs` or station busy
  counts.
- [ ] Add a failing test proving a non-terminal row older than
  `stale_after_seconds` does not appear in station tiles or consume capacity,
  but can produce one bounded Needs You rail item.
- [ ] Run red through Brigade:
  `brigade work verify run --target . --command "pytest -q tests/test_fleet_command_deck.py tests/test_fleet_dashboard.py -k 'terminal_family or abandoned or station_busy'" --capture brigade-work`.
- [ ] Add one pure `is_terminal_state(state: str) -> bool` helper. Accept exact
  lifecycle terminals and the suffix families `.failed`, `.completed`,
  `.interrupted`, `.cancelled`, `.canceled`, `.timed_out`, and `.timeout`.
  Use the same helper for live reads, outcomes, failure buckets, and rail logic.
- [ ] Apply the active-age cutoff after latest-row selection and before result
  limiting. Preserve old non-terminal rows only as bounded abandoned attention;
  do not delete SQLite history.
- [ ] Run the focused tests green through Brigade.
- [ ] Commit `fix: remove terminal history from active fleet capacity`.

## Task 2: atomic cloud leases and subscription policy at the hub

**Owner:** one implementation worker after Task 1. This task owns all hub schema
and client transport files to avoid overlapping migrations and HTTP routes.

**Files:**

- Modify `src/brigade/fleet_hub.py`
- Modify `src/brigade/fleet_client.py`
- Modify `src/brigade/fleet_command_deck.py` for config shapes only
- Modify `src/brigade/cli/fleet.py`
- Modify `tests/test_fleet_claims.py`
- Modify `tests/test_fleet_sync.py`
- Modify `tests/test_fleet_command_deck.py`

- [ ] Add failing schema and concurrency tests for an additive v5 migration with
  `cloud_leases` and `cloud_provider_state`. Existing v4 databases and rows must
  survive.
- [ ] Add failing tests for one atomic admission transaction checking both the
  provider cap and global cap under concurrent requests. Initial policy is
  Cursor 3, Codex 2, Claude 0, global 4. Grok Bot remains tracked but does not
  consume these hosted-subscription slots.
- [ ] Add failing authentication tests: node tokens may act only as their node;
  dashboard cookies are read-only; unknown/revoked credentials cannot admit,
  renew, bind, release, or change a circuit.
- [ ] Add failing lease tests for a short unbound submission TTL, provider task
  binding, idempotent retry, renewal, fenced release, expiry, safe list payloads,
  and provider circuit close/open behavior.
- [ ] Run red through Brigade with the selected fleet tests.
- [ ] Extend startup-frozen deck config with validated cloud limits and enabled
  state. Absence gets the approved defaults. Never copy credentials into config.
- [ ] Add authenticated `GET /cloud` and `POST /cloud` routes. Supported actions:
  `admit`, `bind`, `renew`, `release`, and bounded circuit updates. Use node
  identity from the bearer, not an untrusted body value. Admission uses
  `BEGIN IMMEDIATE`, deletes/ignores expired rows, checks global and provider
  counts, and inserts once.
- [ ] Add `fleet_client` decisions and helpers mirroring the claim client's
  bounded timeout, stable auth failure, idempotency, and fail-closed semantics.
  A configured but unavailable hub denies a cloud launch. It does not fall back
  to local-only counting.
- [ ] Add `brigade fleet cloud --json` as an operator-safe view of active/all
  leases and policy. It prints no holder/fencing token.
- [ ] Run selected fleet tests green through Brigade.
- [ ] Commit `feat: add fleet-authoritative cloud admission`.

## Task 3: provider normalization, Cursor inventory, and Claude contracts

**Owner:** one implementation worker after Task 2.

**Files:**

- Create `src/brigade/cursor_cloud.py`
- Modify `src/brigade/cloud_tracker.py`
- Modify `src/brigade/cli/run_cloud.py`
- Modify `tests/test_cloud_tracker.py`
- Create or modify `tests/test_cursor_cloud.py`
- Modify `docs/technical-guide.md`

- [ ] Add failing tests for Cursor Basic-auth requests without leaking the key,
  bounded `GET /v1/agents` pagination, `latestRunId` resolution through the run
  endpoint, state normalization, retryable/unavailable errors, and active-only
  filtering. Network calls use fakes.
- [ ] Add failing tests proving `claude-cloud` is accepted by register/adopt and
  reported disabled with limit zero, while no `claude` subprocess is invoked.
- [ ] Add failing tests proving GitHub-only branch inference remains orphaned
  recovery evidence and never becomes active capacity.
- [ ] Add a failing CLI contract for `brigade run cloud sync --json`: provider
  observations reconcile safe active leases through `fleet_client`, terminal
  observations release them, and capacity refusals become Needs You records.
- [ ] Run red through Brigade:
  `brigade work verify run --target . --command "pytest -q tests/test_cloud_tracker.py tests/test_cursor_cloud.py" --capture brigade-work`.
- [ ] Implement `cursor_cloud.py` with stdlib `urllib`, Basic auth, a bounded
  request deadline, maximum pages/items, strict JSON shape checks, and sanitized
  exceptions. Never include headers or response bodies in errors.
- [ ] Add `claude-cloud` to provider and CLI choices. Report authority as
  `disabled-by-policy` until the hub limit is raised. Do not guess a Claude list
  API and do not run a live canary while usage is exhausted.
- [ ] Implement explicit `sync`; keep read-only `status` non-mutating. Sync may
  adopt an already-active browser-created Cursor task if admission remains, but
  cannot exceed the shared cap.
- [ ] Keep registry v1 read-compatible and avoid rewriting or deleting old
  entries during status/sync.
- [ ] Run provider tests green through Brigade.
- [ ] Commit `feat: reconcile cloud provider workers through the fleet`.

## Task 4: gate Codex Cloud dispatch and release capacity

**Owner:** one implementation worker after Task 3.

**Files:**

- Modify `src/brigade/codex_cloud.py`
- Modify `tests/test_codex_cloud.py`
- Modify `src/brigade/agents.py` only if the adapter boundary requires it

- [ ] Add failing tests proving admission happens before `codex cloud exec`, a
  denial invokes no provider process, submit failure releases the unbound lease,
  task ID binding follows submit, renewals follow running observations, and all
  terminal/error/timeout paths release capacity.
- [ ] Add a failing test proving the diff is still returned but never applied.
- [ ] Run the focused tests red through Brigade.
- [ ] Wrap the existing Codex Cloud flow with `fleet_client` admission. Preserve
  the current local registry, timeouts, process registry, prompt hashing, and
  result shape. Do not add automatic `codex cloud apply`.
- [ ] Make hub auth/unavailability a stable fail-closed launch result when fleet
  is configured. No-hub developer environments keep the existing local behavior
  only when no hub is configured at all.
- [ ] Run `tests/test_codex_cloud.py` and tracker regressions green through
  Brigade.
- [ ] Commit `feat: gate Codex Cloud dispatch on fleet capacity`.

## Task 5: Cloud Workers command-deck projection

**Owner:** one implementation worker after Task 4.

**Files:**

- Modify `src/brigade/fleet_command_deck.py`
- Modify `src/brigade/fleet_hub.py`
- Modify `tests/test_fleet_command_deck.py`
- Modify `tests/test_fleet_dashboard.py`

- [ ] Add failing pure and HTTP tests for a separate Cloud Workers section with
  Cursor, Codex, and Claude `used/limit`, circuit state, safe active task cards,
  and zero-work empty states.
- [ ] Prove cloud work never increments Rocinante, Shadowfax, or Gandalf busy
  counts. Prove expired/terminal leases move to outcomes or Needs You and do not
  render as active.
- [ ] Prove task labels, IDs, repos, and URLs are escaped, and no bearer,
  fencing token, API key, prompt, transcript, or raw provider error appears.
- [ ] Run the dashboard tests red through Brigade.
- [ ] Add immutable cloud projection dataclasses and fetch helpers. Supply safe
  lease and policy rows from the existing request-local hub connection.
- [ ] Render the new section in the current deck visual system without adding a
  client framework, dependency, or direct browser-to-provider request.
- [ ] Keep Recent Outcomes and Needs You bounded.
- [ ] Run the dashboard tests green through Brigade.
- [ ] Commit `feat: show cloud workers in the fleet command deck`.

## Task 6: full verification, bounded security review, and beta rollout

**Owner:** orchestrator for verification/deploy; Daybreak is read-only scan only.

- [ ] Run focused integration tests through Brigade:
  `brigade work verify run --target . --command "pytest -q tests/test_cloud_tracker.py tests/test_cursor_cloud.py tests/test_codex_cloud.py tests/test_fleet_command_deck.py tests/test_fleet_dashboard.py tests/test_fleet_claims.py tests/test_fleet_sync.py tests/test_fleet_nodes.py" --capture brigade-work`.
- [ ] Capture the outcome against the implementation run/card.
- [ ] Dispatch one bounded Daybreak review of only auth, secret handling, lease
  fencing, and SQLite concurrency. Do not run repetitive scans.
- [ ] Fix accepted findings test-first, then rerun the focused integration gate.
- [ ] Run the completion gate through Brigade:
  `brigade work verify run --target . --command "timeout 3600 ./scripts/verify" --capture brigade-work`.
- [ ] Write and lint the Memory Handoff.
- [ ] Deploy the verified beta checkout to the hub CT and update Brigade on
  Rocinante, Shadowfax, and Gandalf using the device-fleet runbook. Preserve each
  machine's own node identity and token files.
- [ ] Update `/etc/brigade/command-deck.json` with physical 10/8/4 and approved
  cloud 3/2/0/global-4 limits. Restart only the fleet hub service after config
  validation.
- [ ] Run read-only Cursor and Codex inventory canaries. Do not call Claude.
- [ ] Run `brigade run cloud sync --json`, `brigade fleet cloud --json`,
  `brigade fleet status --all`, and `brigade fleet claims`; then flush any
  preserved spool.
- [ ] Verify `/health`, `/deck`, and `/deck/repos` through Tailscale. Confirm no
  terminal or over-age row consumes station capacity and Cloud Workers shows
  provider caps even at zero active.
- [ ] Launch at most one Cursor Cloud canary only after its lease is visible.
  Do not auto-apply or auto-merge its output. Claude stays disabled.
- [ ] Record exact receipts, service status, deployed commit, and rollback ref.

## Worker routing and safety

- Use `ox_worker` for implementation while Ox Alpha is available. Its Grok
  fallback is forbidden for this task; if Ox fails, stop that run and explicitly
  route the remaining work to the Codex Terra `coder` seat.
- Do not use Cursor Grok or Composer. Do not use Claude seats while Max usage is
  exhausted. Do not use retired GPT-5.3 Spark or GPT-5.5.
- Use `daybreak` once for the bounded read-only security review in Task 6.
- Every behavior change follows red-green-refactor. Every test that counts runs
  through `brigade work verify run`, followed by outcome capture.
- No push, merge, tag, release, event deletion, branch deletion, auto-apply, or
  auto-merge is authorized.
