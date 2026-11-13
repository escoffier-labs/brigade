# Stage 1 Generic Connector and Grok Bot Removal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace product-specific cloud memory and messaging access with one isolated MCP profile, then remove Grok Bot execution machinery without deleting user data.

**Architecture:** Extend the existing `src/brigade/dot/` connector with a closed `cloud-agent` profile and optional messaging. Move the shared lifecycle, transport and fleet-runner helpers out of Grok-named modules before removing those modules. Keep Dot and Duet behavior, memory authorization and the hub wire contract intact.

**Tech Stack:** Python 3.11+, standard library, existing optional MCP and JWT dependencies, pytest, Brigade verification, existing systemd unit rendering.

**Spec:** `docs/superpowers/specs/2026-11-12-brigade-stack-trim-design.md`, Stage 1. The current owner instruction settles its open questions: hub messaging stays in core and is separately enabled within the generic profile. The design file still says draft, but the owner supplied it as approved for this planning task.

## Owner amendments (2026-11-12)

These override anything below that conflicts with them.

1. **Connector commands live under the hub.** The lifecycle group is
   `brigade hub connector {list,show,setup,doctor,canary,install-service,update,remove,serve}`,
   registered from `src/brigade/cli/hub_connector.py` under a new top-level
   `hub` group that this PR creates with only the `connector` subgroup. The
   hub is becoming the core of the product and the `run` area is being
   trimmed, so nothing new is added under `run cloud`. Old Dot and Duet
   invocation routes under `run cloud` stay as compatibility aliases.
2. **A token is the baseline authentication. Cloudflare Access is optional.**
   The `cloud-agent` runtime accepts an `auth` block in one of two modes:
   - `mode: "token"` (default): the listener requires
     `Authorization: Bearer <token>` on every request, compared in constant
     time against the profile's own protected token file. That file is
     distinct from the hub principal token, is validated with the existing
     protected-file checks, and is never the node credential. A missing,
     malformed or wrong token gets 401 with no detail. Both modes require a loopback bind. Remote access uses the operator's
     TLS tunnel or reverse proxy connecting to loopback. The prior allowance
     for a non-loopback bind with an allowed-hosts list is withdrawn.
   - `mode: "access"`: today's signed-identity verification, unchanged, with
     its audience check.
   Dot and Duet keep requiring Access. Only `cloud-agent` gains token mode.
   `doctor` reports the active mode. Token mode is not a failure or a warning.
3. **Tests for amendment 2 belong to Task 1:** token accepted, wrong token
   refused, missing header refused, token file shared with another profile
   or with the hub principal refused at configuration time, non-loopback bind
   refused in both modes even with allowed hosts, and Access mode behavior unchanged. Add to
   Review Focus: a token-mode listener must not accept an Access assertion in
   place of the token, and must not log the token.
4. **Task 1's pull request gets a security review** on its current head
   before merge, because it adds an authentication path.
5. Task 6's guide documents token mode first and Access as the option for
   clients that need a signed-identity front.

## Follow-ups from the Task 1 reviews

Task 1 merged. These were judged safe to follow and belong to the tasks named:

- **Task 2:** classify a connector as cloud-agent from a normalized profile
  identity, so a library caller cannot evade the exact-string check with a
  malformed pack id. Run setup rollback for any exception after the token
  file is created, not only the expected ones.
- **Task 6 (guide):** say that the failed sign-in limit reduces noise and is
  not brute-force protection, since a valid credential is always served. Say
  that when cloud-agent is installed, the other profiles' isolation check
  also reads the machine's fleet credentials and refuses if they cannot be
  read. Say that cloud-agent listens on loopback only and remote access goes
  through the operator's own tunnel or proxy.

## Decisions made by the planner

1. Keep Dot and Duet profiles and their current tools, runtime schemas, credentials and stored configuration working. Folding them into the generic profile is follow-up work because their dispatch and instruction contracts differ.
2. Name the new packaged profile `cloud-agent`, runtime schema `brigade.cloud-agent.runtime.v1`, principal `agent:cloud-agent`, service `brigade-cloud-agent.service`, and state root `/var/lib/brigade-cloud-agent`. Each deployment uses its own protected token and Access audience. No automatic credential reuse from old profiles.
3. Default `messaging_enabled` to false. False exposes exactly three memory tools. True exposes those three plus four messaging tools. Both variants use the same profile and principal, with hub grants bounding message recipients.
4. Add `brigade hub connector` as the canonical lifecycle path for all surviving connector profiles. Keep old Dot/Duet invocation routes as compatibility aliases, without importing Grok modules. Removed Grok operations use one shared tombstone handler for one release.
5. Use six ordered PRs against `overhaul`. Required import, test and registration cleanup travels with each deletion, so each intermediate PR imports cleanly.
6. Preserve legacy SQLite schema creation and read-only historical projections needed by existing databases in a small neutral storage helper. Remove job mutation, admission, leases and background workers. Do not drop tables, rewrite records or erase files.
7. Treat the 29,389 Python lines in the whole-file manifest as gross source deletion. Shared helper relocation and the generic profile reduce net savings. Measure net source lines and all four size counters on each actual PR head.
8. State a temporary additive size exception in PR 1 for the replacement profile and regression tests while both profiles coexist, as permitted by the design's stated-exception rule. Never raise a legacy module cap. Later PRs lower the recorded budget. If the Stage 0 aggregate budget gate has no exception mechanism, land shared-helper deduplication sufficient to offset additions in PR 1 before merging it.
9. Record the removal release once in the tombstone module at implementation time using the release version that will ship Stage 1. Do not guess a future release number in this plan or bump the package just to fill it.

## Global Constraints

- All implementation PRs target `overhaul`, never `main`. This plan is local-only and does not authorize execution, push or a PR.
- The live fleet keeps working at every stage. Every stage is an ordinary release.
- Stored data that users own (memory cards, handoffs, receipts, evidence) stays readable. Removing a command never deletes user data. Queue/job stores, plugin ledgers and listener runtime directories also remain.
- A removed command prints a "this was removed in version X" message for one release, then disappears. The tombstone names the actual removal version and the replacement or migration guide.
- Generic profile first. Delete Grok memory/inbox profiles only after tool-list, memory-search, private-card and untrusted-handoff parity passes on the existing connector checks.
- Hub messaging stays in core. Dispatch, claim, fleet and skill tools are absent from generic discovery and cannot be invoked directly, regardless of runtime input.
- Keep distinct principal, protected token, state directory and Access audience. No host-node credential fallback and no authority inferred from a client name.
- Zero new runtime dependencies. Existing optional MCP/JWT packages remain optional. No new top-level harness, depth or include.
- CI enforces a 2,000-line module ceiling and a size ratchet (`scripts/check_size_ratchet_baseline.py`). Its baseline is `LEGACY_OVERSIZED` in `tests/test_module_size_ratchet.py`. Remove deleted entries, lower surviving changed entries to observed counts, remove entries once below 2,000, and never raise or add caps. Each PR updates applicable entries downward or records that none changed.
- Re-run the size comparison with `--base-ref origin/overhaul`. The CI workflow already uses `github.base_ref`, so preserve that selection. Stage 0 aggregate size counters are separate from this per-module cap mapping.
- Conventional commits, Codex co-author trailer, and one `changelog.d/stage-1-<slice>.md` fragment per user-visible PR. Never edit `CHANGELOG.md`.
- No changes anywhere under `engines/hub/`. Claims, heartbeat, schema snapshots and upgrade fixtures stay until a contract v2 hub release. Appendix D names the protected paths.
- Preserve safety rails: `work_mode.py`, `guard/`, credential/secret scanning and pre-push publication checks. No bypasses or relaxed skip/coverage gates.
- Public text contains no private machine identifiers, private counts, home paths, em dashes or inspiration-project comparisons. Research inputs inform retirement obligations but are not cited or copied.
- Run checks through Brigade. Required CI supplies full suite, 78% coverage, platform and merge gates on the actual head. Local full verification is opt-in. Never retry full-gate status 75 with a larger timeout.
- Actual dashboard/icon/interaction changes need matching real before/after screenshots in their PR, including affected responsive states. Text-only PRs state: `Text-only change; no UI/layout change`. Do not create Markdown screenshots for wording changes.

## Review Focus

1. Disabled messaging or a hostile runtime containing dispatch keys must expose only memory tools, even when the caller invokes a hidden tool by name. Task 1 pins discovery and direct-call refusal.
2. A reused token, shared state path or wrong Access audience must fail closed rather than borrowing Dot, Duet or node authority. Task 1 pins cross-profile rejection and missing-credential refusal.
3. Old generated Dot/Duet services and module entry points must still start after Grok packages disappear. Tasks 1 and 4 pin argv compatibility and optional-dependency import behavior.
4. Existing populated queue stores and historical events must stay readable with no background mutation after removal. Tasks 4 and 5 pin byte-preserving tombstones, SQLite compatibility and safe historical rendering.
5. Outstanding inbox deliveries and handoff retries across endpoint migration must retain grant checks, deduplication and untrusted marking. Tasks 1, 2 and 6 pin retry behavior and migration order.

---

## Groundwork and execution protocol

Survey base: `7a23fc0ac031dc36f8529ea6891a7091d34ebeb1`, matching `origin/overhaul` when this plan was written. The checkout was clean before the plan. Manifest counts are physical lines including blank lines/comments from tracked files, using `git ls-files` and file line counts. They are not the design's approximate implementation estimate.

`brigade code impact grokbot_jobs --json` failed because the code-graph database could not be opened read-only. Code graph coverage is skipped. Fallback: scan tracked Python for imports and qualified references with `rg -n 'grokbot|grok.bot' src tests registry scripts`, identify enclosing functions using Python AST, and inspect shared call sites. Every surviving source importer found by this scan is in Appendix B. In particular, `dot/fleet_tools.py` imports `grokbot_t3.fleet`, `dot/lifecycle.py` imports `grokbot_mcp`, `grokbot_ops`, `grokbot_packs` and `_write_rendered` from `grokbot_t3.lifecycle`, and `cli/fleet_dot.py` imports the protected token reader. These must move before deletion. This is static evidence, not proof about external plugins or dynamic imports.

Before each implementation PR, run `brigade work brief --target .`, inspect dirty paths/claims, and retry `brigade code impact <deleted-module-or-changed-symbol> --json` or `brigade code callers <symbol> --json` if a current graph exists. Record the actual impact set and test selectors. If unavailable, repeat tracked import grep plus import smoke tests, including `registry/skills/verify-brigade/`. No separate agent dispatch is required by this plan.

Verification commands below use a shared notation: `FOCUSED <selectors...>` means exactly `brigade work verify run --target . --command "./scripts/verify-focused <selectors...>" --capture brigade-work`. Pass each selector as a separate argument, never one quoted space-separated selector. The script accepts multiple selectors and includes lint, formatting, mypy, version sync, managed and template-profile snapshots. `CHECK <command>` means exactly `brigade work verify run --target . --command "<command>" --capture brigade-work`. Every named test path below exists at the survey base unless marked Create.

For each code PR, regenerate inventory with `CHECK env BRIGADE_EXTRAS=1 brigade roadmap commands --write`, then run `FOCUSED tests/test_cli_help.py tests/test_cli_inventory_contract.py tests/test_module_size_ratchet.py tests/test_size_ratchet_baseline.py`, `CHECK python3 scripts/check_size_ratchet_baseline.py --base-ref origin/overhaul`, and `CHECK python3 scripts/changelog_fragments.py check`. Include those files in the PR. Required CI remains authoritative for full coverage. Regenerate snapshots through their existing writers only if the intended interface changes. Keep `tests/fixtures/dot_tools_snapshot.json` unchanged for Dot. Pin generic tool lists in a new fixture, not by overwriting Dot's.

