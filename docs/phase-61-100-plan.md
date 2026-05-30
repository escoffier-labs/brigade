# Brigade Phase 61-100 Plan

This plan is the working queue for the next long roadmap completion run. It turns the remaining roadmap gaps, deferred items, and hardening needs into finite phases with testable stop conditions.

## Boundaries

- Keep behavior local, explicit, and receipt-backed.
- Do not add a daemon, scheduler mutation, hosted UI, database, remote sync, automatic promotion, automatic fixes, automatic memory mutation, automatic publish action, or secret storage.
- Do not copy private repo names, owner names, org names, private paths, raw chat, raw logs, raw scanner output, hostnames, tokens, webhook URLs, or exact external reference repo names into public docs, fixtures, imports, handoffs, or release evidence.
- Each phase must update focused tests, docs when public behavior changes, a Memory Handoff when durable knowledge is produced, and the roadmap status when complete.

## Phase Queue

### Phase 61: Roadmap Audit Precision And Phase Queue
Goal: make `brigade roadmap audit` accurate enough to drive phases 61-100. Normalize documented commands, stop treating prose and parameterized examples as missing CLI commands, close known pattern-owner gaps, and add this phase queue as the public source of truth.

Acceptance: roadmap audit tests cover command normalization, parent-command examples, prose filtering, stale phase behavior, and pattern coverage. README, ROADMAP, CHANGELOG, and this plan document the phase queue.

Status: implemented.

### Phase 62: Roadmap Ownership And Deferred Item Records
Goal: add explicit roadmap ownership records for deferred items so later phases can close or re-defer them without ambiguous status text.

Acceptance: roadmap audit JSON includes owner, subsystem, deferred reason, and suggested next phase for known deferred items.

Status: implemented with `deferred_items` in `brigade roadmap audit --json`.

### Phase 63: Public Command Documentation Contract
Goal: make public docs and CLI command discovery agree through a stable command contract, without relying on fragile prose scans.

Acceptance: tests compare documented command snippets to parser command paths and public docs list every supported top-level command group.

Status: implemented with `brigade roadmap commands`.

### Phase 64: Cross-Producer Provenance Compatibility Audit
Goal: audit scanner, backup, memory-care, security, review, tool-catalog, repo-fleet, and learning imports for a common provenance and fingerprint contract.

Acceptance: focused fixtures prove every local producer writes source fingerprints, safe summaries, evidence labels, and dismissed-until-changed metadata.

Status: implemented with `brigade work import provenance` and inbox doctor provenance contract warnings.

### Phase 65: Producer Privacy Regression Suite
Goal: add shared privacy fixtures proving raw private evidence never leaks from producer outputs into imports, handoffs, context packs, release evidence, or public docs.

Acceptance: tests cover chat, backup, security, repo-fleet, context, learning, and release paths with redaction assertions.

Status: implemented with `tests/test_privacy_regression.py`, safe context metadata summaries, learning safe-summary fallback, and release note input redaction.

### Phase 66: Chat Export Provider Alias Completion
Goal: complete local export-family aliases for configured chat surfaces while keeping live APIs out of scope.

Acceptance: tests cover alias validation, ingest, import counts, sweep review, task promotion, and handoff promotion for configured export families.

Status: implemented with provider alias normalization, expanded starter surfaces, JSONL fixtures, scanner sweep review coverage, task promotion, and handoff promotion.

### Phase 67: Backup Closeout Policy And Operator Summary
Goal: make backup closeouts quiet reviewed risks while surfacing changed fingerprints, stale restore rehearsal evidence, and safe operator status summaries.

Acceptance: tests cover closeout quieting, changed-risk resurfacing, restore rehearsal release evidence, and no private destination leakage.

Status: implemented with raw versus active backup issue counts, quieted reviewed or deferred issue counts, changed fingerprint surfacing, restore rehearsal evidence in release readiness, and safe operator summaries in backup status and the daily brief.

### Phase 68: Tool Projection Parity Closeout
Goal: add explicit parity closeout receipts for portable tool projections across configured harnesses.

