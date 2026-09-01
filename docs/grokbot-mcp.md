# Grok Bot MCP listener

Install the listener with Brigade. The `grokbot-dispatch-mcp` sidecar is retired; do not clone or install it. See [Retirement and rollback window](phase-grokbot-one-repository-distribution.md).

For the whole integration (pack reference, queue actor authorization, lifecycle semantics, finding delivery, and Bot configuration), see [Grok Bot operating guide](grokbot-operating-guide.md).

```bash
pipx install "brigade-cli[grokbot]"
```

The package contains the adapter source, but it does not turn the Brigade CLI into a listener. Each role runs a separate listener process with a fixed role. The listener delegates queue operations to the queue below its selected Brigade target. It has no independent queue authority. Keep every target local to the operator that owns that queue.

## Role setup and checks

Run these commands from the local Brigade target. Each example uses a distinct loopback port and a separate protected token file. Create each token file through your secret-management process with permissions limited to the service account. The `setup` command stores only the file reference, never the bearer value.

The `install-service` commands below render a systemd unit to standard output. They do not write a unit because they omit `--out`. Review the rendered unit before installing it through the host's normal service-management process. `canary` makes bounded authenticated requests to the running listener and checks anonymous rejection plus the exact role tool inventory. The Obsidian Operator canary also calls non-mutating `obsidian_capabilities` and requires a valid Phase 1 projection (`phase` remains `phase1`). A live matching private adapter can register private CAS tools against Local REST v2 `addMcpTool` because the checked-in plugin bundles pinned Zod 3.25.76 and passes a real `{path, expected_sha256, replacement_utf8}` shape; capabilities still stay Phase 1 until that live fingerprint is observed. It does not mutate the queue and does not cut over live services.

### Operator

```bash
brigade run cloud grokbot setup --target . --instance operator --bind 127.0.0.1:8766 --bearer-file /run/brigade/grokbot-operator.token
brigade run cloud grokbot doctor --target . --instance operator
brigade run cloud grokbot install-service --target . --instance operator
brigade run cloud grokbot serve --target . --instance operator --bind 127.0.0.1:8766 --bearer-file /run/brigade/grokbot-operator.token
brigade run cloud grokbot canary --target . --instance operator
```

The operator listener exposes queue listing, status, cancellation, expiry, and operator-only report retrieval. It cannot claim or complete worker jobs.

### Repository scout

```bash
brigade run cloud grokbot setup --target . --instance repository-scout --bind 127.0.0.1:8767 --bearer-file /run/brigade/grokbot-repository-scout.token
brigade run cloud grokbot doctor --target . --instance repository-scout
brigade run cloud grokbot install-service --target . --instance repository-scout
brigade run cloud grokbot serve --target . --instance repository-scout --bind 127.0.0.1:8767 --bearer-file /run/brigade/grokbot-repository-scout.token
brigade run cloud grokbot canary --target . --instance repository-scout
```

The repository-scout listener lists and reads only its role's jobs, then claims, renews, starts, completes, fails, or acknowledges cancellation for those jobs.

### Implementation worker

```bash
brigade run cloud grokbot setup --target . --instance implementation-worker --bind 127.0.0.1:8768 --bearer-file /run/brigade/grokbot-implementation-worker.token
brigade run cloud grokbot doctor --target . --instance implementation-worker
brigade run cloud grokbot install-service --target . --instance implementation-worker
brigade run cloud grokbot serve --target . --instance implementation-worker --bind 127.0.0.1:8768 --bearer-file /run/brigade/grokbot-implementation-worker.token
brigade run cloud grokbot canary --target . --instance implementation-worker
```

The implementation-worker listener has the same worker operations, constrained to implementation-worker jobs.

A successful worker claim returns the bounded validated envelope (label, role, repository, base ref, owned paths, instructions, verification commands, artifact kind, and timeout) as the worker's execution context. List, status, operator, and CLI surfaces stay redacted: they never include the envelope's `instructions` or `verification_commands`.

The routine sequence for a worker is: list, claim, validate role and repository, start, renew before expiry, then complete or fail. Repository Scout completion should send `report_text` so the operator can retrieve the snapshot.