### Task 1: PR 1, add the generic profile and independent lifecycle

**Files:**
- Modify: `src/brigade/dot/{profile,runtime_config,adapter,lifecycle,__init__}.py`, `src/brigade/cli/run_cloud.py`, `src/brigade/grokbot_packs.py` (delegate surviving-profile lifecycle only).
- Create: `src/brigade/dot/connector_registry.py`, `src/brigade/cli/hub_connector.py`, `tests/test_cloud_agent_profile.py`, `tests/test_connector_registry.py`, `tests/fixtures/cloud_agent_tools_snapshot.json`.
- Modify/Test: `tests/test_dot_lifecycle.py`, `tests/test_dot_runtime.py`, `tests/test_duet_profile.py`, `tests/test_dot_access_auth.py`, `tests/test_dot_handoff_write_path.py`, `tests/test_dot_memory_tools.py`, `tests/test_dot_inbox_tools.py`, `tests/test_grokbot_packs.py`.
- Modify: common inventory/baseline files above. Create `changelog.d/stage-1-generic-profile.md`. Add setup instructions to `docs/duet-connector.md` temporarily, then consolidate in Task 6.

**Interfaces:**
- Consumes: `ConnectorProfile`, `Runtime`, `parse_runtime(payload: object, *, schema: str) -> Runtime`, `profile_for(pack_id: str) -> ConnectorProfile`, existing principal memory and inbox implementations.
- Produces: `CLOUD_AGENT: ConnectorProfile`, `CLOUD_AGENT_MEMORY_TOOLS: frozenset[str]`, `CLOUD_AGENT_MESSAGE_TOOLS: frozenset[str]`, `_parse_cloud_agent_runtime(payload: object) -> Runtime`.
- Extend `runtime_profile(profile: ConnectorProfile, dispatch_enabled: bool, *, messaging_enabled: bool = False) -> ConnectorProfile` and pass the runtime flag at both adapter call sites. Only the cloud-agent branch uses the new keyword. True adds messaging tools, `hub_inbox=True`, strict recipients and the existing Grok inbox send limit. False has no polling or message discovery. Both have `principal_memory=True`, `memory_push=False`, `prompt_load_scan=True`, `fleet_dispatch=False`, no capabilities tool.
- `connector_registry.py` exposes `instance_config_path(target: Path, pack_id: str) -> Path`, `load_instance_config(target: Path, pack_id: str) -> dict[str, Any]`, `packaged_manifests() -> tuple[dict[str, Any], ...]`, and `PackError(ValueError)` with the existing `reason` attribute. Its Dot-family API is `preview_setup(target: Path, pack_id: str, *, bind: str | None = None, allowed_hosts: list[str] | None = None, allowed_origins: list[str] | None = None, runtime_path: str | Path | None = None) -> dict[str, Any]`, with the identical signature for `apply_setup`. Also expose `preview_update(target: Path, pack_id: str) -> dict[str, Any]` and `apply_update` with the same signature, `preview_remove(target: Path, pack_id: str, *, unit_dir: Path | None = None) -> dict[str, Any]` and `apply_remove` with the same signature, `doctor(target: Path, pack_id: str, *, service_result: bool = False) -> list[dict[str, str]]`, `canary(target: Path, pack_id: str) -> dict[str, Any]`, `render_install_service(target: Path, pack_id: str) -> str`, `apply_install_service(target: Path, pack_id: str, *, out_dir: Path, force: bool = False) -> dict[str, Any]`, `list_packs(target: Path | None = None) -> list[dict[str, Any]]`, and `show_pack(pack_id: str, target: Path | None = None) -> dict[str, Any]`. Legacy wrappers accept old flags and retain their existing validation before delegation. Preserve Dot/Duet schema and instance paths. Cloud-agent uses `.brigade/connectors/cloud-agent/instance.json` and loopback port 8783 unless explicitly configured, with collision checks retained.
- `hub_connector.register(sub: argparse._SubParsersAction) -> None` and `dispatch(args: argparse.Namespace) -> int` own `hub connector {list,show,setup,doctor,canary,install-service,update,remove,serve}`. Use `--id` for profile selection and existing setup flags applicable to Dot-family profiles. Keep `python -m brigade.dot` watch entry point and its `--pack` selection.

- [ ] **Step 1: Write failing generic-profile tests** in `tests/test_cloud_agent_profile.py`. A local `configured_cloud_agent(tmp_path, *, messaging_enabled=False)` fixture builds protected fake credentials and existing fake hub fixtures, without using the real workspace. Assert:

```python
assert names_without_messaging == {"memory_search", "memory_card", "handoff_write"}
assert names_with_messaging == names_without_messaging | {
    "inbox_list", "inbox_ack", "inbox_reply", "message_send"
}
assert profile.principal_memory is True
assert profile.fleet_dispatch is False
assert runtime.hub.principal == "agent:cloud-agent"
```

Parameterize absent/false/true messaging, reject non-booleans and unknown dispatch/events/skill keys. Invoke each forbidden tool through the actual MCP adapter and require existing unknown-tool refusal, not an authorization-only wrapper around a registered tool.

- [ ] **Step 2: Write registry, isolation and messaging tests**. In `tests/test_connector_registry.py`, pin `test_closed_generic_registry`, `test_existing_dot_duet_config_paths_unchanged` and `test_canonical_unit_argv`: unknown IDs raise `PackError`, Dot/Duet paths and schemas match the legacy registry, and new units invoke `hub connector serve --id cloud-agent`. Preserve preview/apply rollback and collision tests from the old pack suite. Then add isolation tests using `test_cloud_agent_rejects_shared_identity`, `test_cloud_agent_rejects_wrong_audience`, `test_cloud_agent_no_node_token_fallback`, `test_cloud_agent_message_grants_and_retry`. Assert shared token/state/principal and incorrect audience fail existing configuration/auth checks, missing token does not invoke node CLI authority, disallowed recipient returns `message_recipient_not_permitted`, duplicate sends deduplicate within the existing 10-minute UTC bucket, hourly limit remains shared between send/reply, and failed acknowledgements leave messages pending.

- [ ] **Step 3: Run red tests** with `FOCUSED tests/test_cloud_agent_profile.py tests/test_connector_registry.py`. Expected: the new profile/registry imports fail until implementation.

- [ ] **Step 4: Implement the profile/parser and effective allowlist** using the Interfaces above. Accept exactly schema/auth/hosted paths/hub and optional notify/messaging keys. Validate the dedicated principal, require explicit Access configuration, preserve protected file validation, reject dispatch activation, and ensure separate state/token/audience checks cover every profile. Reuse existing memory search/card/handoff code without widening hub grants.

- [ ] **Step 5: Extract the surviving connector lifecycle registry and add its CLI**. Move applicable validation, preview/apply, rollback, collision and ownership logic from `grokbot_packs.py` without duplication. Legacy registry delegates Dot-family entries to the new registry while all old Grok entries still work. Render new service argv through the canonical connector command. Leave old generated Dot/Duet argv accepted. Do not start services or provision live credentials during development.

- [ ] **Step 6: Run green checks** with `FOCUSED tests/test_cloud_agent_profile.py tests/test_connector_registry.py tests/test_dot_lifecycle.py tests/test_dot_runtime.py tests/test_duet_profile.py tests/test_dot_access_auth.py tests/test_dot_memory_tools.py tests/test_dot_handoff_write_path.py tests/test_dot_inbox_tools.py tests/test_grokbot_packs.py`. Expected: PASS, unchanged Dot snapshot, both old profiles still present. Execute common inventory, fragment and ratchet checks. Lower extracted legacy caps where counts fall. Record the temporary aggregate-budget exception in the PR body and the path to its later removal.

- [ ] **Step 7: Commit** as `feat(connector): add isolated generic cloud-agent profile`, with the required trailer. Open only an implementation-authorized PR targeting `overhaul`. Merge after current-head required CI/reviews, before Task 2 starts.

### Task 2: PR 2, prove parity and retire Grok memory/inbox profiles

**Files:**
- Modify: `src/brigade/dot/{profile,runtime_config,__init__}.py`, `src/brigade/dot/connector_registry.py`, `src/brigade/grokbot_packs.py`, `src/brigade/cli/run_cloud.py`.
- Create: `src/brigade/cli/removed_commands.py`, `tests/test_removed_commands.py`, `tests/test_cloud_agent_parity.py`.
- Delete after porting surviving assertions: `tests/test_grokbot_memory_profile.py`, `tests/test_grokbot_s26_profiles.py`.
- Modify/Test: `tests/test_dot_duet_prompt_scan.py`, `tests/test_dot_memory_push_hub.py`, `tests/test_memory_hub_replica_principal.py`, `tests/test_dot_conductor_2473.py`, `tests/test_duet_profile.py`.
- Update inventory, ratchet and `changelog.d/stage-1-retire-grok-profiles.md`. Put immediate migration warnings in `docs/grokbot-memory-connector.md` and `docs/grokbot-inbox-connector.md` until Task 6 removes those pages.

**Interfaces:**
- Consumes: Task 1 profile/parser/registry. Preserve the same public tool argument schemas and response envelopes for migrated memory/message operations.
- Produces: `removed(command: str, *, replacement: str | None = None) -> int` in `cli/removed_commands.py`, returning 2 and writing `error: <command>: this was removed in <REMOVED_IN>. <migration guidance>` to stderr. `REMOVED_IN: str` records the shipping version once. All later removals call this handler. It imports no removed implementation and touches no target/state files.
- Retired IDs are recognized only by CLI compatibility parsing, not by `PROFILES`, runtime validation or MCP discovery. Old Dot/Duet pack IDs continue through compatibility delegates.

- [ ] **Step 1: Write paired parity tests while both old profiles still exist**, using the existing connector test fixtures and fake hub with equal grants for distinct principals. `test_cloud_agent_memory_parity` asserts exact equality of tool names, input schemas and normalized search/card results. `test_cloud_agent_private_cards_withheld` asserts private search results are absent and direct private-card reads are denied. `test_cloud_agent_handoff_untrusted` asserts the same hub submission payload and untrusted marking, with no local file inbox fallback. `test_cloud_agent_inbox_parity` asserts the enabled messaging four-tool inventory and equivalent list/ack/reply/send results with retry/dedup behavior. Normalize only profile identity and generated IDs/timestamps, never grants, trust flags or content.

- [ ] **Step 2: Run the deletion gate** with `FOCUSED tests/test_cloud_agent_parity.py tests/test_grokbot_memory_profile.py tests/test_grokbot_s26_profiles.py tests/test_cloud_agent_profile.py`. Expected: PASS for old and generic profiles in the same tree. Retain this Brigade receipt and its exact head in the PR evidence. A failure blocks profile deletion.

- [ ] **Step 3: Write tombstone tests**, parameterizing retired profile setup/serve/doctor/canary and later queue/pack commands. Assert exit 2, actual removal version and replacement text, no optional MCP import, no network calls, no token reads, and identical target tree before/after. Historical argv with additional flags must reach the message, not argparse's unhelpful invalid-choice error.

- [ ] **Step 4: Run red tombstone tests** with `FOCUSED tests/test_removed_commands.py`. Expected: failure because old commands still dispatch.

- [ ] **Step 5: Remove old profile definitions/parsers/exports and register tombstones** after the recorded Step 2 pass. Turn the paired parity fixtures into fixed generic contract assertions before deleting old tests so the final tree imports no retired constants. Preserve the pre-deletion receipt as evidence. Use the same tombstone dispatcher for pack IDs and serve selections, and keep a static command-path table with release expiry, without another plugin registry.

