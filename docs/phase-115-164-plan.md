# Brigade Phase 115-164 Plan

This plan is the source of truth for the next production-hardening tranche. The purpose is to refine the local daily operator system, not to add a UI, daemon, scheduler, database, remote sync, or broad new subsystem.

## Guardrails

- Keep `brigade daily` as the wrapper-facing entrypoint.
- Prefer schema stability, auditability, receipt quality, and recovery over new command sprawl.
- Keep all behavior local and explicit.
- Do not run arbitrary suggested commands.
- Do not start scanners, reviewers, tools, fleet sweeps, or publish flows automatically.
- Do not mutate remotes, push, tag, publish, edit issues, edit PRs, transfer repos, or change visibility.
- Do not edit canonical memory.
- Do not add dependencies.
- Keep raw private paths, logs, scanner output, repo names, owner names, org names, hostnames, tokens, private URLs, raw chat text, and private evidence out of public docs, fixtures, imports, handoffs, release evidence, and committed diffs.

## Workstream 1: Phase 115-124, Daily Production Hardening

Goal: make the daily loop recoverable, explainable, and consistently receipt-backed.

- 115: Audit daily config and unsafe local policy states.
- 116: Verify latest run receipts have normalized adapter results.
- 117: Verify latest plan receipts have candidate explanations.
- 118: Track approval hygiene and stale approval requests.
- 119: Track telemetry warnings and repeated blockers.
- 120: Route unresolved daily reliability findings into the work inbox.
- 121: Close out reviewed daily reliability findings.
- 122: Keep daily protocol output stable for wrappers.
- 123: Keep JSON output clean when wrapped commands print noise.
- 124: Carry daily reliability state into release evidence.

Status: implemented.

Implementation notes:

- `brigade daily hardening plan` marks phases 115-124 as implemented while later phases remain planned.
- `brigade daily hardening audit` now checks recent daily run receipts for normalized adapter result fields, recent daily plan receipts for candidate explanations, approval hygiene, telemetry warnings, protocol step coverage, wrapped-output leakage, and release evidence coverage.
- `brigade daily hardening import-issues` preserves phase number and phase title metadata on generated work imports.
- `brigade daily hardening closeout` stores finding fingerprints so reviewed findings stay quiet until their source fingerprint changes.
- Release readiness and release candidate evidence now include daily hardening summary state.

## Workstream 2: Phase 125-134, Operator Center Contract Cleanup

Goal: keep center status, activity, reviews, templates, and schema output consistent for wrappers.

- 125: Audit center schema manifest presence.
- 126: Audit center review item field coverage.
- 127: Verify review items include suggested next commands.
- 128: Verify receipt references stay local and safe.
- 129: Surface center contract findings in daily hardening audit.
- 130: Route center contract findings into the work inbox.
- 131: Keep center status readable without requiring subsystem-specific parsing.
- 132: Keep center reviews as the unified local review queue.
- 133: Document center schema expectations for wrappers.
- 134: Carry center contract state into release evidence.

Status: implemented.

Implementation notes:

- Center schema output now includes a `center-contract-health` contract that documents the audit payload used by daily hardening and release evidence.
- The center contract health payload validates required schema ids, required item fields, suggested next commands, and unsafe reference shapes across status, activity, reviews, and templates.
- `brigade daily hardening audit` consumes center contract health and routes contract findings with phase metadata.
- Release readiness and release candidate evidence now include an operator-center contract summary.

## Workstream 3: Phase 135-144, Inbox And Evidence Quality

Goal: reduce queue noise and improve provenance, acceptance, and evidence quality.

- 135: Audit pending imports missing acceptance.
- 136: Audit pending imports missing provenance.
- 137: Audit inbox hygiene issues.
- 138: Penalize noisy and deferred imports in daily planning.
- 139: Preserve changed-fingerprint resurfacing.
- 140: Route inbox quality findings into the work inbox.
- 141: Keep scanner, review, security, backup, memory-care, and project imports deduped.
- 142: Keep daily action selection biased toward high-evidence items.
- 143: Document inbox quality expectations.
- 144: Carry inbox quality state into release evidence.

Status: implemented.

Implementation notes:

- `work_cmd._inbox_quality_payload` now scores pending imports for acceptance, provenance, age, deferred state, noisy source history, duplicate pending identity, and changed dismissed fingerprints.
- `brigade daily plan` carries inbox quality metadata on import candidates and uses the shared quality score in ranking.
- `brigade daily hardening audit` now reports phase-specific inbox quality findings for acceptance, provenance, hygiene, stale/deferred/noisy imports, changed dismissed fingerprints, duplicate pending imports, and weak top candidates.
- Release readiness and release candidate evidence now include inbox quality summaries.

## Workstream 4: Phase 145-154, Repo Fleet Daily Use

Goal: keep fleet reports, actions, dispatch, and release trains visible in daily planning without making fleet execution automatic.

- 145: Audit repo fleet health from the daily hardening layer.
- 146: Surface fleet action queue health.
- 147: Surface fleet sweep health.
- 148: Surface fleet release train health.
- 149: Route fleet daily-use findings into the work inbox.
- 150: Keep fleet dispatch manual and local.
- 151: Keep fleet release plans manual-only.
- 152: Keep safe repo labels and receipt labels only.
- 153: Document fleet daily-use expectations.
- 154: Carry fleet state into release evidence.

## Workstream 5: Phase 155-164, Self-Dogfood Release Loop

Goal: make Brigade's own local release path readable through daily receipts and release evidence.

- 155: Audit latest release readiness receipt.
- 156: Audit latest release candidate packet.
- 157: Verify release candidate evidence includes daily driver state.
- 158: Surface blocked release readiness in daily hardening audit.
- 159: Route release dogfood findings into the work inbox.
- 160: Keep publish steps manual-only.
- 161: Keep daily closeout verification evidence visible.
- 162: Document the daily-to-release self-dogfood path.
- 163: Keep release schema output stable for wrappers.
- 164: Close out this hardening tranche with tests, docs, privacy scan, handoff, commit, and push.

## Command Surface

- `brigade daily hardening plan`
- `brigade daily hardening audit`
- `brigade daily hardening import-issues`
- `brigade daily hardening closeout`

These commands are local and receipt-backed. They do not execute fixes, promote imports, run tasks, run scanners, run reviewers, run tools, run fleet sweeps, mutate remotes, publish releases, or edit canonical memory.