`doctor` reports sanitized dependency, configuration, permissions, queue, and endpoint checks. It returns nonzero after setup until the listener starts because the endpoint check cannot connect. Run it again after the listener starts. A canary needs the listener already running.

## Connector packs

The three queue roles, the Cerebro Memory connector, the Fleet Steward connector, the Backup Steward connector, the Obsidian Operator connector, the Wazuh Triage connector, the n8n-operator connector, and the operations-relay connector are packaged as first-party connector packs. The registry is closed: Brigade does not load user-supplied pack manifests. Pack commands default to preview. `--apply` may write only private local config and an explicitly selected unit file. Pack commands do not start, stop, or reload services, and they never store bearer values. Cerebro setup also stores absolute CLI executable and workdir references. Pack manifests, config previews and results, doctor, canary, errors, receipts, and checked-in docs do not reveal those machine-specific paths. The generated local unit may include the already-validated workdir only as a systemd `ReadWritePaths` entry so `ProtectSystem=strict` can still persist proposals. The CLI executable path, bearer value, and secret material stay out of the unit.

```bash
brigade run cloud grokbot pack list
brigade run cloud grokbot pack show --id operator
brigade run cloud grokbot pack setup --id operator --bearer-file /run/brigade/grokbot-operator.token
brigade run cloud grokbot pack setup --id operator --bearer-file /run/brigade/grokbot-operator.token --apply
brigade run cloud grokbot pack setup --id cerebro-memory --bearer-file /run/brigade/grokbot-cerebro.token --cli-executable /usr/local/bin/cerebro-agents --workdir /var/lib/cerebro-agents
brigade run cloud grokbot pack setup --id cerebro-memory --bearer-file /run/brigade/grokbot-cerebro.token --cli-executable /usr/local/bin/cerebro-agents --workdir /var/lib/cerebro-agents --apply
brigade run cloud grokbot pack setup --id fleet-steward --bearer-file /run/brigade/grokbot-fleet.token --runtime-path /var/lib/grokbot-fleet/runtime.json --ledger-path /var/lib/grokbot-fleet/ledger.json --action-state-path /var/lib/grokbot-fleet/actions --approval-dir /var/lib/grokbot-fleet/approvals
brigade run cloud grokbot pack setup --id fleet-steward --bearer-file /run/brigade/grokbot-fleet.token --runtime-path /var/lib/grokbot-fleet/runtime.json --ledger-path /var/lib/grokbot-fleet/ledger.json --action-state-path /var/lib/grokbot-fleet/actions --approval-dir /var/lib/grokbot-fleet/approvals --apply
brigade run cloud grokbot pack setup --id backup-steward --bearer-file /run/brigade/grokbot-backup.token --runtime-path /var/lib/grokbot-backup/runtime.json --ledger-path /var/lib/grokbot-backup/ledger.jsonl --action-state-path /var/lib/grokbot-backup/actions --approval-dir /var/lib/grokbot-backup/approvals
brigade run cloud grokbot pack setup --id backup-steward --bearer-file /run/brigade/grokbot-backup.token --runtime-path /var/lib/grokbot-backup/runtime.json --ledger-path /var/lib/grokbot-backup/ledger.jsonl --action-state-path /var/lib/grokbot-backup/actions --approval-dir /var/lib/grokbot-backup/approvals --apply
brigade run cloud grokbot pack setup --id obsidian-operator --bearer-file /run/brigade/grokbot-obsidian.token --runtime-path /var/lib/grokbot-obsidian/runtime.json --action-state-path /var/lib/grokbot-obsidian/actions --approval-dir /var/lib/grokbot-obsidian/approvals --staging-dir /var/lib/grokbot-obsidian/staging --excalidraw-bin /usr/local/bin/excalidraw-helper --upstream-url https://127.0.0.1:27124/ --upstream-key-env GROKBOT_OBSIDIAN_UPSTREAM_KEY
brigade run cloud grokbot pack setup --id obsidian-operator --bearer-file /run/brigade/grokbot-obsidian.token --runtime-path /var/lib/grokbot-obsidian/runtime.json --action-state-path /var/lib/grokbot-obsidian/actions --approval-dir /var/lib/grokbot-obsidian/approvals --staging-dir /var/lib/grokbot-obsidian/staging --excalidraw-bin /usr/local/bin/excalidraw-helper --upstream-url https://127.0.0.1:27124/ --upstream-key-env GROKBOT_OBSIDIAN_UPSTREAM_KEY --apply
brigade run cloud grokbot pack setup --id n8n-operator --bearer-file /run/brigade/grokbot-n8n.token --runtime-path /var/lib/grokbot-n8n/runtime.json --action-state-path /var/lib/grokbot-n8n/actions --approval-dir /var/lib/grokbot-n8n/approvals
brigade run cloud grokbot pack setup --id n8n-operator --bearer-file /run/brigade/grokbot-n8n.token --runtime-path /var/lib/grokbot-n8n/runtime.json --action-state-path /var/lib/grokbot-n8n/actions --approval-dir /var/lib/grokbot-n8n/approvals --apply
brigade run cloud grokbot pack setup --id operations-relay --bearer-file /run/brigade/grokbot-operations-relay.token --owner /srv/owner
brigade run cloud grokbot pack setup --id operations-relay --bearer-file /run/brigade/grokbot-operations-relay.token --owner /srv/owner --apply
brigade run cloud grokbot pack doctor --id operator
brigade run cloud grokbot pack install-service --id operator
brigade run cloud grokbot pack canary --id operator
brigade run cloud grokbot pack update --id operator
brigade run cloud grokbot pack remove --id operator
```

