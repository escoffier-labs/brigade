# Grok Bot MCP listener

Install the listener with Brigade. Do not clone or install `grokbot-dispatch-mcp` for a new deployment.

```bash
pipx install "brigade-cli[grokbot]"
```

The package contains the adapter source, but it does not turn the Brigade CLI into a listener. Each role runs a separate listener process with a fixed role. The listener delegates queue operations to the queue below its selected Brigade target. It has no independent queue authority. Keep every target local to the operator that owns that queue.

## Role setup and checks

Run these commands from the local Brigade target. Each example uses a distinct loopback port and a separate protected token file. Create each token file through your secret-management process with permissions limited to the service account. The `setup` command stores only the file reference, never the bearer value.

The `install-service` commands below render a systemd unit to standard output. They do not write a unit because they omit `--out`. Review the rendered unit before installing it through the host's normal service-management process. `canary` makes bounded authenticated requests to the running listener and checks anonymous rejection plus the exact role tool inventory. It does not mutate the queue.

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

## Migration from `grokbot-dispatch-mcp`

Migration is an operator-run comparison, not an automatic conversion.

1. Run the old sidecar and the Brigade listener separately, each with its own endpoint and rollback path.
2. Direct one test bot to the Brigade listener while the remaining bots use the old sidecar.
3. Confirm that the test bot receives the matching role-specific tool inventory, then run its passing non-mutating canary.
4. Keep the old sidecar available until the comparison has passed. Remove it only after the operator decides to cut over.

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

## Current limits

`setup` writes non-secret configuration and bearer references only. The operator still provisions Grok Bot routines and secrets. This work does not commission a Grok Bot and does not prove weekly quota attribution. A report snapshot is at most 12,000 UTF-8 bytes. The MCP request ceiling is 80,000 bytes.