- [ ] **Step 6: Run green checks** with `FOCUSED tests/test_cloud_agent_parity.py tests/test_cloud_agent_profile.py tests/test_removed_commands.py tests/test_duet_profile.py tests/test_dot_duet_prompt_scan.py tests/test_dot_memory_push_hub.py tests/test_memory_hub_replica_principal.py tests/test_dot_conductor_2473.py`. Run common checks, lower applicable caps, and update inventory to distinguish tombstones from available functionality. Expiry is the next ordinary release after `REMOVED_IN`, with removal of the static table scheduled in release notes.

- [ ] **Step 7: Commit** as `refactor(connector): retire Grok-specific memory and inbox profiles`, with trailer. Target `overhaul`, obtain current-head gates and merge before pack removal.

### Task 3: PR 3, remove steward packs and caller-specific helpers

**Files:**
- Delete: Appendix A entries marked PR 3, including all of `grokbot_fleet/`, `grokbot_obsidian/`, `grokbot_t3/`, `grokbot_wazuh/`, dedicated plugin assets/installer and tests.
- Create: `src/brigade/dot/fleet_runner.py`, `src/brigade/dot/rendered_files.py`, `tests/test_dot_fleet_runner.py`.
- Modify: `src/brigade/dot/{fleet_tools,lifecycle}.py`, `src/brigade/grokbot_packs.py`, `src/brigade/cli/run_cloud.py`, `tests/conftest.py`, CI/allowlist/registry entries in Appendix C as each target disappears.
- Test: `tests/test_dot_fleet_tools.py`, `tests/test_duet_dispatch.py`, `tests/test_daemon_renderer_isolation.py`, `tests/test_fleet_action_runner.py`, `tests/test_fleet_action_launch_isolation.py`, `tests/test_windows_read_security.py`, `tests/test_verify_brigade_skill.py`.
- Create `changelog.d/stage-1-remove-stewards.md`, update inventory and ratchet.

**Interfaces:**
- Consumes: Task 2 tombstone handler and Task 1 lifecycle registry.
- Produces: neutral `dot/fleet_runner.py` containing the existing `FleetRunner`, `FleetCallError`, `FleetProtocolError`, `FleetTimeout`, `RevisionError`, `child_env() -> dict[str, str]`, and protocol constants with unchanged signatures/values from `grokbot_t3/fleet.py`. Move only helpers used by Dot/Duet. `dot/rendered_files.py` owns `_write_rendered(path: Path, rendered: str, *, force: bool) -> Path` with its existing safe ownership/atomic-write semantics. Its existing grokbot_ops support calls move with their dependency closure in Task 4, to eliminate the hidden import.

- [ ] **Step 1: Port shared runner and rendered-file assertions before deleting tests** to `tests/test_dot_fleet_runner.py` and `tests/test_daemon_renderer_isolation.py`. Assert existing request/result/error schemas, bounded output, timeout/undecodable response refusal, clean child environment and safe quoting. `test_dot_and_duet_work_without_steward_imports` must import/build both profiles with deleted package imports blocked and assert their inventories and dispatch behavior match existing tests.

- [ ] **Step 2: Run red surviving-profile tests** with `FOCUSED tests/test_dot_fleet_runner.py tests/test_dot_fleet_tools.py tests/test_duet_dispatch.py tests/test_daemon_renderer_isolation.py`. Expected: new neutral helper imports fail.

- [ ] **Step 3: Move the shared helpers and retarget imports**. Update `dot/fleet_tools.py`, lifecycle unit writer and relevant test monkeypatch targets. Do not carry steward intake, ledger, reconcile or watch behavior into the neutral runner.

- [ ] **Step 4: Delete Appendix A PR 3 files and their CLI/registry branches**, leaving Task 2 tombstones for retired pack IDs. Remove Wazuh helpers with their sole remediation caller. Remove now-unused t3 fixtures in `tests/conftest.py`. Port shared pack lifecycle assertions into `tests/test_connector_registry.py`. Remove deleted test entries from Windows weights/allowlists, test tiers and CI references in the same PR. Run a tracked import grep and fix every surviving importer listed in Appendix B/C before committing.

- [ ] **Step 5: Run green checks** with `FOCUSED tests/test_connector_registry.py tests/test_removed_commands.py tests/test_dot_fleet_runner.py tests/test_dot_fleet_tools.py tests/test_duet_dispatch.py tests/test_dot_lifecycle.py tests/test_daemon_renderer_isolation.py tests/test_fleet_action_runner.py tests/test_fleet_action_launch_isolation.py tests/test_windows_read_security.py tests/test_verify_brigade_skill.py`. Expected: PASS. Run common checks and applicable Windows/CI contract tests from Appendix C. Delete corresponding baseline caps and lower remaining ones.

- [ ] **Step 6: Commit** as `refactor(connector): remove Grok steward packs and helpers`, with trailer, then current-head review/CI and merge into `overhaul`.

### Task 4: PR 4, remove job queue, feeds, workers, reconcile and relay

**Files:**
- Delete: Appendix A PR 4 source, tests and verifier manifests, including `grokbot*.py`, `fleet_client_grokbot.py`, `fleet_hub_grokbot.py`, `cli/run_cloud_build_feed.py` and remaining Grok-named tests.
- Create: `src/brigade/dot/transport.py`, `src/brigade/dot/service_render.py`, `src/brigade/fleet_hub_legacy_storage.py`, `tests/test_dot_transport.py`, `tests/test_fleet_hub_legacy_storage.py`.
- Modify: Appendix B importer sites and Appendix C registration/CI entries required to remove imports now. Dashboard presentation cleanup follows in Task 5.
- Test: `tests/test_dot_compat.py`, `tests/test_fleet_dot.py`, `tests/test_fleet_cloud.py`, `tests/test_fleet_hub_routes.py`, `tests/test_fleet_hub_startup_signals.py`, `tests/test_fleet_nodes.py`, `tests/test_dispatch_memory.py`, `tests/test_ci_workflow.py`.
- Create `changelog.d/stage-1-remove-job-workers.md`, update inventory and ratchet.

**Interfaces:**
- Consumes: Task 1 neutral lifecycle, Task 2 tombstones, Task 3 neutral runner.
- Produces: `dot/transport.py` preserves exact signatures/behavior of `ConfigurationError`, `OptionalDependencyError`, `load_hub_token_file(path: Path) -> str`, its descriptor-safe helpers, `_load_mcp() -> tuple[Any, Any, Any, Any]`, `_valid_host(value: str) -> bool`, `_valid_origin(value: str) -> bool`, `_is_loopback(host: str) -> bool` and `SDK_LOOPBACK_ALLOWED_HOSTS` that surviving imports use. `dot/service_render.py` retains `_systemd_quote(value: str) -> str`, `_connect_host(host: str) -> str`, `ServiceRenderError(ValueError)` and the existing `_read_regular_text`, `_path_is_symlink` and safe-write helper dependency closure used by the neutral rendered-file writer. Copy their signatures unchanged from `grokbot_ops.py` and preserve refusals. No queue tokens, leases, adapters or journaling move with them.
- `fleet_hub_legacy_storage.ensure_schema(conn: sqlite3.Connection) -> None` preserves the current `fleet_hub_grokbot.ensure_schema` DDL needed by lockstep/parity, including actor/job/operation tables. No worker/mutation entry points. Existing database schema, migration IDs and historical rows remain intact. Keep reserved legacy harness/provider rejection in `fleet_hub.store_events` and `handle_cloud` so ordinary callers cannot impersonate retired hub authority.

- [ ] **Step 1: Write retention tests**. `test_removed_commands_preserve_populated_store` snapshots a temp queue directory and SQLite contents, invokes queue claim/feed/reconcile/status tombstones and asserts no changed bytes or new files. `test_legacy_schema_is_readable_without_workers` opens a populated upgrade-style fixture, applies only retained schema setup, reads job/lease/report fields through read-only SQLite and asserts schema shape/rows unchanged. `test_retired_route_never_mutates` requires `/grokbot` to return the existing not-found envelope without modifying rows. No DROP or data deletion is permitted.

- [ ] **Step 2: Write transport/import regressions**. Port protected reader tests to `tests/test_dot_transport.py`: missing, symlink, nonregular, wrong-owner/mode, oversized and changed-during-read files must retain fail-closed behavior. Pin optional dependency import behavior with MCP/JWT/posix facilities absent using `tests/test_dot_compat.py`. `test_old_dot_duet_service_argv_survives` must parse and route old generated service argv to the neutral lifecycle with unchanged target/profile selection.

- [ ] **Step 3: Run red checks** with `FOCUSED tests/test_dot_transport.py tests/test_fleet_hub_legacy_storage.py tests/test_removed_commands.py`. Expected: new helper imports and removed-route assertions fail before implementation.

- [ ] **Step 4: Extract only shared helpers and retained storage DDL**, update every importer in Appendix B and test monkeypatch in Appendix C, and delete Appendix A PR 4 files. Replace `fleet_hub._apply_schema`'s queue-module import with neutral schema setup. Stop starting/joining the expiry sweeper. Remove Python `/grokbot` routes/adapters, job enrollment CLI, queue discovery and cloud admission registration. Preserve generic cloud APIs, messaging and memory routes, claims, schema compatibility and reserved-identity checks. Keep historical data readable without exposing private task bodies in normal dashboards.

- [ ] **Step 5: Replace removed CLI dispatch with shared tombstones**. Static parsing accepts the complete former command suffix and flags without reading them or the target. Old `run cloud grokbot pack` and `serve --pack dot|duet` aliases still delegate to neutral lifecycle. All other retired queue/worker/pack commands return Task 2's message. Delete Grok-only dispatch-memory authority/ref attachment functions, retaining common packet APIs and audience behavior.

- [ ] **Step 6: Update verifier/CI wiring**. Remove queue/pack features and controller commands, manifests and audit allowlist functions for the sweeper. Rename optional extra `grokbot` to `connector` with identical existing dependency pins, retaining `grokbot` as a one-release install alias to avoid breaking existing Dot/Duet installs. Make optional MCP CI install `.[dev,connector]` and run `tests/test_cloud_agent_profile.py` plus transport checks with zero skips, preserving its no-skip enforcement and Python matrix. Update workflow-contract tests instead of dropping them. Remove deleted test entries from weights, allowlists, shadow tiers and scan exceptions, preserving unrelated entries. Never edit parity SQL/golden fixtures or Rust hub files.

- [ ] **Step 7: Run green checks** with `FOCUSED tests/test_removed_commands.py tests/test_dot_transport.py tests/test_fleet_hub_legacy_storage.py tests/test_cloud_agent_profile.py tests/test_cloud_agent_parity.py tests/test_connector_registry.py tests/test_dot_compat.py tests/test_fleet_dot.py tests/test_fleet_cloud.py tests/test_fleet_hub_routes.py tests/test_fleet_hub_startup_signals.py tests/test_fleet_nodes.py tests/test_dispatch_memory.py tests/test_ci_workflow.py tests/test_verify_brigade_audit.py tests/test_windows_affected_tests.py tests/test_posix_facade_behaviour.py tests/test_handoff_show_audience.py`. Run common checks and the remaining Appendix C tests. Expected: PASS, surviving source imports resolve without any Appendix A module. Lower caps and record measured net source/doc/command counts without counting retired commands as removed until their tombstones expire.

- [ ] **Step 8: Commit** as `refactor(cloud): remove Grok job queue and worker paths`, with trailer, then current-head gates and merge into `overhaul`.

### Task 5: PR 5, clean mixed fleet/dashboard/cloud references