First-party Fleet Steward, Backup Steward, and pending Wazuh findings can be relayed through the generic owner-review path without writing generated manifests. Setup stores only the owner workspace path in `.brigade/grokbot/relay.json`. Preview is the default. The optional timer calls `--apply --limit 50` and does not start, stop, or replace a live connector.

```bash
brigade run cloud grokbot pack relay-setup --target . --owner /srv/owner
brigade run cloud grokbot pack relay-setup --target . --owner /srv/owner --apply
brigade run cloud grokbot pack relay-doctor --target .
brigade run cloud grokbot pack relay --target .
brigade run cloud grokbot pack relay --target . --apply --limit 50
brigade run cloud grokbot pack install-relay-service --target .
```

`install-relay-service` without `--out` prints `brigade-grokbot-findings-relay.service` and `.timer`. The service is oneshot with `UMask=0077`, `TimeoutStartSec=120`, and `ReadWritePaths` limited to the queue state directory, the owner workspace, and a configured Wazuh state parent. The timer uses `OnBootSec=2min`, `OnUnitActiveSec=5min`, `RandomizedDelaySec=30s`, `AccuracySec=30s`, and `Persistent=false`. Review the rendered units before installing them through the host's normal service-management process. This command does not enable a timer or cut over a live relay.

Default pack binds do not collide: operator `127.0.0.1:8766`, repository-scout `127.0.0.1:8767`, implementation-worker `127.0.0.1:8768`, cerebro-memory `127.0.0.1:8770`, fleet-steward `127.0.0.1:8771`, backup-steward `127.0.0.1:8772`, obsidian-operator `127.0.0.1:8773`, wazuh-triage `127.0.0.1:8774`, n8n-operator `127.0.0.1:8775`, and operations-relay `127.0.0.1:8777`. See [n8n-operator](grokbot-n8n-operator.md) for the closed read and action catalog. Custom `--bind` values that reuse another pack's packaged default or installed port are refused without printing config contents. Queue-role apply writes the pack instance store and the legacy role store together; a failed second write restores both to their prior bytes and modes, or to absent on first install, and only after each restored target is verified. Connector apply writes only the pack instance store. A failed restore or verification raises `rollback-failed` without printing config contents. Queue-role packs keep the same exact tool inventory and credential reference as their legacy roles. Cerebro exposes `cerebro_search`, `cerebro_show`, `cerebro_propose`, `cerebro_proposal_status`, and `cerebro_health`. Fleet Steward exposes `fleet_overview`, `host_status`, `incident_bundle`, `propose_remediation`, `service_health`, and `execute_remediation`. Backup Steward exposes `backup_overview`, `backup_target_status`, `backup_restore_readiness`, `backup_operation_status`, `backup_propose_action`, and `backup_execute_action`. Obsidian Operator is a closed connector on public route `/mcp` and exposes only `obsidian_capabilities`, `obsidian_search`, `obsidian_read`, `obsidian_action_status`, `obsidian_propose_action`, and `obsidian_execute_action`. Wazuh Triage exposes `wazuh_ingest`, `wazuh_classify`, `wazuh_alert_status`, `wazuh_incident_bundle`, `wazuh_propose_remediation`, and `wazuh_action_status`. n8n-operator exposes only `n8n_overview`, `n8n_workflow_status`, `n8n_execution_bundle`, `n8n_propose_action`, `n8n_action_status`, and `n8n_execute_action`. operations-relay exposes only `submit_automation_finding` and `automation_finding_status`, and its setup requires `--owner`: an absolute, non-symlink, mode-`0700` directory owned by the current uid. It stores that owner workspace path and the bearer reference only. n8n setup stores bearer plus absolute runtime, action-state, and approval path references. The n8n API-key file reference lives inside the private runtime JSON, never as a CLI secret. Public capability output stays generic: Phase 1 search plus the supported action identifiers. Fleet and Backup setup store bearer plus absolute runtime, ledger, action-state, and approval path references. Obsidian setup stores bearer plus a validated loopback HTTPS upstream URL, an upstream-key env or file reference, and absolute runtime, action-state, approval, staging, and helper-executable references, never a raw upstream key, ledger, or Cerebro CLI paths. The listener resolves the upstream key, requires it to differ from the public bearer and known peer tokens, and constructs the fixed Streamable-HTTP native MCP client. The generated unit may include `--upstream-url` and the upstream-key reference so the service can resolve them without embedding the secret. Those paths stay out of doctor, canary, receipts, and checked-in docs. The generated local unit may include the already-validated action-state and staging directories only as systemd `ReadWritePaths` entries. Approval files stay operator-created and are never written on startup. This pack does not cut over live Obsidian or Backup Steward services. The existing `--instance` commands remain supported.