Acceptance: tests cover current, stale, missing, conflicted, unmanaged, deferred, and reviewed parity states in doctor, brief, and import routing.

Status: implemented with `brigade tools parity status/closeout`, local parity closeout receipts, quieting for unchanged reviewed or deferred projection issues, changed-fingerprint resurfacing, doctor and brief integration, and import routing through active parity issues only.

### Phase 69: Tool Pack Release Evidence Integration
Goal: make tool packs and sync plans first-class release evidence without applying projections automatically.

Acceptance: release readiness and candidate tests include tool pack freshness, parity closeout state, approvals, run history, and sync blockers.

Status: implemented with tool pack health, stale pack detection, sync-plan blocker summaries, parity closeout evidence, approval queue counts, run history, and checkpoint state in release readiness and release candidate evidence.

### Phase 70: Context Sync Planning Receipts
Goal: add reviewable context sync plans and receipts for configured harness destinations without writing context files by default.

Acceptance: tests cover context sync plan text and JSON, freshness checks, conflicts, and release/operator-center integration.

Status: implemented with `brigade context sync plan/record`, configured harness destination checks, stale pack and missing source-reference warnings, unmanaged conflict detection, local sync-plan receipts, release evidence, and operator-center activity/review integration.

### Phase 71: Context Pack Freshness And Import Routing
Goal: surface stale context packs, missing references, outdated task acceptance, and stale tool references as reviewable imports.

Acceptance: tests cover doctor, work brief, center reviews, release readiness, and `source: context-pack` imports.

Status: implemented with `brigade context doctor/import-issues`, stale pack checks, missing source-reference checks, task acceptance drift detection, stale tool-reference detection, daily brief and work doctor visibility, center review queue entries, release evidence, and `source: context-pack` imports.

### Phase 72: Project Migration Readiness Receipts
Goal: turn project audit decisions into local readiness receipts for docs, license, security, release, ownership, and migration blockers.

Acceptance: tests cover bake-in, integrate, catalog-only, move-candidate, and leave-alone readiness without remote mutation.

Status: implemented with `brigade projects readiness plan/record/list/show`, decision-specific readiness requirements, local receipts under `.brigade/projects/readiness/`, release evidence integration, deduped project-consolidation imports, and manual-only migration evidence.

### Phase 73: Project Move Plan Closeout
Goal: add reviewed closeout for manual-only project move plans so deferred moves do not stay noisy unless fingerprints change.

Acceptance: tests cover reviewed, deferred, superseded, archived, changed-fingerprint, and import routing states.

Status: implemented with `brigade projects closeout/closeouts/closeout-show`, local project closeout receipts under `.brigade/projects/closeouts/`, quieting for reviewed, deferred, and archived unchanged readiness issues, superseded non-quieting records, changed-fingerprint resurfacing, and import routing through active project closeout issues.

### Phase 74: Learning Accepted-Risk And Dismissal Quieting
Goal: add source-aware quieting for learning candidates that become accepted risk, dismissed, archived, or deferred.

Acceptance: tests cover scanner, security, review, tool, handoff, memory-care, backup, and release learning candidates.

Status: implemented with `brigade learn closeout/closeouts/closeout-show`, source-aware candidate fingerprints, quieting for accepted-risk, dismissed, archived, and deferred outcomes, changed-fingerprint resurfacing, and import routing through active learning candidates only.

### Phase 75: Learning Replay Compare Receipts
Goal: compare learning replay receipts before and after code, rule, or policy changes without editing memory or source automatically.

Acceptance: tests cover replay export, replay compare, redaction, release evidence, and center reviews.

Status: implemented with `brigade learn replay export/list/show/compare`, redacted before/after replay receipts under `.brigade/learn/replays/`, compare receipts under `.brigade/learn/replay-compares/`, release evidence integration, and operator-center review surfacing for regressions.

### Phase 76: Dependency-Free Security SARIF Export
Goal: add SARIF output for security findings if it can be implemented with no dependencies, otherwise record a precise deferral.

Acceptance: tests cover SARIF schema shape or a documented deferral with release evidence and roadmap status.