**Files:**
- Modify: remaining Appendix B fleet/action/dashboard/cloud tracker/summary sites and their coverage files. Modify `tests/test_module_size_ratchet.py` and command inventory as applicable.
- Create `changelog.d/stage-1-trim-mixed-cloud-references.md`.

**Interfaces:**
- Consumes: neutral helpers and schema preservation from Task 4. No remaining product-module imports.
- Produces: existing public generic cloud/status/claims/owner APIs with Grok-only scheduling and action affordances removed. Retain legacy opaque harness labels and reserved-identity defenses where historical records or wire compatibility require them. Treat historical rows as terminal/read-only and exclude them from active capacity, without erasing them.

- [ ] **Step 1: Add surviving-provider tests** named `test_trim_keeps_other_cloud_providers`, `test_trim_keeps_ordinary_claims_and_owners`, `test_legacy_rows_are_read_only_and_redacted`, in `tests/test_cloud_tracker.py`, `tests/test_fleet_claim_display.py`, and `tests/test_fleet_dashboard.py` respectively. Assert Codex/Claude/Cursor provider auth/status/sweep routing and errors retain their existing envelopes, ordinary claims retain occupancy, unknown harness labels still render safely, and legacy rows never expose task bodies or become runnable actions. Assert old owner-policy `grokbot_jobs` selectors are tolerated as inert on-disk configuration for this release, with a migration warning, so unrelated owners keep working.

- [ ] **Step 2: Run red checks** with `FOCUSED tests/test_cloud_tracker.py tests/test_fleet_claim_display.py tests/test_fleet_dashboard.py tests/test_fleet_policy_owners.py`. Expected: new no-action/legacy assertions fail before presentation cleanup.

- [ ] **Step 3: Remove the specific branches listed in Appendix B**. Keep other provider dispatch, public read-only historical rendering, messaging, memory and claims behavior. Remove the Grok brand/icon/action controls from current activity views and retire the Grok quota-export source. Retain generic unknown-label fallback and compatibility reads required for stored policy. Do not turn historical harness literals into a new live provider.

- [ ] **Step 4: Run all surviving tests named by Appendix B for this PR** through `FOCUSED`, including `tests/test_cloud_tracker.py tests/test_cloud_tracker_brief.py tests/test_codex_cloud.py tests/test_claude_cloud_routine.py tests/test_claude_cloud_adoption.py tests/test_cursor_cloud.py tests/test_fleet_dashboard.py tests/test_fleet_command_deck.py tests/test_fleet_deck_badges.py tests/test_ui_theme_integration.py tests/test_fleet_owners.py tests/test_fleet_owners_view.py tests/test_fleet_owners_page.py tests/test_fleet_claim_display.py tests/test_fleet_policy_owners.py tests/test_fleet_quota.py tests/test_fleet_model_inventory.py tests/test_fleet_action_runner.py tests/test_fleet_action_launch_isolation.py tests/test_center_agent_activity.py tests/test_center_readiness_cmd.py tests/test_daily_driver_cmd.py tests/test_operator_cmd.py`. Expected: PASS. Run common checks, lower surviving oversized caps, and capture matching base/head dashboard screenshots with fixture data and required scrub checks.

- [ ] **Step 5: Commit** as `refactor(fleet): trim Grok-only cloud projections and actions`, with trailer, then current-head gates and merge into `overhaul`.

### Task 6: PR 6, publish generic setup and operator migration instructions

**Files:**
- Create: `docs/cloud-agent-connector.md` containing setup and migration, no separate doc hierarchy.
- Delete: Appendix A PR 6 dedicated product guides and obsolete phase/audit pages, after moving applicable instructions.
- Modify: Appendix C mixed documentation and registry feature indexes, `docs/command-inventory.md`, `changelog.d/stage-1-connector-migration.md`.
- Test: `tests/test_cli_help.py`, `tests/test_cli_inventory_contract.py`, `tests/test_verify_brigade_skill.py`, `tests/test_connector_registry.py`, `tests/test_cloud_agent_profile.py`.

**Interfaces:**
- Consumes: canonical `hub connector` lifecycle from Task 1, tombstone wording/version from Task 2, existing `python -m brigade.dot` watch path. Uses existing hub principal/grant and Access provisioning documentation without inventing a new provisioning API.
- Produces: one public guide for any MCP client, including Grok Bot, with three-tool default and seven-tool messaging opt-in. Documentation links use repository-relative paths. No new command/service is implemented here.

- [ ] **Step 1: Document a temp-directory setup walkthrough** with protected runtime/token references, dedicated hub principal/grants and Access audience. Render setup preview, apply only to the temp target, doctor, canary and MCP tool listing. Show both exact tool sets and sample search/card/untrusted-handoff results. Explain how to opt into messaging and recipient grants without enabling dispatch. Do not paste live credentials, Access identifiers or client profiles.

- [ ] **Step 2: Document listener retirement in operational order**. Inventory user-level installed Grok listener/watch/relay services and timers with `systemctl --user list-unit-files` and `systemctl --user list-timers --all`, inspect exact `FragmentPath`/`ExecStart` with `systemctl --user show <unit>`, and build an explicit list of owned units before changes. Include queue roles, all three stewards, operations relay, old memory/inbox endpoints and associated T3 watch timers. For system units use the corresponding administrator-managed scope. Disable and stop timers first, then their services using `systemctl --user disable --now <unit>`. Remove only inspected generated unit files from their reported fragment paths, run `systemctl --user daemon-reload` and `systemctl --user reset-failed`, then verify no selected unit remains running/enabled. Never use a wildcard delete or remove state/token directories. Warn that removing package code alone does not stop an already installed listener.

- [ ] **Step 3: Document client cutover and rollback**. Provision the separate generic identity and endpoint first, validate discovery/search/private-card/untrusted-handoff behavior, stop new sends to old inbox endpoints, inspect pending deliveries and acknowledge/reply only as the authorized old principal, then point Grok Bot's MCP configuration at the generic URL and corresponding Access audience. Enable messaging only if needed and verify recipient grants/dedup. Retire old listeners/timers after the new endpoint works, revoke old endpoint credentials after pending deliveries are settled, and retain user stores and protected old configuration for rollback. Rollback uses the prior package and inspected unit/config backups, without copying credentials between profiles. Do not claim principal inbox histories are automatically transferred.

- [ ] **Step 4: Document readable retained data and command expiry**. Explain read-only SQLite inspection/export of preserved job tables using a backup copy and standard tools, plus ordinary file access for local queue/report archives. No compatibility worker or live queue mutation is retained. Name the actual removal release in the guide from the same tombstone constant and state that tombstones and the install-extra alias disappear the following release. Record Dot/Duet folding as follow-up, and keep contract v2 hub cleanup out of Stage 1.

- [ ] **Step 5: Remove obsolete pages and repair inbound links** using Appendix C, regenerate inventory with the common command, and run each edited page through `CHECK brigade guard scan <page> --policy src/brigade/guard/policies/public-repo.json`. Run `CHECK vale docs/cloud-agent-connector.md`, the focused selectors listed under Files, fragment and ratchet checks. Expected: no broken links to deleted pages, no private details, no Vale errors, PASS for help and inventory. The guide's walkthrough is checked against temp fixtures via connector tests, not against the operator's live services. Record all four aggregate size counters lower than the Stage 1 base, accounting separately for tombstones that still count as command paths. If only docs changed, the legacy cap mapping stays unchanged and is checked with no artificial cap changes.

- [ ] **Step 6: Commit** as `docs(connector): document generic access and Grok listener migration`, with trailer. Target `overhaul`, preserve required docs CI/review gates, and merge only when current-head checks pass. `Text-only change; no UI/layout change`.

## Appendix A: Whole-file deletion manifest

Each row names a tracked file and its physical line count at the survey base. PR numbers assign deletion ownership, not permission to delete without porting shared assertions/helpers. The generic profile lands before any of these deletions.