## Report snapshots

`grokbot_queue_complete` accepts an optional `report_text` string. Send it only for a Repository Scout job whose expected artifact kind is `report`. The text must be non-empty UTF-8 and at most 12,000 bytes. The completion `artifact` remains `{"kind": "report", "path": "<repo-relative path>", "sha256": "<hex>"}`, and `sha256` must be the SHA-256 of those exact UTF-8 bytes. Draft-PR and branch completions reject `report_text`.

Omitting `report_text` keeps the existing metadata-only completion. The job can still reach `completed`. No snapshot is stored, so later operator retrieval fails with the same public validation error as any other rejected tool input.

The MCP HTTP request ceiling is 80,000 bytes. The smaller report cap leaves room for the JSON-RPC envelope, artifact metadata, and escaped C0 controls.

Verified snapshots live at `.brigade/cloud/grokbot/artifacts/<job-id>.md`, beside `jobs` and `idempotency`. The `artifacts` directory is mode `0700`. Each snapshot file is mode `0600`. The queue writes the snapshot before the completed job record.

Operator-only `grokbot_queue_report` takes a `job_id` and returns `job_id`, `text`, `bytes`, and `sha256` for one completed Repository Scout report. It does not return instructions, verification commands, ownership paths, leases, tokens, or other artifact kinds. A worker calling it receives the same public validation error as any hidden tool. Missing legacy snapshots, digest mismatches, oversize files, symlinks, and invalid UTF-8 also return that public error and never include report text.

Report text does not appear in job JSON, list, status, tracker rows, Fleet Hub events, receipts, logs, or error projections.

Update the Repository Scout Grok Bot routine so it sends the report bytes, then let the operator retrieve them:

1. Keep the existing sequence: list, claim, validate role and repository, start, renew before expiry.
2. On `grokbot_queue_complete`, pass `report_text` plus the matching report artifact (`kind`, `path`, and `sha256` of those exact UTF-8 bytes).
3. After the job is `completed`, call `grokbot_queue_report` on the operator listener with that `job_id`.

Do not put report text in chat, GitHub comments, or worker status calls. Metadata-only completion remains valid during rollout, but `grokbot_queue_report` cannot retrieve those jobs.

## Cloudflare boundary

If remote Grok Bot needs the listener, a Cloudflare Tunnel may publish only that listener endpoint. Put Cloudflare Access in front of the published endpoint. Do not publish the fleet hub or any local queue path, and do not replace Access with an unauthenticated tunnel policy. This guide intentionally does not include account-specific Tunnel or Access configuration.

## `grokbot-dispatch-mcp` retirement

Cutover completed 2026-08-30 through PR #1340. The legacy dispatch, Cerebro, and Fleet services are stopped and disabled; their unit files are retained for rollback. Do not stand the sidecar back up for a new deployment. The retained units, the cutover evidence receipts, the soak blockers, and the rollback window end date are recorded in [Retirement and rollback window](phase-grokbot-one-repository-distribution.md).

## Approved feed

The listener only consumes jobs already in the local queue. To put approved work there, write a private manifest and run the local feed command. The command never invents tasks, reads chat, selects GitHub issues, or changes approval state. It is not added to any MCP inventory.

The manifest is a regular file owned by the current user and not group- or world-writable. Schema `brigade.grokbot.feed.v1` requires `approved: true`, a non-empty operator label, and an `entries` array. Each entry has an `idempotency_key` plus the existing Grok Bot job `spec`.

```bash
brigade run cloud grokbot feed --target . --manifest /path/to/approved-feed.json
brigade run cloud grokbot feed --target . --manifest /path/to/approved-feed.json --apply --limit 1
```

The first command validates only and writes no queue state. `--apply` is required to enqueue. `--limit` bounds newly created jobs from 1 through 10 and defaults to 1. Known idempotency records do not consume the limit. The feed stops on the first invalid entry and performs no enqueue when validation fails. Output is counts and safe job projections only.

The command does not schedule itself. systemd, cron, OpenClaw, or another operator may run an approved manifest later. Repeated runs are safe through the existing idempotency store.

## Approved issue selector

`scout-feed` finds one open GitHub issue for a Repository Scout job. The explicit GitHub approval label is the operator gate: the command only considers issues carrying the label named in the private policy. It does not read issue titles, bodies, or comments into queue state or command output.

Store the policy as a regular file owned by the current user and readable only by that owner. Do not use a symlink. For example:

```json
{
  "schema": "brigade.grokbot.scout-feed.v1",
  "approved": true,
  "repository": "example/brigade",
  "approval_label": "grokbot-scout-approved",
  "base_ref": "main",
  "ownership_paths": ["src/brigade", "tests"],
  "verification_commands": [".venv/bin/pytest -q tests/test_grokbot_scout_feed.py"],
  "timeout_seconds": 7200,
  "daily_limit": 3
}
```

```bash
chmod 600 /etc/brigade/grokbot-scout-feed.json
brigade run cloud grokbot scout-feed --target . --policy /etc/brigade/grokbot-scout-feed.json
brigade run cloud grokbot scout-feed --target . --policy /etc/brigade/grokbot-scout-feed.json --apply
```

The first command is preview-only. `--apply` may create at most one job per invocation. `daily_limit` includes every Repository Scout job created that UTC day, including failed and expired attempts. The adapter cannot infer the remaining Grok Bot quota percentage.

An operator may run the apply command hourly with systemd. Replace the executable and policy paths with the local approved locations:

```bash
systemd-run --user --on-calendar=hourly --unit=brigade-grokbot-scout-feed --collect /usr/local/bin/brigade run cloud grokbot scout-feed --target /srv/brigade --policy /etc/brigade/grokbot-scout-feed.json --apply
```

## Report reconciliation

