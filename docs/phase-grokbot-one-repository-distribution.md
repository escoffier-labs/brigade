# Grok Bot one-repository distribution

## Status

Approved by the operator on 2026-08-27 and tracked in #1255.

## Existing state

#1130 proved that Brigade can queue bounded work, Grok Bot can run it on its
cloud computer, and Brigade can reconcile a report or pull request. #1163
moved the role-scoped queue adapter into `brigade-cli` and removed the second
repository from the coder-lane install path.

The remaining dependency on `grokbot-dispatch-mcp` is operational. It still
contains generic automation relay behavior and the current Cerebro, Fleet
Steward, Backup Steward, and Obsidian connectors. That leaves two installers,
two update paths, and two documentation sets for one Brigade feature family.

## Product contract

Brigade owns the supported Grok Bot integration from install through removal:

- one source repository
- one Python package and optional dependency extra
- one CLI namespace
- one release and update stream
- one documentation set

The runtime remains split into role-scoped processes. Each process keeps its
own credentials, port, exact tool inventory, health state, and restart policy.
The Fleet Hub stays private and is never mounted behind a public connector.

## State and trust boundaries

The canonical owner reviews all durable findings before memory ingestion.
Rocinante remains the canonical memory owner.

An automation producer may emit a bounded finding descriptor. The private
producer manifest (`brigade.grokbot.findings.v1`) carries routing metadata,
identity, timestamps, a content digest, a local source reference, and may
include title and body because it is the approved private producer artifact.
The generic entry represents live fleet and backup values without weakening
validation: severities include `warning` and `unknown`, `observed_at` may be
empty, `source_digest` is a `sha256:` digest that is not forced to hash the
body, and `content_digest` is the required title + NUL + body integrity check.
Instructions, credentials, tokens, and arbitrary environment data stay out.
Finding title and body never enter Brigade receipts, delivery markers, CLI
output, or Fleet Hub projections. Live fleet `reason` and backup `summary`
values may reach the live maximum; `adapt_live_finding` derives the generic
title with the live relay `proposalTitle` rule and keeps the full body.
`source_ref` is an opaque bounded reference and may contain consecutive dots.
The supported batch conversion path is
`brigade run cloud grokbot convert-findings --input <live.json> --output <findings.json>`.

The relay verifies the descriptor and artifact, creates one deterministic
Memory Handoff draft in the owner's review inbox, and records an idempotent
delivery receipt. The finding remains untrusted automation output until the
owner's normal review and ingestion flow accepts it.

Fleet Hub may carry safe coordination projections, such as finding handles and
delivery state. It must not carry finding text, report text, credentials, or
connector bearer values.

## CLI direction

The current queue commands remain under `brigade run cloud grokbot`. Connector
packs use the same namespace under `brigade run cloud grokbot pack`. The
supported operations are fixed:

- configure a role or connector without storing secret values
- inspect configuration and endpoint health
- render or install an isolated service
- run bounded local and public canaries
- update a connector pack with a versioned migration
- preview and apply removal

Profiles may group supported components for setup, but a profile never combines
their processes or credentials.

## Delivery sequence

### PR 1: AutomationFinding relay and owner delivery

1. Add failing tests for the finding schema, file ownership and permissions,
   digest verification, idempotency, deterministic draft paths, delivery
   receipts, redacted output, and concurrent apply.
2. Implement a preview-first local import and owner-delivery module.
3. Add CLI parsing and safe JSON and text projections.
4. Do not add a new daemon or service-rendering command. The existing systemd
   timer can call the one-shot command after cutover.
5. Document producer and owner contracts.

This slice does not migrate a live connector or disable the sidecar.

### PR 2: connector-pack registry and lifecycle

1. Define a versioned first-party pack manifest
   (`brigade.grokbot.connector-pack.v1`) and local instance config
   (`brigade.grokbot.connector-instance.v1`).
2. Add preview-first `brigade run cloud grokbot pack` commands for list, show,
   setup, doctor, canary, install-service, update, and remove.
3. Reject duplicate pack IDs, duplicate ports, overlapping non-empty public
   routes, unsafe or non-loopback default binds, invalid versions, missing or
   weak secret references, and declared tool inventory that differs from the
   runtime allowlist.
4. Keep secrets outside Brigade state and repository content. The first
   packaged packs are the existing queue roles, with isolated default ports
   8766, 8767, and 8768. Legacy `setup` / `doctor` / `canary` /
   `install-service` remain compatible. This slice does not start, stop, or
   reload services.

### PR 3: first-party connector packs

Move the supported Cerebro, Fleet Steward, Backup Steward, and Obsidian
connectors into Brigade. Preserve their current external URLs during rollout.
Keep each connector isolated. Split this work when one pull request would make
review or rollback unsafe.

### PR 4: cutover and retirement

1. Preview and apply a reversible state migration to Brigade-owned paths.
2. Run local and public edge parity canaries for each component.
3. Prove that one Grok Bot finding reaches Rocinante's review inbox exactly
   once and follows normal memory ingestion.
4. Disable legacy services after parity passes and retain a time-bounded
   rollback path.
5. Remove the supported-user dependency on `grokbot-dispatch-mcp` and archive
   it after the soak period.

## Compatibility

- Current Grok Bot connector URLs remain stable during the migration.
- Existing queue tool names, schemas, roles, and lease behavior remain stable.
- Existing operator, Repository Scout, and Implementation Worker services stay
  available throughout the work.
- State migration is preview-first and refuses symlinks, unsafe permissions,
  conflicting destination records, and digest mismatches.
- A failed migration leaves the legacy service enabled and its source state
  unchanged.

## Security requirements

- Do not store GitHub PATs, Cloudflare credentials, Gmail credentials, origin
  bearers, or Fleet Hub node tokens in Brigade config, queue records, finding
  descriptors, receipts, logs, or repository fixtures.
- Default every listener to loopback.
- Require an explicit public route, Cloudflare Access, and a separate origin
  bearer for a remote connector.
- Enforce each connector's tool allowlist in discovery and direct dispatch.
- Bound request bytes, artifact bytes, processing time, and per-run mutation.
- Keep preview as the default for imports, migrations, and removal.
- Treat all Grok Bot output as untrusted until owner review.

## Non-goals

- One listener process for all connectors.
- One shared connector credential.
- A public Fleet Hub endpoint.
- Personal host paths or secret values in the public repository.
- Unrestricted tools or mutation authority for every bot.
- Automatic canonical-memory edits.

## Acceptance

1. A clean environment installs all supported Grok Bot components from
   `brigade-cli` without cloning another repository.
2. Brigade configures, diagnoses, renders services for, canaries, updates, and
   removes every supported component.
3. Existing coder-lane canaries remain green.
4. Each migrated connector rejects missing edge identity and missing origin
   bearer, exposes its exact tool inventory, and passes one bounded functional
   canary.
5. One AutomationFinding reaches the owner's review inbox exactly once with a
   delivery receipt. Report text does not enter Fleet Hub or Brigade receipts.
6. Rocinante receives the handoff through the normal memory path without a
   manual request to Grok Bot.
7. Disabling the old sidecar does not break supported Brigade functionality.

## Verification and review

Each slice follows tests-first development and runs its focused tests through
`brigade work verify run`. Before a pull request is marked ready, run
`./scripts/verify` through the same Brigade work loop. Record the outcome and
write a Memory Handoff for durable behavior or migration findings.

Use `Refs #1255` on intermediate pull requests. The final retirement pull
request uses `Fixes #1255` only after every acceptance item has live evidence.