| File | Lines | PR |
|---|---:|---:|
| `docs/adaptive-ci/test-audit-grokbot-ledger.md` | 1,493 | 6 |
| `docs/adaptive-ci/test-audit-grokbot.md` | 237 | 6 |
| `docs/grokbot-blinded-evals.md` | 87 | 6 |
| `docs/grokbot-dot.md` | 422 | 6 |
| `docs/grokbot-inbox-connector.md` | 95 | 6 |
| `docs/grokbot-mcp.md` | 373 | 6 |
| `docs/grokbot-memory-connector.md` | 210 | 6 |
| `docs/grokbot-operating-guide.md` | 761 | 6 |
| `docs/grokbot-t3-dispatch.md` | 447 | 6 |
| `docs/phase-grokbot-approved-feed.md` | 99 | 6 |
| `docs/phase-grokbot-approved-issue-selector.md` | 149 | 6 |
| `docs/phase-grokbot-listener-recovery.md` | 66 | 6 |
| `docs/phase-grokbot-obsidian-missing-file-plan.md` | 32 | 6 |
| `docs/phase-grokbot-one-repository-distribution.md` | 250 | 6 |
| `docs/phase-grokbot-report-reconcile.md` | 53 | 6 |
| `docs/phase-grokbot-report-snapshots.md` | 129 | 6 |
| `obsidian-plugin/grokbot-operator-adapter/NOTICE` | 26 | 3 |
| `obsidian-plugin/grokbot-operator-adapter/SHA256SUMS` | 2 | 3 |
| `obsidian-plugin/grokbot-operator-adapter/main.js` | 4,992 | 3 |
| `obsidian-plugin/grokbot-operator-adapter/manifest.json` | 9 | 3 |
| `obsidian-plugin/grokbot-operator-adapter/scripts/bundle-zod.js` | 260 | 3 |
| `obsidian-plugin/grokbot-operator-adapter/src/adapter.js` | 538 | 3 |
| `obsidian-plugin/grokbot-operator-adapter/vendor/LICENSE` | 21 | 3 |
| `obsidian-plugin/grokbot-operator-adapter/vendor/zod-3.25.76.cjs` | 4,443 | 3 |
| `obsidian-plugin/grokbot-operator-adapter/vendor/zod.lock.json` | 23 | 3 |
| `ops/install-grokbot-operator-adapter.sh` | 148 | 3 |
| `registry/skills/verify-brigade/features/grokbot-pack-lifecycle.md` | 130 | 4 |
| `registry/skills/verify-brigade/features/run-cloud-grokbot-queue.md` | 109 | 4 |
| `src/brigade/cli/run_cloud_build_feed.py` | 68 | 4 |
| `src/brigade/fleet_client_grokbot.py` | 322 | 4 |
| `src/brigade/fleet_hub_grokbot.py` | 1,489 | 4 |
| `src/brigade/grokbot_build_feed.py` | 460 | 4 |
| `src/brigade/grokbot_feed.py` | 673 | 4 |
| `src/brigade/grokbot_findings.py` | 932 | 4 |
| `src/brigade/grokbot_findings_relay.py` | 318 | 4 |
| `src/brigade/grokbot_fleet/__init__.py` | 65 | 3 |
| `src/brigade/grokbot_fleet/actions.py` | 1,217 | 3 |
| `src/brigade/grokbot_fleet/contracts.py` | 153 | 3 |
| `src/brigade/grokbot_fleet/exec.py` | 247 | 3 |
| `src/brigade/grokbot_fleet/ledger.py` | 485 | 3 |
| `src/brigade/grokbot_fleet/lifecycle.py` | 717 | 3 |
| `src/brigade/grokbot_fleet/normalize.py` | 144 | 3 |
| `src/brigade/grokbot_fleet/policy.py` | 203 | 3 |
| `src/brigade/grokbot_fleet/probes.py` | 656 | 3 |
| `src/brigade/grokbot_fleet/registry.py` | 117 | 3 |
| `src/brigade/grokbot_fleet/runtime_config.py` | 316 | 3 |
| `src/brigade/grokbot_fleet/tools.py` | 1,108 | 3 |
| `src/brigade/grokbot_job_clock.py` | 38 | 4 |
| `src/brigade/grokbot_job_validation.py` | 198 | 4 |
| `src/brigade/grokbot_jobs.py` | 1,989 | 4 |
| `src/brigade/grokbot_jobs_claim_target.py` | 65 | 4 |
| `src/brigade/grokbot_jobs_expiry.py` | 219 | 4 |
| `src/brigade/grokbot_jobs_hub.py` | 179 | 4 |
| `src/brigade/grokbot_mcp.py` | 1,512 | 4 |
| `src/brigade/grokbot_memory_credential.py` | 171 | 4 |
| `src/brigade/grokbot_obsidian/__init__.py` | 71 | 3 |
| `src/brigade/grokbot_obsidian/actions.py` | 306 | 3 |
| `src/brigade/grokbot_obsidian/adapters.py` | 1,000 | 3 |
| `src/brigade/grokbot_obsidian/capabilities.py` | 192 | 3 |
| `src/brigade/grokbot_obsidian/catalog.py` | 212 | 3 |
| `src/brigade/grokbot_obsidian/content_policy.py` | 79 | 3 |
| `src/brigade/grokbot_obsidian/contracts.py` | 824 | 3 |
| `src/brigade/grokbot_obsidian/excalidraw_client.py` | 278 | 3 |
| `src/brigade/grokbot_obsidian/lifecycle.py` | 819 | 3 |
| `src/brigade/grokbot_obsidian/native_client.py` | 252 | 3 |
| `src/brigade/grokbot_obsidian/operator_adapter.py` | 140 | 3 |
| `src/brigade/grokbot_obsidian/path_policy.py` | 260 | 3 |
| `src/brigade/grokbot_obsidian/runtime_config.py` | 478 | 3 |
| `src/brigade/grokbot_obsidian/store.py` | 757 | 3 |
| `src/brigade/grokbot_obsidian/tls.py` | 157 | 3 |
| `src/brigade/grokbot_obsidian/tools.py` | 836 | 3 |
| `src/brigade/grokbot_obsidian/utf8.py` | 37 | 3 |
| `src/brigade/grokbot_operations_relay.py` | 682 | 4 |
| `src/brigade/grokbot_ops.py` | 803 | 4 |
| `src/brigade/grokbot_pack_relay.py` | 448 | 4 |
| `src/brigade/grokbot_packs.py` | 1,508 | 4 |
| `src/brigade/grokbot_paths.py` | 39 | 4 |
| `src/brigade/grokbot_reconcile.py` | 585 | 4 |
| `src/brigade/grokbot_scout_feed.py` | 535 | 4 |
| `src/brigade/grokbot_t3/__init__.py` | 27 | 3 |
| `src/brigade/grokbot_t3/__main__.py` | 57 | 3 |
| `src/brigade/grokbot_t3/contracts.py` | 389 | 3 |
| `src/brigade/grokbot_t3/fleet.py` | 204 | 3 |
| `src/brigade/grokbot_t3/inbox.py` | 249 | 3 |
| `src/brigade/grokbot_t3/intake.py` | 329 | 3 |
| `src/brigade/grokbot_t3/ledger.py` | 396 | 3 |
| `src/brigade/grokbot_t3/lifecycle.py` | 746 | 3 |
| `src/brigade/grokbot_t3/runtime_config.py` | 308 | 3 |
| `src/brigade/grokbot_t3/tools.py` | 413 | 3 |
| `src/brigade/grokbot_t3/watch.py` | 357 | 3 |
| `src/brigade/grokbot_wazuh/__init__.py` | 1 | 3 |
| `src/brigade/grokbot_wazuh/contracts.py` | 218 | 3 |
| `src/brigade/grokbot_wazuh/normalize.py` | 236 | 3 |
| `src/brigade/grokbot_wazuh/policy.py` | 100 | 3 |
| `tests/obsidian_operator_plugin_harness.js` | 752 | 3 |
| `tests/test_fleet_hub_grokbot_expiry.py` | 441 | 4 |
| `tests/test_grokbot_build_feed.py` | 1,469 | 4 |
| `tests/test_grokbot_client_identity.py` | 480 | 4 |
| `tests/test_grokbot_feed.py` | 1,643 | 4 |
| `tests/test_grokbot_findings.py` | 1,207 | 4 |
| `tests/test_grokbot_findings_relay.py` | 722 | 4 |
| `tests/test_grokbot_fleet_actions.py` | 606 | 3 |
| `tests/test_grokbot_fleet_app.py` | 121 | 3 |
| `tests/test_grokbot_fleet_contracts.py` | 106 | 3 |
| `tests/test_grokbot_fleet_exec.py` | 115 | 3 |
| `tests/test_grokbot_fleet_ledger.py` | 168 | 3 |
| `tests/test_grokbot_fleet_lifecycle.py` | 442 | 3 |
| `tests/test_grokbot_fleet_normalize.py` | 57 | 3 |
| `tests/test_grokbot_fleet_probes.py` | 145 | 3 |
| `tests/test_grokbot_fleet_registry.py` | 98 | 3 |
| `tests/test_grokbot_fleet_remediation.py` | 1,265 | 3 |
| `tests/test_grokbot_fleet_runtime_config.py` | 193 | 3 |
| `tests/test_grokbot_fleet_tools.py` | 356 | 3 |
| `tests/test_grokbot_jobs.py` | 2,565 | 4 |
| `tests/test_grokbot_mcp.py` | 2,994 | 4 |
| `tests/test_grokbot_memory_credential.py` | 432 | 4 |
| `tests/test_grokbot_memory_profile.py` | 391 | 2 |
| `tests/test_grokbot_obsidian_actions.py` | 453 | 3 |
| `tests/test_grokbot_obsidian_adapters.py` | 674 | 3 |
| `tests/test_grokbot_obsidian_capabilities.py` | 50 | 3 |
| `tests/test_grokbot_obsidian_client.py` | 111 | 3 |
| `tests/test_grokbot_obsidian_contracts.py` | 133 | 3 |
| `tests/test_grokbot_obsidian_excalidraw_client.py` | 258 | 3 |
| `tests/test_grokbot_obsidian_lifecycle.py` | 644 | 3 |
| `tests/test_grokbot_obsidian_operator_install.py` | 109 | 3 |
| `tests/test_grokbot_obsidian_operator_plugin.py` | 133 | 3 |
| `tests/test_grokbot_obsidian_phase2.py` | 664 | 3 |
| `tests/test_grokbot_obsidian_policy.py` | 76 | 3 |
| `tests/test_grokbot_obsidian_runtime.py` | 444 | 3 |
| `tests/test_grokbot_operations_relay.py` | 537 | 4 |
| `tests/test_grokbot_ops.py` | 1,311 | 4 |
| `tests/test_grokbot_pack_relay.py` | 460 | 4 |
| `tests/test_grokbot_packs.py` | 1,499 | 4 |
| `tests/test_grokbot_reconcile.py` | 978 | 4 |
| `tests/test_grokbot_s26_profiles.py` | 872 | 2 |
| `tests/test_grokbot_scout_feed.py` | 1,250 | 4 |
| `tests/test_grokbot_t3_contracts.py` | 182 | 3 |
| `tests/test_grokbot_t3_fleet.py` | 222 | 3 |
| `tests/test_grokbot_t3_inbox.py` | 2,256 | 3 |
| `tests/test_grokbot_t3_intake.py` | 554 | 3 |
| `tests/test_grokbot_t3_ledger.py` | 309 | 3 |
| `tests/test_grokbot_t3_lifecycle.py` | 713 | 3 |
| `tests/test_grokbot_t3_platform.py` | 94 | 3 |
| `tests/test_grokbot_t3_runtime.py` | 272 | 3 |
| `tests/test_grokbot_t3_tools.py` | 696 | 3 |
| `tests/test_grokbot_t3_watch.py` | 709 | 3 |
| `tests/test_grokbot_wazuh_contracts.py` | 97 | 3 |
| `tests/test_grokbot_wazuh_normalize.py` | 147 | 3 |
| `tests/test_grokbot_wazuh_policy.py` | 116 | 3 |
| `verify/manifests/grokbot-feed.json` | 24 | 4 |
| `verify/manifests/grokbot-reconcile.json` | 24 | 4 |

**Total Python lines under `src/brigade/` in whole files removed: 29,389.** This excludes tests/docs/plugin assets, partial deletions in mixed modules and new or relocated helpers. Measure net reduction from the final diff.

## Appendix B: Mixed source edits and surviving coverage

Symbols come from matching reference lines and their enclosing definitions at the survey base. Imports/constants means the module-level reference, not permission to rewrite the whole module. Every coverage filename below is relative to `tests/`. Run all named surviving selectors for the files changed by a PR, in addition to its task checks. Names marked new are defined by the tasks.