Completed Repository Scout reports stay in the private queue snapshot until an operator asks Brigade to turn them into Memory Handoff drafts for canonical-owner review. The command never edits canonical memory, `MEMORY.md`, or memory cards.

```bash
brigade run cloud grokbot reconcile-reports --target /srv/brigade --owner /srv/owner
brigade run cloud grokbot reconcile-reports --target /srv/brigade --owner /srv/owner --apply --limit 1
```

The first command is preview-only and writes nothing. `--apply` verifies each completed scout report through `read_report`, then creates at most `--limit` deterministic drafts in the owner workspace. The default destination is the canonical owner's review inbox (`memory/handoff-inbox`), so later ingest flags cannot auto-edit canonical memory. `--inbox grok` or another owner-relative writer inbox is available when an operator explicitly wants the normal handoff-ingest path. Absolute paths, parent traversal, and symlink escapes outside the owner workspace are rejected. Each draft names `job_id`, repository, task hash, report SHA-256, and completion time, marks the report as untrusted automation output, and quotes every report line so embedded markdown headings cannot change handoff sections.

Idempotency markers live under `.brigade/cloud/grokbot/reconcile/` on the queue target, mode `0700`/`0600`. Repeated apply does not create a second draft. Corrupt snapshots and conflicting markers fail closed. Legacy completed jobs that predate report snapshots are counted as `unavailable` and left unmarked, so they do not block newer retrievable reports. Output is counts and safe job handles only: report text is never printed and never stored in job JSON, Fleet Hub events, receipts, or marker JSON.

The command does not schedule itself and does not add a tool to any MCP inventory. Apply currently requires POSIX descriptor primitives and fails closed on Windows; preview remains available there.

## Automation findings

Generic one-shot finding delivery stays out of the listener. A producer writes a private manifest. Two commands share that manifest and never edit canonical memory, `MEMORY.md`, or memory cards:

- `reconcile-findings` is memory-only. It writes owner-review drafts and queue markers. It does not create a Fleet outbox and does not call Fleet Hub.
- `relay-findings` is the Fleet + canonical memory owner path. Preview writes nothing. Apply preflights every entry, then selects, spools, and delivers one bounded batch under the Grok Bot queue lock. It calls unchanged `fleet_client.report_event` only for durable outbox records marked ready after draft and marker delivery.

The manifest is a regular file owned by the current user, mode `0600`, and not a symlink. Schema `brigade.grokbot.findings.v1` requires an `entries` array. Each entry has exactly `producer`, `finding_id`, `revision`, `observed_at`, `severity`, `title`, `body`, `source_ref`, `source_digest`, and `content_digest`. Unexpected fields, including secret-shaped keys, are rejected. Identity is `producer` plus `finding_id`. Severity is `info`, `low`, `medium`, `warning`, `high`, `critical`, or `unknown`. `observed_at` is either an empty string for a legacy unknown time or a timezone-aware ISO timestamp. `source_digest` is a `sha256:` lowercase hex digest and is not required to hash the body, so a live revision digest can be represented unchanged. `content_digest` must equal `sha256` of canonical UTF-8 title + NUL + body. `source_ref` is an opaque bounded reference: length and control characters are checked, and consecutive dots are allowed. `adapt_live_finding` converts a live fleet or backup normalized record (extra `trust` / `delivery` labels and a raw 64-hex `source_digest`) into this exact-key shape without rewriting live severity, time, digest, or body values. Live `reason` / `summary` values may be up to 16,384 UTF-8 bytes. The adapter derives `title` as `[UNTRUSTED] {producer} {finding_id}` sliced to 120 characters, matching the live relay `proposalTitle` rule, and preserves the full body. The private manifest may contain title and body. `reconcile-findings` output prints counts and identity handles. `relay-findings` output prints counts and irreversible relay IDs only. Delivery markers, the Fleet outbox, Brigade receipts, and Fleet Hub events must not contain title, body, `source_ref`, producer, finding ID, path, address, command, or credential.