Status: implemented with dependency-free SARIF 2.1.0 generation in security scan bundles, `brigade security sarif` regeneration from existing reports, release evidence for SARIF readiness, and redacted SARIF payload tests.

### Phase 77: Agent Instruction Guardrail Fixtures
Goal: expand security fixtures for repo guidance, skills, slash commands, subagents, tool wrappers, and prompt-injection risks.

Acceptance: tests cover guardrail categories, runtime confidence, public-template treatment, and safe remediation imports.

Status: implemented with guardrail fixtures and surface labels for repo guidance, skills, slash commands, subagents, and tool wrappers, plus prompt-injection and environment-exfiltration findings, runtime versus template confidence tests, and safe remediation import assertions.

### Phase 78: Security Policy Pack Closeouts
Goal: make personal, public-repo, CI, and strict security policy packs reviewable with accepted-risk closeouts.

Acceptance: tests cover policy-specific blockers, warnings, suppressions, accepted risk, release readiness, and release candidates.

Status: implemented with a `ci` policy preset, security closeout policy-pack evidence for blocker and warning counts, accepted-risk metadata, and release readiness plus release candidate evidence for latest policy closeouts.

### Phase 79: Repo-Shareable Workflow Rule Templates
Goal: add public-safe workflow rule templates for issue/TDD loops without embedding personal preferences.

Acceptance: tests cover install output, docs, privacy scan, and work doctor visibility.

Status: implemented with repo install templates under `rules/`, `brigade work doctor` visibility, docs, and focused install/doctor tests.

### Phase 80: Stale Active Issue Repair Imports
Goal: route stale active issue context and closed remote issue mismatches into repairable local imports.

Acceptance: tests cover issue-backed tasks, missing issue context, closed remote issue checks, and no GitHub mutation.

Status: implemented with `brigade work import issue-repairs`, stable source fingerprints, missing-context and closed-remote tests, unavailable-`gh` handling, and no GitHub mutation coverage.

### Phase 81: Task Acceptance Release Rollup Hardening
Goal: improve acceptance coverage rollups across pending tasks, completed tasks, review findings, work closeouts, release readiness, and release candidates.

Acceptance: tests cover acceptance gaps, completion metadata gaps, review-finding task outcomes, and release evidence.

Status: implemented with hardened `brigade work acceptance` payloads, release readiness blockers, release candidate evidence, and focused work/release tests.

### Phase 82: Memory Card Freshness Metadata Review
Goal: make memory-care status explain missing or stale freshness metadata without editing memory cards.

Acceptance: tests cover reviewed dates, freshness dates, confidence, evidence metadata, and memory-care imports.

Status: implemented with `missing-reviewed` and `missing-freshness` memory-care issues, metadata coverage summaries in status and JSON, import routing, docs, and focused tests.

### Phase 83: Memory-Care Safe Autofix Planning
Goal: add planning only for low-risk memory metadata repairs, with no automatic memory mutation.

Acceptance: tests cover safe plans, blocked plans, raw evidence exclusion, and handoff/task review paths.

Status: implemented with `brigade memory care plan-fixes`, blocked reviewed/freshness metadata repair candidates, no-write assertions, import metadata, daily brief visibility, docs, and focused tests.

### Phase 84: Handoff Ingest Warning Parser Hardening
Goal: improve local parsing of ingestor warning logs including no-reply, skipped, failed, malformed, and unreachable-source states.

Acceptance: tests cover normalized receipts, draft reconciliation, repair imports, and daily brief quieting.

Status: implemented with hardened issue parsing, normalized reconcile warning events, skipped/failed/malformed/unreachable/no-reply receipt fields, docs, and focused tests.

### Phase 85: Handoff Source Coverage Repair Flow
Goal: turn uncovered handoff inboxes and source config drift into reviewed work imports and closeouts.

Acceptance: tests cover source coverage doctor, imports, dismissed-until-changed behavior, and no canonical memory edits.

Status: implemented with fingerprinted source coverage imports, dismissed-until-changed uncovered-inbox behavior, missing configured inbox doctor/import coverage, no-memory-edit assertions, docs, and focused tests.

### Phase 86: Release Evidence Schema Manifest
Goal: add a machine-readable schema manifest for release readiness, candidates, train bundles, waivers, and evidence records.