| Mixed file | Exact functions/branches and disposition | Surviving tests |
|---|---|---|
| `src/brigade/center_cmd/readiness.py` | `_readiness_findings`. Drop queue readiness findings. keep ordinary readiness. | test_center_readiness_cmd.py, test_center_agent_activity.py |
| `src/brigade/cli/fleet.py` | `_dispatch_grokbot_enroll_actor`, `register`. Remove actor enrollment registration/dispatcher. use tombstone. Keep node enrollment and claims. | test_fleet_nodes.py, test_fleet_events_cli.py, test_fleet_claims.py |
| `src/brigade/cli/fleet_dot.py` | `dispatch`. Retarget protected holder-file reader to dot.transport with identical refusal. | test_fleet_dot.py, test_windows_read_security.py |
| `src/brigade/cli/fleet_work_prepared_handoff.py` | `_audience`. Remove retired Grok dispatch audience branch. preserve connector audience for Dot/Duet and local policy. | test_handoff_show_audience.py, test_fleet_campaign.py |
| `src/brigade/cli/run_cloud.py` | `_dispatch_cloud_ops`, `_dispatch_grokbot_convert_findings`, `_dispatch_grokbot_feed`, `_dispatch_grokbot_findings`, `_dispatch_grokbot_ops`, `_dispatch_grokbot_pack_relay`, `_dispatch_grokbot_pack`, `_dispatch_grokbot_reconcile`, `_dispatch_grokbot_relay_findings`, `_dispatch_grokbot_scout_feed`, `_dispatch_grokbot`, `_feed_hub_identity`, `_format_grokbot_job_line`, `_grokbot_bearer_valid`, `_grokbot_health_reason`, `_print_convert_findings_result`, `_print_feed_result`, `_print_findings_result`, `_print_grokbot_result`, `_print_reconcile_result`, `_print_relay_findings_result`, `_print_scout_feed_result`, `_reconcile_hub_identity`, `add_cloud_subcommands`, `dispatch`. Replace all _dispatch_grokbot* and associated printers/identity helpers with tombstones or Dot/Duet aliases. remove grokbot-cloud provider choices/health branch, add canonical connector registration. Keep other providers. | test_codex_cloud.py, test_claude_cloud_routine.py, test_cursor_cloud.py, test_duet_profile.py, test_removed_commands.py (new) |
| `src/brigade/cloud_tracker.py` | `_grokbot_artifact_refs`, `_grokbot_cloud_observation`, `_grokbot_hub_unavailable_row`, `_grokbot_rows`, `_provider_for_branch`, `health`, `observe_provider`, `register`, `status_payload`, imports/constants. Remove live Grok provider/job projections and health observation. retain safe read-only historical fallback and other providers. | test_cloud_tracker.py, test_cloud_tracker_brief.py, test_codex_cloud.py, test_claude_cloud_adoption.py |
| `src/brigade/daily_cmd/status_plan.py` | `status_payload`. Remove bounded Grok queue summary only. | test_daily_driver_cmd.py |
| `src/brigade/dispatch_memory.py` | `connector_text`, `grokbot_memory_authority`, `job_refs`, imports/constants. Remove grokbot_memory_authority, job_refs, with_job_refs and queue-only validate_refs if caller grep confirms none survive. Retain build/block_text/for_remote and existing packet audience behavior. Remove connector_text only after its sole steward caller disappears. | test_dispatch_memory.py, test_handoff_show_audience.py |
| `src/brigade/dot/__init__.py` | imports/constants. Add cloud exports, then remove retired exports and import-cycle comment. | test_dot_compat.py, test_duet_profile.py |
| `src/brigade/dot/access_auth.py` | imports/constants. Retarget OptionalDependencyError to neutral transport. | test_dot_access_auth.py, test_dot_compat.py |
| `src/brigade/dot/fleet_tools.py` | imports/constants. Retarget runner imports to dot.fleet_runner, preserve Dot protocol. | test_dot_fleet_tools.py, test_duet_dispatch.py |
| `src/brigade/dot/lifecycle.py` | `_base`, `_isolated_from_dot`, `_load_config`, `_server`, `_valid_host`, `allowed_transport`, `build_app`, `dependencies`, `render_unit`, `render_watch_units`, `run_listener`, imports/constants. Retarget registry/transport/render helpers, strengthen isolation enumeration, use canonical service argv, preserve old aliases. | test_dot_lifecycle.py, test_dot_compat.py, test_daemon_renderer_isolation.py |
| `src/brigade/dot/memory_tools.py` | `_cli_hub_card`, `memory_search`. Replace Grok-specific comments/authority naming with profile-neutral wording. preserve principal memory grants and node-fallback refusal. | test_dot_memory_tools.py, test_memory_hub_replica_principal.py, test_cloud_agent_parity.py (new) |
| `src/brigade/dot/profile.py` | imports/constants. Add CLOUD_AGENT and gated messaging allowlist, then remove GROKBOT_* definitions. Preserve DOT/DUET constants. | test_duet_profile.py, test_dot_contracts.py, test_dot_duet_prompt_scan.py, test_cloud_agent_profile.py (new) |
| `src/brigade/dot/runtime_config.py` | `_parse_grokbot_inbox_runtime`, `_parse_grokbot_memory_runtime`, `parse_runtime`, imports/constants. Add dedicated parser/schema/principal, remove two old parser branches after parity. | test_dot_runtime.py, test_duet_profile.py, test_cloud_agent_profile.py (new) |
| `src/brigade/dot/server.py` | imports/constants. Retarget error import. generic registration uses effective allowlist only. | test_dot_lifecycle.py, test_dot_inbox_argument_errors.py, test_cloud_agent_profile.py (new) |
| `src/brigade/fleet_action_adapters.py` | `recover`. Remove Grok recovery branch. keep other adapters. | test_fleet_action_launch_isolation.py, test_fleet_action_runner.py |
| `src/brigade/fleet_action_page.py` | `compose`. Remove Grok live compose/action affordances. | test_fleet_action_runner.py, test_fleet_dashboard.py |
| `src/brigade/fleet_action_runner.py` | `_tool`, `load_manifest`. Remove Grok manifest tool selection, retain other tools and authorization. | test_fleet_action_runner.py, test_fleet_action_launch_isolation.py |
| `src/brigade/fleet_action_targets.py` | `native_identity`, `sync`. Remove Grok native identity/sync branch. keep other targets. | test_fleet_action_runner.py, test_fleet_action_launch_isolation.py |
| `src/brigade/fleet_claim_display.py` | `_grokbot_rows`, `list_display_claims`, imports/constants. Remove queue-backed active claim derivation and imports. keep ordinary claim occupancy and safe historical labels. | test_fleet_claim_display.py, test_fleet_mcp.py, test_fleet_claims.py |
| `src/brigade/fleet_command_deck.py` | `_enrich_runs`, `_last_known_field`, `default_cloud_config`, `fetch_started_at`, imports/constants. Remove Grok provider defaults and current run enrichment. Keep historical-safe field lookup where needed to read persisted events. | test_fleet_command_deck.py, test_fleet_dashboard.py |
| `src/brigade/fleet_deck_brands.py` | imports/constants. Remove _GROKBOT_SVG and brand map entry, retain unknown-brand fallback. | test_fleet_deck_badges.py, test_ui_theme_integration.py |
| `src/brigade/fleet_hub.py` | `_apply_schema`, `cloud_snapshot`, `handle_cloud`, `store_events`, imports/constants. Retarget _apply_schema to legacy storage. remove queue deck projection. Keep reserved event/provider refusal and persisted schema/indexes. | test_fleet_cloud.py, test_fleet_hub_routes.py, test_fleet_hub_legacy_storage.py (new) |
| `src/brigade/fleet_hub_api_http.py` | `admit_post`, `post`, `serve_get`, imports/constants. Remove /grokbot POST admission/handler and queue projection options. preserve cloud/message routes and admin boundaries. | test_fleet_hub_routes.py, test_fleet_cloud.py, test_fleet_hub_message_keys.py |
| `src/brigade/fleet_hub_http.py` | `_serve_owners_page`, `handle_grokbot`, imports/constants. Remove handle_grokbot/import. remove active queue owner source, preserve editable-view authority. | test_fleet_dashboard.py, test_fleet_hub_routes.py, test_fleet_owners_page.py |
| `src/brigade/fleet_hub_startup.py` | `run`. Remove expiry sweeper start/stop/join only. preserve other shutdown handlers. | test_fleet_hub_startup_signals.py, test_fleet_nodes.py |
| `src/brigade/fleet_hub_status.py` | `_last_known_seat`, `_latest_sql`, `board_status`, `latest_status`, imports/constants. Remove live queue assumptions. Preserve global legacy event ordering/read-only lookups where needed for stored records, never active admission. | test_fleet_dashboard.py, test_fleet_command_deck.py |
| `src/brigade/fleet_model_inventory.py` | imports/constants. Remove Grok Bot-specific unsupported entry while keeping generic unsupported behavior and unrelated Grok CLI harness handling. | test_fleet_model_inventory.py |
| `src/brigade/fleet_owners.py` | `_grokbot_jobs`, `_matches`, `load_view`, imports/constants. Remove _grokbot_jobs active source. tolerate old stored selector as inert. Keep claims/actions/work ownership. | test_fleet_owners.py, test_fleet_owners_view.py, test_fleet_owners_page.py |
| `src/brigade/fleet_policy_owners.py` | `_parse_match`, imports/constants. Retain parsing compatibility for old grokbot_jobs key as inert, add warning, remove active matching. | test_fleet_policy_owners.py, test_fleet_owners.py |
| `src/brigade/fleet_quota.py` | imports/constants. Remove grokbot-export source, preserve other quota sources and Grok CLI separation. | test_fleet_quota.py, test_fleet_routing.py |
| `src/brigade/operator_cmd/health.py` | `status_payload`. Remove Grok queue summary only. | test_operator_cmd.py |
| `src/brigade/dot/adapter.py` | `DotTools` construction and `build_tools` runtime_profile calls: pass the messaging keyword and construct no inbox poller for memory-only cloud-agent. | test_dot_adapter.py, test_dot_inbox_tools.py, test_dot_hub_tools.py, test_duet_dispatch.py, test_cloud_agent_profile.py (new) |

## Appendix C: Mixed test, support and documentation edits

Shared tests below keep their surviving behavior. Remove only assertions/fixtures for intentionally deleted functionality. Move shared transport, profile and storage assertions to the new owning tests first. Every row is also a selector for checking surviving behavior in that file. Do not retain imports merely to make old monkeypatch targets work.