The supported batch conversion path is the CLI. Input schema `brigade.grokbot.live-findings.v1` is a regular owner-only mode-`0600` file with an `entries` array of at most 50 live normalized records. The command preflights every entry through `adapt_live_finding`, rejects duplicate identities, sorts by `producer`, `finding_id`, and `revision`, and writes schema `brigade.grokbot.findings.v1` atomically as mode `0600`. Any invalid later entry, unsafe permission, destination symlink, or symlink in any input or output parent component fails closed: the command does not read through the link and writes nothing. POSIX conversion walks every parent component with descriptor-relative `O_NOFOLLOW`. Windows writes fail closed; Windows reads use the same no-follow parent walk when available and fail closed otherwise. Command output prints counts and identity handles only.

```bash
brigade run cloud grokbot convert-findings --target /srv/brigade --input /path/to/live-findings.json --output /path/to/findings.json
brigade run cloud grokbot reconcile-findings --target /srv/brigade --owner /srv/owner --manifest /path/to/findings.json
brigade run cloud grokbot reconcile-findings --target /srv/brigade --owner /srv/owner --manifest /path/to/findings.json --apply --limit 1
brigade run cloud grokbot relay-findings --target /srv/brigade --owner /srv/owner --manifest /path/to/findings.json
brigade run cloud grokbot relay-findings --target /srv/brigade --owner /srv/owner --manifest /path/to/findings.json --apply --limit 1
```

`reconcile-findings` preview writes nothing. `--apply` preflights the entire manifest, then writes at most `--limit` deterministic drafts or recovery repairs to `memory/handoff-inbox`. `--limit` is 1 through 50 and defaults to 1. Recovery writes count against the same limit. Selection sorts by `producer`, `finding_id`, and `revision` before classification or delivery. A later revision may create a replacement draft with a non-colliding filename. Repeating the same revision is idempotent. Conflicting markers, existing drafts with different bytes, unsafe permissions, symlink traversal, digest mismatches, and malformed later entries fail closed before any write. Preview walks `.brigade`, `cloud`, and `grokbot` with `O_NOFOLLOW` and refuses those parent symlinks. Manifest input paths walk every parent component the same way.

Each draft uses the standard Memory Handoff sections, marks the content as untrusted and review-only, and quotes every producer-controlled title and body line. Delivery markers live under `.brigade/cloud/grokbot/findings/` on the queue target, mode `0700`/`0600`. Marker JSON stores schema, an irreversible identity digest, revision, source digest, content digest, draft relative path, draft SHA-256, and a timezone-aware `delivered_at` only.

`relay-findings` preview is also write-nothing. `--apply` preflights every entry before any write, then holds the queue lock while it selects the bounded batch, creates mode-`0600` outbox records under `.brigade/cloud/grokbot/outbox/`, and writes the matching drafts and delivery markers. Each record moves from `pending` to `ready` only after draft delivery. A replay repairs the transition when interruption lands between marker delivery and the ready write. The Fleet event is built once and persisted: `run_id` is the irreversible relay digest, `seat` and `harness` are fixed labels (`findings-relay` / `grokbot`), `state` is the allowlisted `finding.<severity>` value, and `ts` / `sequence` / `digest` are stable. Brigade calls unchanged `fleet_client.report_event` with every ready record, including records absent from the next producer batch. `report_external_event` is not used because it regenerates `ts`, `sequence`, and `digest`. The outbox is marked `reported` only when `report_event` returns True. False or a raised report leaves the record ready, and the next apply retries the identical event so Fleet Hub can dedupe. Public Python and CLI results contain only counts plus irreversible relay IDs.

Concurrent apply serializes on the existing Grok Bot queue lock. Apply requires POSIX descriptor primitives and fails closed on Windows; preview remains available there. The command does not schedule itself and does not add a tool to any MCP inventory. Custom inbox paths are out of scope.

## Current limits

`setup` writes non-secret configuration and bearer references only. The operator still provisions Grok Bot routines and secrets. This work does not commission a Grok Bot and does not prove weekly quota attribution. A report snapshot is at most 12,000 UTF-8 bytes. The MCP request ceiling is 80,000 bytes.