Acceptance: tests cover schema manifest generation, missing receipt detection, and wrapper-friendly JSON.

Status: implemented with `brigade release schema`, manifest entries for readiness receipts, candidate evidence, fleet trains, waivers, and manual evidence records, missing receipt checks, docs, and focused tests.

### Phase 87: Release Candidate Provenance Audit
Goal: audit release candidate bundles for stale receipts, missing evidence, changed HEAD, changed docs, changed command contracts, and privacy boundaries.

Acceptance: tests cover audit text and JSON, release doctor integration, and import routing.

Status: implemented with `brigade release candidate audit`, `brigade release candidate import-issues`, candidate command-contract fingerprints, provenance/privacy checks, release doctor integration, docs, and focused tests.

### Phase 88: Operator Center JSON Schema Export
Goal: export stable local JSON schemas for center status, activity, reviews, templates, reports, and action queues.

Acceptance: tests cover schema output, read-only behavior, and wrapper-facing field stability.

### Phase 89: Operator Report Diff Receipts
Goal: compare two operator reports and write local diff receipts showing changed queues, resolved items, new blockers, and stale references.

Acceptance: tests cover report diff text and JSON, center activity, work doctor, and release doctor integration.

### Phase 90: Operator Action Aging And SLA Policy
Goal: add local aging thresholds and review policies for operator actions without executing suggested commands.

Acceptance: tests cover stale pending, stale active, deferred too long, archived completed, and import routing.

### Phase 91: Safe Repo Root Discovery Plan
Goal: add an explicit root discovery plan for repo fleet entries under configured roots without scanning arbitrary home directories by default.

Acceptance: tests cover dry-run discovery, include/exclude rules, safe labels, private path redaction, and no cloning.

### Phase 92: Fleet Health Command Registry
Goal: make optional read-only fleet health commands named, validated, and receipt-backed across configured repos.

Acceptance: tests cover command labels, timeouts, high-risk command refusal, stale receipts, and fleet report integration.

### Phase 93: Fleet Dispatch Supersede Reports
Goal: add reports explaining dispatch supersession, target import changes, dismissed target imports, and broken references.

Acceptance: tests cover dispatch history, superseded imports, reconciliation warnings, and center review integration.

### Phase 94: Fleet Release Matrix Report
Goal: generate a release train matrix across repos, evidence steps, waivers, readiness, closeouts, and manual publish evidence.

Acceptance: tests cover Markdown and JSON matrix files, no remote mutation, and release doctor integration.

### Phase 95: Fleet Waiver Policy Templates
Goal: add local templates and checks for waiver expiry, renewal reason quality, review owner labels, and waiver scope.

Acceptance: tests cover waiver policy warnings, imports, ready gate visibility, and no hidden blockers.

### Phase 96: CI Platform Deprecation Watcher
Goal: detect local GitHub Actions platform deprecation warnings and route them into release readiness without mutating workflows.

Acceptance: tests cover Node action deprecation summaries, safe excerpts, work imports, release evidence, and no network requirement.

### Phase 97: Install Smoke Matrix Receipts
Goal: store local install smoke-test matrix receipts matching supported harness combinations.

Acceptance: tests cover receipt parsing, stale smoke warnings, release candidate evidence, and center activity.

### Phase 98: Public Template Privacy Audit
Goal: add a focused audit for public templates to prove they contain placeholders, not private operator state.

Acceptance: tests cover workspace templates, harness templates, docs references, allowlisted examples, and privacy scan integration.

### Phase 99: Docs Command Contract Generator
Goal: generate or verify a docs command inventory from the CLI parser so public docs stay aligned.

Acceptance: tests cover generated inventory, drift warnings, roadmap audit integration, and no private content.

### Phase 100: Local Operator Readiness Closeout
Goal: build one final local readiness closeout over roadmap audit, center state, release evidence, repo fleet, security, memory, tools, context, learning, and docs command contracts.

Acceptance: tests cover clean ready, blocked ready, waiver-aware ready, imports for unresolved readiness issues, and a manual-only publish checklist.