| Test file | Specific functions/branches to update | Preserved coverage |
|---|---|---|
| `tests/conftest.py` | `t3_ledger_storage`, `t3_owner_reads`, `t3_platform_admission`, `t3_watch_log` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/dot_support.py` | imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_center_agent_activity.py` | `test_center_activity_marks_merged_grokbot_draft_pr_succeeded`, `test_center_activity_projects_grokbot_queue_without_private_instructions`, imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_center_readiness_cmd.py` | `test_center_readiness_surfaces_failed_grokbot_job_without_private_instructions`, imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_ci_workflow.py` | `test_ci_workflow_test_shards_asserts_grokbot_mcp_tests_run_without_skips`, `test_ci_workflow_test_shards_installs_grokbot_extra_on_python312`, `test_shadow_junit_is_requested_only_on_pull_request_python312_shards` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_claude_cloud_adoption.py` | `offline` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_claude_cloud_routine.py` | `offline` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_cloud_tracker.py` | `_completed_grokbot_job`, `_grokbot_spec`, `test_cli_cloud_status_includes_grokbot_without_private_envelope`, `test_github_only_branch_is_orphaned_recovery_never_active_capacity`, `test_grokbot_branch_is_orphaned_and_cloud_sweep_keeps_safe_references`, `test_grokbot_merged_draft_pr_lands_without_queue_attention`, `test_grokbot_queue_projects_safe_rows_and_reconciles_claimed_and_draft_pr`, `test_grokbot_stale_entry_surfaces_in_work_brief_without_task_text`, `test_observe_provider_details_keeps_unconfigured_and_failed_inventory_distinct`, `test_provider_auth_failures_are_configured_but_not_reachable`, `test_provider_normalization_contract_accepts_all_cloud_providers`, imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_cloud_tracker_brief.py` | `isolated_providers` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_codex_cloud.py` | `test_claude_cloud_doctor_is_static_conductor_routed`, `test_cloud_doctor_and_canary_route_to_supported_provider_adapters`, imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_daemon_renderer_isolation.py` | `_render`, `test_service_argv_preserves_override_target_and_safe_workdir`, imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_daily_driver_cmd.py` | `test_daily_status_includes_bounded_grokbot_cloud_summary`, imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_dispatch_memory.py` | `bot`, `enqueue`, `make_tools`, `test_grokbot_payload_has_no_task_text_and_only_references_in_metadata`, `test_grokbot_refs_follow_the_bot_principal_grants`, `test_grokbot_refs_name_hub_cards_its_memory_profile_can_read`, `test_grokbot_t3_adapter_appends_a_connector_block`, `test_invalid_memory_refs_are_refused`, `test_local_queue_claim_returns_references_only`, `test_no_bot_credential_means_no_refs`, `test_private_card_never_becomes_a_grokbot_reference`, imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_dot_access_auth.py` | imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_dot_compat.py` | `test_core_imports_without_posix_or_optional_dependencies`, `test_missing_rsa_support_fails_closed_and_doctor_reports_dependency`, `test_windows_operator_diagnostics_name_platform`, imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_dot_conductor_2473.py` | `test_operating_instructions_pin_the_conductor_authorization`, `test_runtime_template_refuses_other_schemas`, imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_dot_duet_prompt_scan.py` | `test_both_profiles_scan_and_every_profile_does`, imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_dot_events.py` | imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_dot_inbox_argument_errors.py` | `_gate` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_dot_lifecycle.py` | `configured`, `test_body_limit_and_schema_table_precede_sdk`, `test_call_log_failure_and_tool_errors_are_safe`, `test_canary_checks_anonymous_401_metadata_and_registry`, `test_doctor_all_checks_and_failures`, `test_invoke_preserves_retry_guidance_and_crash_code`, `test_registered_tools_match_snapshot`, `test_render_unit`, `test_report_session_discovery_is_usable_and_closed`, `test_run_listener_exact_settings_and_state_cleanup`, `test_run_listener_refuses_to_start_without_a_jwt_backend`, `test_sdk_context_events_and_exactly_one_row_per_call` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_dot_memory_push_hub.py` | `test_pushing_profiles_describe_memory_hits_and_the_grok_inbox_does_not` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_duet_dispatch.py` | `test_shared_fleet_identity_is_not_isolated` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_duet_profile.py` | `duet_configured`, `test_cli_serve_duet_passes_its_pack_id`, `test_dot_registration_is_unchanged_by_profiles`, `test_dot_target_cannot_load_a_duet_runtime`, `test_duet_canary_checks_its_own_inventory`, `test_duet_doctor_checks_isolation_instead_of_t3_fleet`, `test_duet_setup_takes_a_duet_runtime_and_no_bearer`, `test_gate_lists_duet_tools_refuses_dispatch_and_logs_client_name`, `test_lifecycle_delegates_duet_with_its_pack_id`, `test_registered_duet_tools_are_the_profile_and_keep_dot_schemas`, `test_registry_lists_duet_pack`, imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_fleet_action_launch_isolation.py` | imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_fleet_action_runner.py` | imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_fleet_campaign.py` | `test_cli_offline_selection_and_no_effects` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_fleet_campaign_authority.py` | `test_inspect_exact_local_lineage_is_read_only` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_fleet_claim_display.py` | `_conn`, `_insert_job`, `test_cli_claims_json_prints_sqlite_projection`, `test_excludes_queued_and_terminal_jobs_even_with_include_all`, `test_expired_and_malformed_leases_require_include_all`, `test_label_falls_back_to_job_id_when_stored_label_is_not_displayable`, `test_missing_grokbot_table_keeps_ordinary_claim_projection_compatible`, `test_preserves_ordinary_claims_without_changing_core_occupancy`, `test_projects_active_jobs_and_keeps_repeated_labels_distinct`, imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_fleet_claims.py` | `test_acquire_preserves_validated_external_harness_labels`, `test_claims_table_renders_external_harness_labels`, `test_client_rejects_invalid_opaque_labels_without_posting`, `test_opaque_labels_are_acquire_only` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_fleet_cloud.py` | `_enroll_actor`, `_enroll_actors`, `_grokbot_enqueue`, `_refuse_cross_actor`, `contend`, `test_cloud_config_defaults_and_validates_hosted_pool`, `test_external_expired_is_terminal_and_releases_hub_capacity`, `test_grokbot_ack_cancel_emits_external_cancel_acknowledged`, `test_grokbot_actor_policy_isolates_node_queue_and_role`, `test_grokbot_cloud_projection_is_admin_only_on_node_get`, `test_grokbot_control_actor_lists_only_its_own_queue`, `test_grokbot_enqueue_rejects_local_repository_path`, `test_grokbot_event_rejects_item_revision_sequence_mismatch`, `test_grokbot_feed_actor_list_is_scoped_to_its_own_queue`, `test_grokbot_feed_actor_list_needs_no_reenrollment`, `test_grokbot_feed_actor_still_cannot_mutate_or_read_reports`, `test_grokbot_http_get_is_removed_and_admin_cannot_list_unscoped`, `test_grokbot_hub_cancel_expire_and_complete_release_capacity`, `test_grokbot_hub_is_authoritative_for_enqueue_claim_renew_and_replay`, `test_grokbot_hub_projection_is_deck_only_and_terminal_jobs_release_capacity`, `test_grokbot_hub_rejects_private_bodies_and_unenrolled_nodes`, `test_grokbot_lifecycle_retry_records_one_sanitized_event_per_revision`, `test_grokbot_noop_expire_is_not_stored_for_replay`, `test_grokbot_operation_replay_and_revision_fencing`, `test_grokbot_operation_replay_cannot_cross_queue_or_node`, `test_grokbot_refuses_unscoped_null_queue_rows`, `test_grokbot_report_artifact_rejects_local_path_and_hides_private_data`, `test_grokbot_report_can_complete_from_claimed`, `test_grokbot_same_queue_peer_cannot_use_stolen_holder_lease`, `test_grokbot_two_claimer_race_one_winner`, `test_legacy_dashboard_start_map_uses_global_grokbot_start_for_latest_claimant`, `test_node_cannot_ingest_reserved_grokbot_harness`, `test_ordinary_node_cannot_squat_grokbot_cloud_namespace`, `test_stale_sweep_commits_before_conflicting_mutation`, `test_v4_migration_preserves_existing_rows`, imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_fleet_command_deck.py` | `test_an_unrecognised_auth_state_is_unknown_not_authenticated`, `test_command_deck_projects_hub_grokbot_rows_and_ignores_terminal_capacity`, `test_grokbot_cross_node_revisions_use_one_global_latest_row`, `test_integration_health_separates_scheduling_pickup_age_and_auth` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_fleet_dashboard.py` | `_insert_grokbot_job`, `test_claim_only_node_and_claim_text_are_rendered_safely`, `test_claims_and_boards_render_external_labels_and_hide_finished_verify`, `test_grokbot_last_known_seat_is_global_across_nodes`, `test_grokbot_start_lookup_seeks_without_sorting`, `test_http_claim_projection_renders_active_grokbot_jobs_without_events`, `test_machine_page_does_not_resurrect_old_grokbot_revision`, imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_fleet_deck_badges.py` | `test_brand_lookups_known_and_unknown` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_fleet_events_cli.py` | imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_fleet_home_guard.py` | `grokbot_mcp_adapter`, `stored_lease`, `test_grokbot_never_acquires_a_stored_key_under_a_relative_home`, `test_grokbot_never_releases_a_stored_key_under_a_relative_home`, `test_grokbot_never_renews_a_stored_key_under_a_relative_home`, `test_grokbot_never_reports_an_event_for_a_stored_key_under_a_relative_home`, `test_grokbot_takes_no_claim_under_a_relative_home` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_fleet_hub_routes.py` | `test_hub_route_inventory_is_unchanged` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_fleet_hub_startup_signals.py` | `test_a_second_stop_during_shutdown_is_ignored_and_the_handlers_come_back`, imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_fleet_mcp.py` | `test_station_reads_use_read_transactions_and_display_claims` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_fleet_model_inventory.py` | `test_unsupported_harnesses_report_unsupported_immediately`, imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_fleet_nodes.py` | `test_grokbot_actor_enrollment_uses_admin_token_and_returns_safe_metadata`, `test_grokbot_enroll_actor_cli_accepts_service_actor_kinds`, `test_grokbot_enroll_actor_cli_posts_one_fixed_role_actor`, `test_grokbot_operator_actor_omits_worker_role`, `test_serve_joins_expiry_sweeper_on_shutdown`, imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_fleet_owners.py` | `_match`, `test_grokbot_jobs_flag_matches_only_job_items` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_fleet_owners_page.py` | `_match`, `_seed_grokbot_job`, `test_owners_json_hides_grokbot_jobs_from_node_tokens_but_not_admin`, `test_owners_page_shows_grokbot_jobs_only_to_editable_viewers` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_fleet_owners_view.py` | `_job`, `broken`, `test_a_failing_source_is_marked_unknown_and_the_others_still_load`, `test_a_source_that_reaches_the_limit_is_reported_as_truncated`, `test_finished_and_expired_rows_are_not_holdings`, `test_grokbot_jobs_are_skipped_entirely_without_the_grokbot_flag`, `test_live_rows_from_all_four_sources_reach_their_owners`, `test_loading_the_view_writes_nothing`, `test_without_charters_every_live_item_is_unassigned` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_fleet_policy_owners.py` | `_owners`, `test_full_record_parses_and_fills_defaults`, `test_section_must_be_an_object_and_bounded`, imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_fleet_quota.py` | `_routing`, `test_name_heuristics_do_not_alias_grok_and_grokbot_pools`, `test_quota_exhaustion_is_distinct_from_missing` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_fleet_routing.py` | `_document`, `_fresh_quota`, `_task_policy`, `test_task_role_repository_precedence` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_fleet_who_paging.py` | `test_claim_pages_preserve_node_metadata_and_redact_agent_metadata` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_handoff_show_audience.py` | `test_other_dispatch_paths_stay_connector_on_an_opted_in_host`, imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_memory_hub_replica_principal.py` | imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_operator_cmd.py` | `test_operator_status_includes_bounded_grokbot_cloud_summary`, imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_posix_facade_behaviour.py` | `test_grokbot_jobs_directory_flags_maps_to_typed_error`, `test_grokbot_reconcile_directory_flags_maps_to_typed_error`, imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_pr_tier_shadow.py` | `log`, `test_full_log_collects_auxiliary_failure_without_replacing_shard_elapsed` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_ui_theme_integration.py` | `test_badge_icons_have_trusted_accessible_labels_without_inline_styles` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_verify_brigade_audit.py` | imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_verify_brigade_skill.py` | `test_skill_doc_includes_checkout_verification_map` | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_windows_affected_tests.py` | `test_module_imports_and_parent_packages`, `test_named_root_import_does_not_select_unrelated_siblings`, imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/test_windows_read_security.py` | `_read_cases`, `test_native_windows_capabilities_are_disabled`, `test_private_readers_fail_closed_before_filesystem_access`, imports/constants | Keep all non-retired cases. retarget shared imports to neutral helpers and add the task-specific assertions before removing product cases. |
| `tests/windows_pytest_allowlist.json` | Remove deleted-test entries only. | tests/test_windows_affected_tests.py and CI Windows allowlist ratchet. |

Support and metadata files are mixed. Keep snapshot evidence and unrelated entries. Coverage listed here is run with `FOCUSED` unless it is a documentation guard or generator command.

| File | Specific edit | Surviving verification |
|---|---|---|
| `.github/workflows/ci.yml` | Optional-extra install and no-skip MCP assertion steps in test shards and secondary install job. Replace removed test path with generic profile tests. Keep all required jobs and base-ref selection. | tests/test_ci_workflow.py, tests/test_cloud_agent_profile.py (new), CI matrix |
| `pyproject.toml` | Add connector extra with existing MCP/JWT constraints, retain grokbot install alias one release. No runtime dependency added. | tests/test_dot_compat.py, tests/test_ci_workflow.py, CI source install |
| `scripts/windows_test_weights.json` | Remove only Appendix A test keys on their deletion PR. | tests/test_windows_affected_tests.py, Windows CI weights validation |
| `scripts/pr_test_tiers.json` | Remove test_grokbot_t3_tools entry with PR 3. | tests/test_pr_tier_shadow.py, tests/test_ci_workflow.py |
| `scripts/pr_tier_shadow.py` | parse_full_log: replace auxiliary Grok MCP no-skip label with generic connector label. | tests/test_pr_tier_shadow.py |
| `scripts/memory_scan_bundle.py` | Module comment: call remote audience generic cloud agent. Preserve scanner and scope rules. | tests/test_dot_duet_prompt_scan.py, tests/test_dispatch_memory.py |
| `tests/test_module_size_ratchet.py` | LEGACY_OVERSIZED: drop deleted module keys, lower edited module caps to measured counts, remove caps below ceiling. No new entries or raises. | tests/test_module_size_ratchet.py, tests/test_size_ratchet_baseline.py, baseline comparison against origin/overhaul |
| `registry/skills/verify-brigade/control-brigade.py` | Remove cmd_pack_setup/doctor/canary/remove, cmd_feed, cmd_status and their build_parser entries, feed schema/constants and queue evidence branch in cmd_evidence. Keep verification/CI/controller commands. | tests/test_verify_brigade_skill.py, tests/test_verify_brigade_audit.py |
| `registry/skills/verify-brigade/hub_audit.py` | Remove start_expiry_sweeper patch tuple and Grok expiry wording. Keep stats/dispatch sweepers and read-only audit. | tests/test_verify_brigade_audit.py |
| `registry/skills/verify-brigade/SKILL.md` | Remove queue/pack verification instructions and replace profile checklist with generic connector checks. | tests/test_verify_brigade_skill.py, guard scan |
| `registry/skills/verify-brigade/features/README.md` | Remove two deleted feature rows. Link generic connector checklist. | tests/test_verify_brigade_skill.py, guard scan |
| `registry/skills/verify-brigade/features/MAINTENANCE.md` | Remove deleted pack/queue ownership rows and update selector guidance. | tests/test_verify_brigade_skill.py, guard scan |
| `README.md` | Remove bounded job-pack row, replace hosted links and Grok execution claim with generic MCP memory and optional messages. | guard scan, link search, tests/test_cli_inventory_contract.py |
| `CONTRIBUTING.md` | Replace optional-extra install example and no-skip test path with connector equivalents. | guard scan, tests/test_ci_workflow.py |
| `THIRD_PARTY_NOTICES.md` | Remove only the deleted Obsidian adapter's bundled Zod entry after plugin deletion. Keep other notices. | guard scan and license path search |

Mixed documentation edits below name exact matching line locations at the survey base. Replace active Grok-only examples/commands with the canonical connector path or remove retired behavior descriptions. Preserve general fleet, memory and other harness guidance. Each row runs a per-file public guard scan, a link search for deleted basenames, and help/inventory checks if commands are mentioned. Historical protocol pages keep schema/wire rationale and gain only a short retirement note or repaired link. Do not rewrite historical evidence.

| Mixed document | Matching lines to edit | Surviving behavior to cross-check |
|---|---|---|
| `docs/adaptive-ci/shadow-selection.md` | 171 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/assets/brands/README.md` | 25 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/command-inventory.md` | 224, 631, 632, 633, 634, 635, 636, 637, 638, 639, 640, 641, 642, 643, 644, 645, 646, 647, 648, 649, 650, 651, 652, 653, 654, 655, 656, 657, 658, 659, 660, 661, 662, 663, 664 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/comparison.md` | 88 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/duet-connector.md` | 192, 201, 202, 203, 204, 205, 209, 291, 309, 333 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/execution-model.md` | 51, 59, 64, 66 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/fleet-agent-actions.md` | 92 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/fleet-campaign-preview.md` | 94, 111 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/fleet-claim-generation-protocol.md` | 107 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/fleet-dot-reporting.md` | 152 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/fleet-mcp-events.md` | 145 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/fleet-model-inventory.md` | 210 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/memory-operations.md` | 101, 113, 158, 170, 189, 1976 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/outcome-scoring.md` | 75, 76 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/phase-adaptive-ci.md` | 323, 475 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/phase-agent-mesh-plan.md` | 584, 673 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/phase-agent-mesh.md` | 178, 207 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/phase-agent-plugin.md` | 7, 24, 126 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/phase-fleet-cloud-workers-plan.md` | 9, 104 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/phase-fleet-cloud-workers.md` | 39, 255 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/phase-fleet-interactive-session-presence.md` | 211, 264 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/phase-fleet-model-roster-authority.md` | 511 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/phase-fleet-ownership-plan.md` | 7, 24, 26, 27, 85, 86, 91, 92, 111, 112, 113, 114, 115, 117, 122, 127, 128, 159, 173, 236, 296, 388, 440, 441, 442, 445, 558, 659, 724, 729, 737, 744, 749, 756, 765, 766, 771, 799, 807, 813, 865, 880, 933, 1010, 1011, 1013, 1019, 1105, 1436, 2111, 2143 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/phase-fleet-ownership.md` | 9, 31, 32, 36, 68, 69, 71, 74, 75, 103, 108, 141, 148 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/phase-fleet-roster-deck-page.md` | 19, 219, 341 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/phase-hub-contract.md` | 177 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/phase-v2-hub-architecture.md` | 257, 268, 280, 334, 480, 1600, 2284, 2310, 2690, 2691, 2699, 2760, 2860, 2862, 2866, 2869, 2870, 2876, 2887, 2889, 3589, 3742, 3783, 3786, 3818, 3819 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/phase-worklore-core-plan.md` | 40 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/pr-tier-shadow-trial.md` | 121, 127, 128, 131, 229 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/runbooks/fleet-hub-proxmox.md` | 917 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/technical-guide.md` | 167, 169, 171, 178, 180, 183, 185, 190, 193 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |
| `docs/windows-pytest.md` | 210, 218 | Existing non-Grok commands and links, checked against help/inventory and the owning Appendix B selectors. |

Historical specs/plans under `docs/superpowers/`, release history/fragments, adaptive-CI baseline reports, captured graph assets, `.content-guard-baseline.json`, and research fixtures are retained as evidence. Repair only inbound links to deleted guides if necessary, directing them to `docs/cloud-agent-connector.md` with a retirement note. Do not edit the approved trim spec. Keep `registry/skills/verify-brigade/storage-parity-golden.json` and its SQL fixtures unchanged: schema compatibility is retained. Keep unrelated Grok CLI harness manifest, hooks, evidence sources and generic model-inventory handling, which are not Grok Bot execution paths.

The test inventory also includes indirect consumers that have no literal Grok reference: `tests/test_cli_help.py`, `tests/test_cli_inventory_contract.py`, `tests/test_dot_adapter.py`, `tests/test_dot_contracts.py`, `tests/test_dot_runtime.py`, `tests/test_dot_fleet_tools.py`, `tests/test_dot_inbox_tools.py`, `tests/test_dot_hub_tools.py`, `tests/test_dot_memory_tools.py`, `tests/test_dot_handoff_write_path.py`, `tests/test_fleet_dot.py` and the size-ratchet tests. Retain and run them for the corresponding edits. `tests/fixtures/mypy_override_baseline.txt` has no Grok-specific entry at the survey base, so do not change it speculatively.

## Appendix D: Protected hub files

The exclusion covers the entire `engines/hub/` tree, including the following named paths. Do not remove, regenerate or adjust snapshots to make Stage 1 pass. Contract v2 owns their eventual migration and rollback.

- `engines/hub/src/claims/mod.rs`, `arbiter.rs`, `tests.rs` and `engines/hub/src/claims_route.rs`.
- `engines/hub/src/heartbeat/{mod,events,sessions,status}.rs`.
- `engines/hub/src/schema/{mod,audit}.rs`.
- `engines/hub/tests/{claims,claims_native,heartbeat_native}.py`.
- `engines/hub/schema/{required.json,variant-inventory.json,VARIANTS.md,normalize-cases.json,inventory.py,lockstep.py,capture_upgrades.py}`.
- Every file in `engines/hub/schema/upgrade-fixtures/`, enumerated below.

- `engines/hub/schema/upgrade-fixtures/chain-v02-v03-v14-v15-v21.sql`
- `engines/hub/schema/upgrade-fixtures/production-v50.sql`
- `engines/hub/schema/upgrade-fixtures/schema-01.sql`
- `engines/hub/schema/upgrade-fixtures/schema-02.sql`
- `engines/hub/schema/upgrade-fixtures/schema-03.sql`
- `engines/hub/schema/upgrade-fixtures/schema-04.sql`
- `engines/hub/schema/upgrade-fixtures/schema-12.sql`
- `engines/hub/schema/upgrade-fixtures/schema-13.sql`
- `engines/hub/schema/upgrade-fixtures/schema-14.sql`
- `engines/hub/schema/upgrade-fixtures/schema-15.sql`
- `engines/hub/schema/upgrade-fixtures/schema-18.sql`
- `engines/hub/schema/upgrade-fixtures/schema-19.sql`
- `engines/hub/schema/upgrade-fixtures/schema-21.sql`
- `engines/hub/schema/upgrade-fixtures/schema-22.sql`
- `engines/hub/schema/upgrade-fixtures/schema-23.sql`
- `engines/hub/schema/upgrade-fixtures/schema-24.sql`
- `engines/hub/schema/upgrade-fixtures/schema-25.sql`
- `engines/hub/schema/upgrade-fixtures/schema-26.sql`
- `engines/hub/schema/upgrade-fixtures/schema-30.sql`
- `engines/hub/schema/upgrade-fixtures/schema-31.sql`
- `engines/hub/schema/upgrade-fixtures/schema-32.sql`
- `engines/hub/schema/upgrade-fixtures/schema-34.sql`
- `engines/hub/schema/upgrade-fixtures/schema-36.sql`
- `engines/hub/schema/upgrade-fixtures/schema-37.sql`
- `engines/hub/schema/upgrade-fixtures/schema-38.sql`
- `engines/hub/schema/upgrade-fixtures/schema-39.sql`
- `engines/hub/schema/upgrade-fixtures/schema-40.sql`
- `engines/hub/schema/upgrade-fixtures/schema-42.sql`
- `engines/hub/schema/upgrade-fixtures/schema-43.sql`
- `engines/hub/schema/upgrade-fixtures/schema-44.sql`
- `engines/hub/schema/upgrade-fixtures/schema-45.sql`
- `engines/hub/schema/upgrade-fixtures/schema-46.sql`
- `engines/hub/schema/upgrade-fixtures/schema-47.sql`
- `engines/hub/schema/upgrade-fixtures/schema-48.sql`
- `engines/hub/schema/upgrade-fixtures/v0.27.0.sql`
- `engines/hub/schema/upgrade-fixtures/v0.28.0.sql`
- `engines/hub/schema/upgrade-fixtures/v0.29.0.sql`
- `engines/hub/schema/upgrade-fixtures/v0.30.0.sql`

## Self-review

1. **Spec coverage:** Task 1 supplies the generic MCP profile and optional core messaging. Task 2 enforces parity before profile deletion. Tasks 3 and 4 remove stewards, helpers, queues, feeds, workers and relays with their tests and registration. Task 5 removes mixed live affordances while preserving other harnesses. Task 6 covers installed services/timers, client cutover, retained data and command expiry. Hub contract v2 is explicitly excluded. No Stage 2 orchestration work is included.
2. **Step scan:** Tasks contain Files, Interfaces, checkbox actions, red/green checks and conventional commits. File tables identify whole deletions and specific mixed symbols. New tests have named assertions, and helper moves preserve their source signatures instead of inventing substitute APIs. Documentation verification uses guard/link/help checks to validate prose.
3. **Type consistency:** CLOUD_AGENT and the two allowlists have one definition in Task 1. The messaging keyword has a default to preserve existing callers. Later tasks consume the one tombstone handler, neutral registry, transport and fleet runner. Retired exports do not survive as accidental import dependencies.
4. **Review Focus:** Hidden/direct tool calls, cross-profile credentials/audiences, legacy service argv, populated stores and message/handoff retries each have a named owning regression test. Paired parity runs before deletion and fixed generic contract tests remain afterward.
5. **Proportion:** The task narrative records interfaces, assertions and decisions and omits implementation bodies. Most additional length comes from the required per-file manifest and surviving coverage. No product implementation is included.
6. **Evidence limits:** The graph was unavailable, so static grep/AST and import regression tests replace it. The 29,389-line figure is gross deletion measured at the stated base, with net savings measured later and no private usage counts included. Final net budgets and shipping removal version are measured/resolved during implementation. No runtime tests were run to validate unimplemented changes.
7. **Publication:** Only repository-relative source paths and public example runtime paths appear. Private research locations, identities and counts are omitted. No em dashes or external inspiration names are present. The committed plan is the only tracked deliverable.

Nothing durable: this plan records the decisions and repository-grounded findings for later implementation.
