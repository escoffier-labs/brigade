# Roadmap Completion Plan

This document is the source of truth for the next large Brigade implementation goal. It closes the remaining roadmap gaps without turning Brigade into a daemon, remote sync engine, or product-specific chat adapter.

## Objective

Finish Brigade as a local operator system for agent work:

- discover useful work from local scanners and repo health checks
- review and promote that work through tasks or Memory Handoffs
- execute approved local tools with receipts
- verify, review, close out, and package releases locally
- inspect a workspace full of repos without taking over those repos
- preserve the best patterns from command, skill, subagent, MCP, self-learning memory, context engineering, local security gate, and multi-harness review systems

## Non-Goals

- No remote mutation by default.
- No daemon, scheduler install, cron mutation, systemd mutation, or background worker.
- No product-specific live chat APIs.
- No automatic promotion, automatic memory mutation, automatic fixes, automatic releases, or automatic approvals.
- No secret storage.
- No new dependencies unless explicitly approved.
- No attempt to implement the product roadmap of every repo under `~/repos`.
- No repository transfer, archival, or ownership change without explicit approval.

## Inspiration And Fleet Boundary

Brigade should do two different things with external repos and reference projects:

1. Keep fleet management for the operator's local repos.
2. Harvest durable workflow patterns from the agent/tooling projects we selected for inspiration.

Those are related, but not the same.

### Fleet Management

Covered:

- Read repo-local `AGENTS.md`, root `~/AGENTS.md`, `~/repos/AGENTS.md`, and fallback `CLAUDE.md` guidance as inspectable repo metadata.
- Detect repos with stale or missing local workflow setup, missing handoff inbox coverage, missing release readiness, missing publish-guard integration, dirty state, stale roadmap items, or absent test command hints.
- Import repo health findings into Brigade's scanner inbox as reviewed local work.
- Support release, review, scanner, handoff, and publish-guard evidence for repos that opt in.
- Preserve publish-guard tip and introduced-content summaries in Brigade release evidence.

Not covered:

- Brigade will not implement unrelated product-analysis features, publish-guard policy model phases, app-specific product features, or repo-specific business logic.
- Brigade will not edit other repos automatically.
- Brigade will not copy private repo guidance or raw local evidence into public Brigade docs.

### Feature Inspiration Coverage

The larger Brigade roadmap should intentionally absorb the useful patterns we selected from external command, memory, skill, security, context-engineering, and cross-harness sync projects, while implementing them in Brigade's local, receipt-backed style.

Reference set:

- Command-harness best-practice material for commands, skills, subagents, project settings, MCP, memory, and permission boundaries.
- Ordered delivery-loop projects for think, plan, multi-role review, QA, release, and learn patterns.
- Durable memory projects for markdown-backed memory, retrieval and graph discipline, skillpacks, job receipts, eval replay, and bounded overnight learning loops.
- Portable skill libraries for `SKILL.md` packaging, focused engineering workflows, planning, TDD, architecture review, and git safety.
- Agent guardrail projects for workspace security posture, policy checks, prompt-injection risk, tool permissions, secrets, and reportable findings.
- Context-engineering projects for staged context packs, command/checklist driven context preparation, and explicit sync into harness-specific contexts.
- Skill/plugin sync tools for cross-harness portability patterns: dry-run planning, add-only sync, managed headers or sidecars, no silent deletes, and explicit conflict handling.
- Related local side projects for consolidation decisions, named only in private or gitignored local config.

Covered inspiration:

- Command-harness slash commands, skills, subagents, memory handoffs, MCP config discovery, project settings, and permission boundaries.
- MCP runner patterns from MCP-focused tooling: config import, stdio and HTTP server metadata, tool listing, call planning, daemon or runtime supervision, timeout handling, and generated wrappers.
- Multi-harness patterns: portable command projection, harness-specific files, parity checks, and local extension or plugin surfaces.
- Agent protocol and orchestration patterns: explicit session ids, run receipts, reviewer producers, subagent outputs, structured findings, and closeout records.
- Delivery-loop patterns: role-based plan review, design review, engineering review, QA review, release manager review, and sprint learning.
- Durable-memory patterns: markdown as source of truth, hybrid retrieval as an implementation detail, memory repair jobs, citation repair, eval export/replay, and learning cycles that produce reviewable receipts.
- Portable-skill patterns: small composable skills, `SKILL.md` as the portable unit, TDD and planning skills, architecture grilling, issue generation, and git guardrails.
- Agent-security patterns: local-only scans, risky tool permission findings, prompt-injection checks, policy reports, and actionable remediation without automatic mutation.
- Context-engineering patterns: explicit context pack planning, scoped context export, stale context warnings, and harness-specific sync receipts.
- Cross-harness sync patterns: one portable source of truth for skills/plugins/commands, exact projection plans, dry-run/apply split, managed metadata, and conflict-safe updates.
- Side-project consolidation patterns:
  - bake tiny, stable workflow primitives into Brigade when Brigade is the natural control plane
  - integrate larger tools by receipts and scanner imports when they have their own product surface
  - keep domain-specific MCP servers as separate repos but make Brigade able to catalog, test, run, and release them
  - consider moving public reusable side repos to an organization when they are no longer personal experiments and need org-owned issue, release, security, and documentation workflows
- Self-learning patterns: durable findings become reviewed Memory Handoffs, memory-care refresh candidates, accepted-risk records, or scanner inbox tasks, never automatic canonical memory edits.
- Security patterns: prompt and instruction scanning, MCP config auditing, permission and hook checks, local evidence bundles, suppressions, accepted risk, and policy-backed release gates.
- Publishing patterns: content guard scans, introduced-content scans, release readiness receipts, release candidate bundles, and manual-only remote publish plans.

Not covered:

- Brigade does not need to be source-compatible with those projects.
- Brigade does not need to use their implementation languages or runtime stacks.
- Brigade does not start live adapters or mutate external tool configs unless an explicit local apply command already exists.
- Brigade does not silently sync skills, plugins, or context files across harnesses. Projection and sync remain explicit local commands.
- Brigade does not turn self-learning into unbounded self-modification. Every learning loop must end as a reviewed task, reviewed handoff, accepted suppression, or local receipt.
- Brigade does not clone, vendor, or name exact reference repositories in public docs by default.
- Brigade does not automatically move repos between GitHub owners. It can produce local migration plans only.

## Completion Themes

### 1. Roadmap State Audit And Closure Map

Deliverable: a machine-readable and human-readable roadmap health view.

Implementation scope:

- Add `brigade roadmap audit`.
- Parse `ROADMAP.md` headings and bullets.
- Classify bullets as `implemented`, `started`, `current`, `planned`, or `unclear`.
- Emit text and stable JSON.
- Warn on stale "Current Phase" or "Next Phase" sections that are already mostly implemented.
- Warn on roadmap items that name commands not present in the CLI.
- Warn on commands present in docs but missing from roadmap.
- Route roadmap hygiene issues into the existing work inbox as `source: roadmap-audit`.

Acceptance:

- Tests cover status classification from roadmap fixtures.
- Tests cover command mismatch detection.
- Tests cover JSON output stability.
- Tests cover inbox imports with stable fingerprints and dismissed-until-changed behavior.

Phase 35 status:

- Implemented command surface: `brigade roadmap audit` with text output, JSON output, stale phase checks, command drift checks, and `--import-issues`.
- Deferred: deeper roadmap ownership modeling. Reason: sections 1 through 4 prioritize the data model, JSON output, tests, and daily-loop health before richer roadmap workflow state.

### 2. Repository Fleet Readiness

Deliverable: inspect local repos as a fleet and turn setup gaps into reviewable work.

Implementation scope:

- Add gitignored `.brigade/repos.toml`.
- Add `brigade repos init/list/show/scan/doctor/import-issues`.
- Add `brigade repos sweep plan/run/runs/show/closeout`.
- Add `brigade repos report plan/build/list/show/archive/closeout`.
- Add `brigade repos actions plan/build/list/show/start/done/defer/archive`.
- Add `brigade repos actions dispatch plan/apply`, `dispatch --all-reviewed`, `reconcile`, and `context plan/build`.
- Discover repos under configured roots such as `~/repos`.
- Record safe repo metadata only: repo path label, branch, dirty counts, presence of `AGENTS.md`, `CLAUDE.md`, `ROADMAP.md`, README, CHANGELOG, test hints, handoff inboxes, publish-guard hooks, Brigade config, latest release readiness receipt, latest release candidate, and latest work closeout.
- Record safe per-repo Brigade state for local rollups: latest operator report, action queue health, pending task and import counts, review finding counts, handoff draft counts, security issue counts, scanner sweep status, release readiness, release candidates, work closeouts, and dirty tracked counts.
- Detect missing or stale operator setup:
  - missing repo-local guidance
  - fallback `CLAUDE.md` present without equivalent `AGENTS.md`
  - missing handoff inbox
  - handoff inbox not covered by configured source list
  - missing publish-guard hook where public release checks are expected
  - stale roadmap with no status markers
  - dirty tracked files older than threshold
  - missing test command hint
  - missing Brigade bootstrap where opted in
- Import issues into the scanner inbox as `source: repo-fleet`.
- Refresh safe local evidence explicitly through fleet sweeps that run configured read/report commands, write local receipts, and feed reports and action queues without cloning, fixing, promoting, or mutating remotes.
- Route reviewed fleet actions into target repo work imports and reconcile target repo progress back into the fleet queue without automatic promotion, work execution, fixes, cloning, or remote mutation.
- Coordinate local fleet release trains from safe per-repo release readiness, candidate, verification, review, security, operator, and fleet action evidence without pushing, tagging, publishing, or mutating remotes.
- Turn reviewed fleet release trains into local release action queues and record manual publish evidence without executing verification, tag, push, release, or remote-mutating commands.
- Reconcile fleet release train actions against manual evidence records and include summary counts in train closeout.
- Add fleet release train report bundles, manual evidence checklists, hygiene checks, unresolved-evidence work imports, and a local manual-publish ready gate.

Acceptance:

- Tests cover config init/list/show/scan/doctor text and JSON.
- Tests cover repo fixtures with AGENTS, CLAUDE fallback, roadmap, publish-guard, handoff inboxes, and dirty state.
- Tests prove private file contents are not copied into imports or docs.
- Tests cover repo-fleet imports, dedupe, and dismissed-until-changed behavior.
- Tests cover fleet sweep plan/run/runs/show/closeout text and JSON, filtering, stale-only selection, failed-repo isolation, safe log labels, and daily-loop integration.
- Tests cover fleet report plan/build/list/show/archive and fleet action plan/build/list/show/start/done/defer/archive text and JSON.
- Tests cover fleet action dispatch, idempotency, dismissed-until-changed behavior, changed-fingerprint superseding, action context packs, reconciliation states, and daily-loop integration.
- Tests cover fleet release train plan/build/list/show/compare/closeout/archive text and JSON, per-repo classifications, bundle evidence, manual-only publish plans, compare warnings, closeout states, daily-loop integration, and release-doctor integration.
- Tests cover fleet release train action plan/build/list/show/start/done/defer/archive and manual release evidence plan/record/list/show, including health integration and no command execution.
- Tests cover release train reconcile and summary for complete, skipped, deferred, blocked, and missing evidence states.
- Tests cover release train report, checklist, hygiene, import-issues, and ready gate behavior without remote mutation.
- Tests cover release train waivers, activity, manifests, audits, waiver-aware ready behavior, and revocation without remote mutation.
- Tests cover release waiver expiry, renewal, doctor checks, import routing, stale review warnings, and ready/audit visibility.
- Tests prove private repo names, owner names, org names, local paths, and raw evidence are not copied into public docs, fixtures, imports, handoffs, release evidence, or committed diffs.

Phase 35 status:

- Implemented command surface: `brigade repos init/list/show/scan/doctor/import-issues`.
- Implemented safe metadata only: repo id, label, path label, branch, dirty counts, guidance-file presence, docs presence, test hints, handoff inbox presence, publish-guard hook presence, Brigade config presence, and local receipt references.
- Deferred: recursive root discovery beyond configured entries. Reason: explicit config avoids accidentally exposing private repo names or paths in this phase.

Phase 40 status:

- Implemented command surface: `brigade repos report plan/build/list/show/archive/closeout`.
- Implemented command surface: `brigade repos actions plan/build/list/show/start/done/defer/archive`.
- Fleet reports write local `FLEET_REPORT.md` and `FLEET_EVIDENCE.json` bundles under `.brigade/repos/reports/` with safe repo ids, labels, counts, statuses, receipt labels, warnings, blockers, and suggested next commands.
- Fleet actions write local queues under `.brigade/repos/actions/`, require a reviewed or deferred fleet report unless explicitly overridden, dedupe by repo id plus report/source fingerprint, and update action metadata only.
- Center status, center reviews, work brief, work doctor, and release doctor surface fleet report and fleet action health.

Phase 41 status:

- Implemented command surface: `brigade repos sweep plan/run/runs/show/closeout`.
- Fleet sweeps run explicit local read/report commands in configured repos, write gitignored receipts under `.brigade/repos/sweeps/`, store raw logs locally, and expose safe repo ids, labels, command labels, status counts, log labels, and receipt labels only.
- Fleet reports, center status, center reviews, work brief, work doctor, and release doctor surface stale, failed, or unclosed fleet sweep health.

Phase 42 status:

- Implemented command surface: `brigade repos actions dispatch plan/apply`, `dispatch --all-reviewed`, `reconcile`, and `context plan/build`.
- Reviewed fleet actions can be dispatched into target repo `repo-fleet` work imports with acceptance criteria, source fingerprints, and fleet provenance. Dispatch is idempotent, respects dismissed imports until material change, and supersedes older dispatch imports when the source fingerprint changes.
- Action-scoped context packs are written in the target repo under `.brigade/context/packs/` with safe summaries, guidance presence, receipt labels, dispatch state, and explicit private-evidence exclusions.
- Reconciliation reads target repo imports, tasks, work closeouts, release readiness, and operator reports, then records `dispatched`, `in-progress`, `completed`, `dismissed`, `superseded`, `stale`, or `broken-reference` state on the fleet action.

Phase 43 status:

- Implemented command surface: `brigade repos release plan/build/list/show/compare/closeout/archive`.
- Fleet release trains write local `FLEET_RELEASE_TRAIN.md`, `FLEET_RELEASE_EVIDENCE.json`, and `MANUAL_PUBLISH_PLAN.md` bundles under `.brigade/repos/releases/`.
- Release train evidence classifies configured repos as `ready`, `blocked`, `needs-review`, `needs-dispatch`, `in-progress`, `stale-evidence`, `no-release-candidate`, or `deferred`, using safe repo ids, labels, counts, fingerprints, receipt labels, and suggested next commands only.
- Compare detects changed repo HEAD labels, newer release readiness, newer release candidates, changed fleet action reconciliation, missing safe receipt ids, and unresolved state changes.
- Closeout records `reviewed`, `deferred`, `superseded`, or `archived` state. Repo doctor, center status, center reviews, work brief, work doctor, and release doctor surface blocked, stale, or unclosed release train health.

Phase 44 status:

- Implemented command surface: `brigade repos release actions plan/build/list/show/start/done/defer/archive`.
- Implemented command surface: `brigade repos release evidence plan/record/list/show`.
- Release train actions are local metadata records under `.brigade/repos/releases/actions.json`, created from reviewed or deferred train repos that are not ready.
- Manual publish evidence is recorded under `.brigade/repos/releases/evidence.jsonl` for verification, release doctor, candidate compare, tag, push, release, and other manual steps.
- Repo doctor, center status, center reviews, work brief, work doctor, and release doctor surface open train actions and blocked manual evidence records without executing any publish or verification command.

Phase 45 status:

- Implemented command surface: `brigade repos release reconcile <train-id|latest>`.
- Implemented command surface: `brigade repos release summary <train-id|latest>`.
- Reconciliation marks release-train actions done when required manual evidence is completed, skipped, or deferred, and keeps actions open when evidence is missing or blocked.
- Release summaries report per-repo evidence status, missing evidence, blocked evidence, unresolved action counts, and suggested next commands.
- Release train closeout now includes reconciliation summary counts when available.

Phase 46-50 status:

- Implemented command surface: `brigade repos release report <train-id|latest>`.
- Implemented command surface: `brigade repos release import-issues <train-id|latest>`.
- Implemented command surface: `brigade repos release checklist <train-id|latest>`.
- Implemented command surface: `brigade repos release hygiene`.
- Implemented command surface: `brigade repos release ready <train-id|latest>`.
- Release reports write `RELEASE_TRAIN_REPORT.md` and `RELEASE_TRAIN_REPORT.json` into the local train bundle. Import routing uses `source: repo-fleet-release` and preserves source fingerprints. The ready gate remains local and fails on blocked repos, unresolved actions, missing evidence, or blocked evidence.

Phase 51-55 status:

- Implemented command surface: `brigade repos release waivers record/list/show/revoke`.
- Implemented command surface: `brigade repos release activity <train-id|latest>`.
- Implemented command surface: `brigade repos release manifest <train-id|latest>`.
- Implemented command surface: `brigade repos release audit <train-id|latest>`.
- Release waivers are local records for `blocked-repo`, `unresolved-action`, `missing-evidence`, and `blocked-evidence` scopes. The ready gate reports active waivers and can pass when remaining blockers are explicitly waived without hiding the underlying counts.
- Release activity gives one chronological local ledger across train creation, closeout, train actions, manual evidence, waivers, reports, and manifests. Manifests record bundle file labels and fingerprints. Audits report missing bundle files, stale manifests, open actions, blocked repos, and unresolved manual evidence.

Phase 56-60 status:

- Implemented command surface: `brigade repos release waivers renew <waiver-id>`.
- Implemented command surface: `brigade repos release waivers doctor <train-id|latest>`.
- Implemented command surface: `brigade repos release waivers import-issues <train-id|latest>`.
- Release waivers can now carry `expires_at`. Expired waivers no longer satisfy `brigade repos release ready`.
- Waiver doctor reports expired active waivers, waivers missing expiry, stale waiver reviews, and waivers tied to an older train fingerprint. Waiver import routing creates `source: repo-fleet-release-waiver` tasks with stable fingerprints and dismissed-until-changed behavior.
- Release ready and release audit include waiver health issues so reviewed risk remains visible even when an active waiver allows readiness to pass.

Phase 61 status:

- Implemented `docs/phase-61-100-plan.md` as the public phase queue for roadmap completion hardening.
- Tightened `brigade roadmap audit` command discovery so it scans command snippets, ignores prose, and normalizes parent commands and parameterized examples to known CLI command paths.
- Closed the context-engineering pattern registry owner and test-hint gap.

Phase 62 status:

- Added public-safe deferred roadmap ownership records to `brigade roadmap audit --json`.
- Each known deferred item now has an id, title, subsystem, owner, source section, deferred reason, status, and suggested phase when the item remains in scope.
- Text audit output includes the deferred item count, while unresolved ownership or missing phase metadata would become roadmap audit warnings.

Phase 63 status:

- Implemented command surface: `brigade roadmap commands`.
- Added text and JSON output for parser-derived command groups, normalized documented command snippets, documentation coverage, and missing top-level public docs.
- README documents the roadmap command contract surface.

Phase 99 status:

- Added `brigade roadmap commands --write` to generate `docs/command-inventory.md` from the CLI parser.
- Added `brigade roadmap commands --check` and roadmap audit integration for stale or missing command inventory warnings.
- The generated inventory contains public command paths only, not local paths, private repo names, or source evidence.

### 3. Inspiration Pattern Registry

Deliverable: a bounded local record of external workflow patterns Brigade intends to support.

Implementation scope:

- Add `docs/inspiration-patterns.md`.
- Add optional local fixtures for pattern families without copying private project contents.
- Define pattern families:
  - slash command
  - skill
  - subagent
  - MCP server
  - portable tool projection
  - runtime supervisor
  - approval queue
  - checkpoint and resume
  - code review producer
  - content guard gate
  - memory handoff
  - self-learning scanner
  - role-based delivery review
  - skillpack
  - memory eval replay
  - release learning closeout
  - context pack
  - context sync receipt
  - skill/plugin sync
  - agent security guardrail
- Map each pattern family to an existing or planned Brigade subsystem.
- Add `brigade roadmap patterns` or include pattern coverage in `brigade roadmap audit`.
- Emit text and stable JSON coverage:
  - pattern id
  - source family: command harness, delivery loop, durable memory, portable skill, agent security, context engineering, sync tool, Brigade-native, or other
  - Brigade subsystem
  - status: implemented, started, planned, deferred
  - acceptance owner doc
- Warn when a pattern family has no Brigade owner or no tests.
- Include a source-pattern coverage table for external inspirations and local side tools:
  - source alias or pattern family
  - pattern extracted
  - Brigade subsystem owner
  - decision: bake-in, integrate, catalog-only, move-candidate, or leave-alone
  - reason
  - public/private evidence boundary

Acceptance:

- Tests cover pattern registry parsing and JSON output.
- Tests cover missing-owner and missing-test warnings.
- Tests prove no raw private reference content is copied into public fixtures.
- Tests cover pattern coverage for command harness, delivery-loop, durable-memory, portable-skill, agent-security, context-engineering, and cross-harness sync families without public reference repo names.
- Tests cover source-project decision records for bake-in, integrate, catalog-only, move-candidate, and leave-alone.
- README, ROADMAP, and this plan document explain the pattern coverage boundary.

Phase 35 status:

- Implemented command surface: `brigade roadmap patterns`.
- Implemented public docs: `docs/inspiration-patterns.md` documents neutral pattern families, decision types, and the public/private evidence boundary.
- Deferred: loading private pattern source aliases from local config. Reason: exact reference names belong only in gitignored host-local config, and sections 1 through 4 require public-safe registry output first.

### 4. Scanner And Inbox Closure

Deliverable: finish the scanner-ready inbox as a complete reviewed daily loop.

Implementation scope:

- Tighten `work inbox doctor` coverage for stale, noisy, missing-provenance, cross-producer provenance contract gaps, broken-promoted, changed-dismissed, and no-import scanner runs.
- Add explicit review state to sweep reviews: pending, reviewed, archived.
- Add `brigade work sweep closeout <sweep-id|latest>` to record that the operator reviewed, dismissed, promoted, or intentionally deferred all actionable imports from a sweep.
- Make `work brief` prefer unclosed sweep reviews before suggesting new sweeps.
- Ensure scanner-health, backup-health, memory-care, security-scan, code-review, tool-catalog, and repo-fleet imports all use the same provenance and fingerprint path.

Acceptance:

- Tests cover sweep closeout clean and blocked cases.
- Tests cover work brief ordering: manual queued task, unclosed sweep, top scanner import, due scanner.
- Tests cover scanner import provenance across all local producer sources.

Phase 35 status:

- Implemented command surface: `brigade work sweep closeout <sweep-id|latest>`.
- Implemented closeout states for reviewed sweeps, deferred pending imports, blocked unresolved pending imports, and missing import references.
- Implemented inbox hygiene for unclosed sweep reports in addition to existing missing provenance, broken references, stale pending imports, changed dismissed fingerprints, and noisy source checks.
- Deferred: cross-producer provenance audits across every historical source. Reason: this phase tightens the common path and leaves historical backfill for a later compatibility cleanup.

### 5. Chat Surface Export Completion

Deliverable: finish export-based chat sweep ingestion without live product APIs.

Implementation scope:

- Complete provider registry docs and fixtures for common chat export families and generic JSONL.
- Implemented provider alias normalization for common export labels and generic JSONL, with scanner sweep review, task promotion, and handoff promotion coverage.
- Add optional provider-family aliases for the longer surface list from the roadmap without implementing live adapters.
- Improve privacy checks for raw transcript fields, user ids, channel ids, private URLs, hostnames, tokens, and unbounded excerpts.
- Ensure chat-surface producer output works through scanner sweep, sweep review, task promotion, handoff promotion, and release closeout evidence.

Acceptance:

- Tests cover each provider fixture through validate, ingest, import, inbox, sweep review, and handoff or task promotion.
- Tests prove raw private chat text is rejected or redacted from imports, session artifacts, handoffs, docs, and release evidence.
- Implemented shared privacy regression fixtures across chat, backup, security, repo-fleet, context, learning, and release candidate paths.

Phase 36 status:

- Existing export-based chat surface commands remain the active implementation path: `brigade chat sweep validate/ingest/import-issues`.
- Deferred: new live chat adapters and expanded provider-specific parsers. Reason: the phase kept the no-live-API boundary and focused on local operator closure.

### 6. Backup And Recovery Closure

Deliverable: make backup health fully reviewable in daily operations.

Implementation scope:

- Add backup health closeout or review status so repeated known issues do not keep the daily brief noisy.
- Add restore rehearsal evidence summaries and stale rehearsal warnings to release readiness.
- Add optional safe operator status summary output for backup health without sending messages.
- Keep all destinations labeled and private.

Acceptance:

- Tests cover backup review closeout and daily brief quieting.
- Tests cover release readiness including backup risk as warning or blocker by policy.
- Tests prove hostnames, remotes, mount paths, webhook URLs, and passwords are not copied.

Phase 36 status:

- Implemented command surface: `brigade work backup closeout`.
- Implemented local closeout receipts under `.brigade/backups/closeouts/` using issue fingerprints and safe summaries only.
- Strengthened backup health with raw, active, quieted, changed-fingerprint, and restore rehearsal issue counts plus safe operator summaries in backup status, daily brief, release readiness, and release candidate evidence.
- Deferred: outbound operator status messages. Reason: notifications are out of scope and would require product-specific surfaces.

### 7. Shared Tool Catalog Completion

Deliverable: make portable tools useful across harnesses while keeping execution explicit.

Implementation scope:

- Add catalog export bundles for reviewed tools:
  - portable source summary
  - projection status by harness
  - contract schemas
  - policy requirements
  - runtime requirements
  - approval and run history
- Add parity report closeout for tool projections across configured harness targets and scripts.
- Add `brigade tools pack build/show/list/archive` for local portable tool packs.
- Add `brigade tools sync plan` and `brigade tools sync apply` as a thin reviewed layer over existing projections:
  - dry-run by default
  - add-only unless `--force` is passed
  - no delete unless a future explicit command is added
  - managed metadata required before updates
  - preserve local harness edits as conflicts
- Keep pack output local unless explicitly copied by the operator.

Acceptance:

- Tests cover pack build/show/list/archive text and JSON.
- Tests cover sync plan/apply dry-run, add-only behavior, managed metadata, and conflict refusal.
- Tests cover projections, contracts, policy, runtime, approvals, run history, and checkpoints represented in pack evidence.
- Tests cover parity closeout and work brief quieting.

Phase 36 status:

- Implemented command surface: `brigade tools pack build/list/show/archive`.
- Implemented command surface: `brigade tools sync plan` and `brigade tools sync apply`.
- Implemented command surface: `brigade tools parity status/closeout`.
- Tool packs summarize catalog entries, projections, policy, runtimes, call approvals, run history, checkpoints, and catalog issues.
- Tool parity closeouts write local receipts under `.brigade/tools/parity-closeouts/`, quiet unchanged reviewed or deferred missing, stale, unmanaged, conflicted, and parity-gap projection issues, and resurface changed projection fingerprints through doctor, brief, and import routing.
- Release readiness and release candidate evidence include latest tool pack health, stale pack warnings, parity closeout state, sync-plan blockers, approval counts, run history counts, and checkpoint state without applying projections or running tools.

### 8. Context Engineering Packs

Deliverable: make repo and task context explicit, reviewable, and portable across harnesses.

Implementation scope:

- Add `brigade context plan`.
- Add `brigade context build`.
- Add `brigade context list/show/archive`.
- Build local context packs under `.brigade/context/packs/`.
- Context pack contents:
  - task or release target
  - relevant docs
  - AGENTS and CLAUDE guidance summary
  - active task acceptance criteria
  - recent work closeout
  - recent review and security findings
  - selected tool catalog entries
  - excluded private/raw evidence summary
- Add context sync planning to configured harness destinations without writing by default.
- Keep raw private chat, secrets, private infrastructure values, and full local logs out of context packs unless explicitly allowed by local policy.

Acceptance:

- Tests cover context plan/build/list/show/archive text and JSON.
- Tests cover context packs for task, repo, release, and tool-use scenarios.
- Tests cover stale context warnings and missing source references.
- Tests prove raw private evidence is excluded by default.

Phase 36 status:

- Implemented command surface: `brigade context plan/build/list/show/archive`.
- Implemented command surface: `brigade context sync plan/record`.
- Context packs are written under `.brigade/context/packs/` and include task acceptance, doc and guidance summaries, selected tool references, recent work/security/review summaries, and an explicit private-evidence exclusion list.
- Context pack doc and guidance summaries use presence and line-count metadata instead of copying raw file contents.
- Context sync planning reads `.brigade/context/sync-targets.json`, reports missing/current/stale/conflicted destinations, stale pack age, and missing source references, and writes local sync-plan receipts under `.brigade/context/sync-plans/`.
- Context doctor and import routing report stale packs, missing source references, stale task acceptance, stale tool references, and sync blockers, then route active issues into the work inbox as `source: context-pack` tasks.
- Deferred: writing context packs into harness destinations. Reason: sync planning remains read-only until a future explicit context apply command exists.

### 9. Side Project Consolidation And Org-Move Planning

Deliverable: decide which related projects become Brigade features, Brigade integrations, catalog-only tools, or organization move candidates.

Implementation scope:

- Add `brigade projects audit`.
- Add `brigade projects readiness plan/record/list/show`.
- Add `brigade projects closeout/closeouts/closeout-show`.
- Add optional gitignored `.brigade/projects.toml`.
- Inspect configured local or public project records without cloning by default.
- For each project, classify:
  - `bake-in`: small workflow primitive belongs directly in Brigade
  - `integrate`: external tool keeps its repo but Brigade reads receipts or imports findings
  - `catalog-only`: tool belongs in the portable tool catalog or MCP catalog
  - `move-candidate`: side repo may belong under an organization
  - `leave-alone`: unrelated or product-owned
- Initial classification guidance:
  - publish guards: integrate as publish gates and release evidence, do not bake in scanner engines
  - memory maintenance tools: bake in compatible handoff/memory-care checks where small, integrate larger repair reports
  - bootstrap maintenance tools: bake in bootstrap budget and oversized-prefix checks
  - output compaction tools: integrate as optional output compaction and run-history scanner, catalog executable
  - MCP runner tools: integrate as MCP/tool execution inspiration and catalog bridge, do not replace them
  - MCP server family: catalog-only plus release/readiness templates, potential organization move candidates when public and reusable
  - prompt library tools: integrate as prompt/skill source catalog and scanner producer
  - code search tools: integrate as optional local scanner and context pack source
  - usage tracking tools: integrate as token budget and run-cost scanner producer
  - notification tools: catalog-only until notifications become explicit
  - domain-specific ops and security products: leave as product repos unless Brigade needs their health receipts
- Produce a local migration plan:
  - source owner
  - recommended owner
  - reason
  - required docs/license/security/release/ownership readiness before move
  - migration blockers
  - manual commands only, not executed
- Route stale or missing project-readiness work into the scanner inbox as `source: project-consolidation`.
- Close out reviewed, deferred, superseded, or archived project move plans locally, quiet unchanged issues, and resurface changed fingerprints.

Acceptance:

- Tests cover project audit text and JSON.
- Tests cover project readiness receipt text and JSON for all project decisions.
- Tests cover reviewed, deferred, superseded, archived, and changed-fingerprint closeout states.
- Tests cover classification rules for bake-in, integrate, catalog-only, move-candidate, and leave-alone.
- Tests cover migration plan generation with manual-only commands.
- Tests prove no GitHub transfer, archive, visibility, or remote mutation occurs.
- Tests cover project-consolidation imports with dedupe and dismissed-until-changed behavior.

Phase 36 status:

- Implemented command surface: `brigade projects audit` and `brigade projects import-issues`.
- Added gitignored `.brigade/projects.toml` contract for safe labels, decisions, readiness flags, and manual-only migration plans.
- Added `brigade projects readiness plan/record/list/show` so local receipts capture docs, license, security, release, ownership, and migration-blocker readiness without running any remote mutation.
- Added `brigade projects closeout/closeouts/closeout-show` so reviewed, deferred, or archived unchanged move-plan issues can quiet daily noise while changed fingerprints route back through `project-consolidation` imports.
- Exact project names and owner names remain local config concerns, not public docs or imports.

### 10. Self-Learning Loop Closure

Deliverable: make self-learning explicit, bounded, and reviewable.

Implementation scope:

- Add a local learning inbox view over durable findings from:
  - scanner imports
  - code review findings
  - security findings
  - failed tool runs
  - checkpoint rejections
  - handoff ingest failures
  - memory-care refresh candidates
- Add `brigade learn plan` and `brigade learn doctor` as read-only summary commands.
- Add `brigade learn import-issues` to route stale or blocked learning candidates into the scanner inbox.
- Add `brigade learn closeout/closeouts/closeout-show` for accepted-risk, dismissed, archived, and deferred learning outcomes.
- Add durable-memory inspired eval replay receipts for learning changes:
  - export a safe local learning scenario
  - replay after code or rule changes
  - record before/after outcome summaries
  - compare before/after replay counts and surface regressions
  - do not upload or publish private examples
- Make every learning candidate choose one review path:
  - task
  - Memory Handoff draft
  - suppression or accepted risk
  - archive or dismissal
- Add release evidence summaries for unresolved learning candidates.
- Do not edit canonical memory, source files, tool configs, or policies automatically.

Acceptance:

- Tests cover learning candidate aggregation from fixtures across scanners, review, security, tools, handoffs, and memory-care.
- Tests cover source-aware learning closeout quieting and changed-fingerprint resurfacing across scanner, security, review, tool, handoff, memory-care, backup, and release candidates.
- Tests cover safe eval replay receipts without private raw evidence.
- Tests cover replay compare receipts, redaction, release evidence, and operator-center review surfacing.
- Tests cover JSON output and inbox imports.
- Tests cover dismissal and accepted-risk quieting.
- Tests prove no automatic memory edits or source edits happen.

Phase 36 status:

- Implemented command surface: `brigade learn plan`, `brigade learn doctor`, and `brigade learn import-issues`.
- Added `brigade learn closeout/closeouts/closeout-show` so accepted-risk, dismissed, archived, and deferred candidates are quieted until their source fingerprint changes.
- Learning candidates aggregate from pending scanner imports, failed review receipts, and failed portable tool run receipts.
- Added `brigade learn replay export/list/show/compare` for redacted local before/after replay receipts and compare receipts that do not mutate memory, source, policy, or remote state.
- Deferred: rich accepted-risk quieting across every source subsystem. Reason: candidate import routing exists first, while source-specific closeout policies remain subsystem-owned.

### 11. Security Plugin Closure

Deliverable: complete security evidence packs and policy-driven local gates.

Implementation scope:

- Add security report bundles that include JSON, Markdown, and optional HTML-safe index without new dependencies.
- Add SARIF output if it can be implemented dependency-free.
- Add prompt and instruction rule fixtures for repo AGENTS, CLAUDE, skills, slash commands, subagents, and tool wrappers.
- Add security review closeout so suppressions, accepted risk, and completed follow-up tasks are visible in release readiness.
- Add policy-pack closeout evidence for personal, public-repo, CI, and strict scan modes.
- Tighten runtime-confidence handling for public templates versus active configs.
- Add agent-security inspired guardrail reporting:
  - tool permission risk
  - prompt injection risk
  - secret and private endpoint risk
  - unsafe hook or auto-run risk
  - harness config risk
  - remediation task routing

Acceptance:

- Tests cover prompt-injection style instruction findings.
- Tests cover agent-security guardrail categories.
- Tests cover repo guidance, skill, slash-command, subagent, and tool-wrapper surfaces with template confidence handling.
- Tests cover SARIF or explicitly document why SARIF is deferred.
- Tests cover security closeout evidence in release readiness and release candidates.
- Tests cover policy-specific blockers, warnings, suppressions, accepted risk, release readiness, and release candidates.
- Existing publish-guard integration still passes.

Phase 36 status:

- Implemented command surface: `brigade security closeout`.
- Security closeouts write local receipts under `.brigade/security/closeouts/` with finding ids, fingerprints, status, suppressions, and accepted-risk state.
- Added dependency-free SARIF 2.1.0 output through `brigade security sarif` and scan bundle generation.
- Added guardrail fixtures and surface labels for repo guidance, skills, slash commands, subagents, and tool wrappers, including prompt-injection and environment-exfiltration patterns.
- Added `ci` policy preset and policy-pack closeout evidence for blocker counts, warning counts, accepted risk, release readiness, and release candidate packets.

### 12. Issue And TDD Loop Closure

Deliverable: close the daily task lifecycle from issue or scanner import to review and release evidence.

Implementation scope:

- Add repo-shareable workflow rule templates without embedding personal preferences. Status: implemented with public-safe install templates under `rules/` and work doctor visibility.
- Add stale active issue context checks and repair imports. Status: implemented with `brigade work import issue-repairs` for missing context, unavailable `gh`, failed checks, stale context, and closed remote issues.
- Add acceptance coverage summaries across pending tasks, completed tasks, review findings, and release closeout. Status: implemented with hardened `brigade work acceptance` rollups.
- Add task outcome rollups in release candidate evidence. Status: started with acceptance coverage, completion gaps, review finding outcomes, and work closeout state in release readiness and candidate evidence.

Acceptance:

- Tests cover stale issue context and repair imports.
- Tests cover shareable workflow rule templates.
- Tests cover acceptance coverage rollups in release evidence.

Phase 36 status:

- Implemented command surface: `brigade work acceptance`.
- Acceptance summaries report pending tasks with acceptance, pending tasks missing acceptance, completed tasks with completion metadata, completed tasks missing completion metadata, completion-time acceptance gaps, review-finding outcomes, and latest work closeout state.
- Deferred: none for the initial issue and TDD loop closure. The local acceptance rollup, shareable workflow rule templates, and issue repair imports are complete.

### 13. Memory And Handoff Closure

Deliverable: make memory handoff and memory-care review quiet, explicit, and auditable.

Implementation scope:

- Track reviewed dates, freshness dates, confidence metadata, and evidence metadata without editing memory cards. Status: started with memory-care metadata coverage summaries, reviewable imports for missing reviewed and freshness dates, and planning-only safe metadata repair output.

- Add handoff closeout views that explain why a draft is still pending, ingested, skipped, failed, reviewed, or archived.
- Harden handoff ingestor warning parsing for skipped, failed, malformed, unreachable-source, and no-reply states. Status: implemented in issue parsing and normalized reconcile receipts.
- Route source coverage drift into reviewed work imports with stable source fingerprints and dismissed-until-changed behavior. Status: implemented for uncovered writer inboxes and requested missing-inbox repairs.
- Add memory-care closeout for refresh candidates that are task-promoted, handoff-promoted, dismissed, or intentionally deferred.
- Add optional safe auto-fix planning only, no memory mutation, for low-risk metadata repair. Status: started with `brigade memory care plan-fixes`, import metadata, and daily brief visibility.

Acceptance:

- Tests cover handoff and memory-care closeout states.
- Tests cover no direct `MEMORY.md` or memory card edits.
- Tests cover daily brief quieting after closeout.

Phase 36 status:

- Implemented command surface: `brigade handoff closeout`.
- Implemented command surface: `brigade memory care closeout`.
- Handoff closeouts record draft ids, lint state, ingestion state, target card or document, source import references, and safe fingerprints.
- Memory-care closeouts record queue fingerprints and review or defer state without editing cards or `MEMORY.md`.

### 14. Release And Publish Gate Completion

Deliverable: turn all local evidence into a complete publish review packet, still without remote mutation.

Implementation scope:

- Extend release readiness and candidates to include roadmap audit, repo-fleet, project-consolidation state, context pack freshness, backup review, tool parity, security closeout, task acceptance rollup, and memory-care closeout.
- Add a wrapper-friendly release evidence schema manifest. Status: implemented with `brigade release schema` for readiness receipts, candidates, fleet trains, waivers, and manual evidence records.
- Add release candidate compare between latest candidate and current HEAD.
- Add release candidate provenance audit and import routing. Status: implemented with candidate audit checks for stale evidence, missing references, changed HEAD/docs/command contracts, privacy-boundary issues, and source `release-candidate` work imports.
- Add publish checklist templates for tag, push, GitHub release, package publish, and docs publish as manual-only steps.
- Add release candidate closeout status: draft, reviewed, superseded, archived.

Acceptance:

- Tests cover release readiness and candidate evidence for every completed local subsystem, including context packs.
- Tests cover candidate compare and closeout state.
- Tests prove no push, tag, release creation, upload, or PR mutation occurs.

Phase 36 status:

- Implemented command surface: `brigade release candidate compare`.
- Implemented command surface: `brigade release candidate closeout`.
- Candidate compare detects changed HEAD, missing referenced receipts, newer release, verification, review, scanner, or security receipts, and docs changed after candidate build.
- Candidate closeout writes local `CLOSEOUT.json` with draft, reviewed, superseded, or archived state. It does not push, tag, publish, or mutate remotes.

### 15. Local Operator Center

Deliverable: expose one read-only local control-plane style view over the existing Brigade state.

Implementation scope:

- Add `brigade center status`.
- Add `brigade center activity`.
- Add `brigade center reviews`.
- Add `brigade center templates`.
- Add `brigade center report plan/build/list/show/archive/review/compare/closeout`.
- Add `brigade center actions plan/build/list/show/start/done/defer/archive`.
- Aggregate existing local state only: work sessions, pending tasks, pending imports, scanner runs and sweeps, review runs, handoff drafts, tool approvals, checkpoints, context packs, learning candidates, repo fleet, project decisions, security health, release readiness, and release candidates.
- Every center item includes subsystem, local id, status, safe summary, suggested next command, and priority or severity when available.
- Keep center commands read-only and JSON-first for future wrappers.
- Build local report bundles only under `.brigade/center/reports/`, with Markdown, dependency-free HTML, and JSON evidence.
- Build reviewed daily action queues only under `.brigade/center/actions/`, without running suggested commands.

Acceptance:

- Tests cover center status, activity, reviews, and templates in text and JSON.
- Tests cover center report plan, build, list, show, archive, review, compare, closeout, freshness checks, and release/work integration.
- Tests cover center action queue plan, build, list, show, start, done, defer, archive, dedupe, reviewed-report gating, and release/work integration.
- Tests prove center commands are read-only.
- Public docs describe the operator center as local CLI output, not a hosted dashboard, app server, daemon, or sync engine.

Phase 36 status:

- Implemented command surface: `brigade center status/activity/reviews/templates`.
- Center status aggregates local subsystem health. Center activity reads receipts and pack metadata. Center reviews presents one pending review queue across imports, learning, projects, and context health. Center templates lists local workflow templates for wrappers.

Phase 37 status:

- Implemented command surface: `brigade center report plan/build/list/show/archive`.
- Operator reports write `OPERATOR_REPORT.md`, `OPERATOR_REPORT.html`, `CENTER_EVIDENCE.json`, and optional `CLOSEOUT.json` under `.brigade/center/reports/`.
- Work brief, work doctor, release doctor, and release candidate evidence include operator report freshness.

Phase 38 status:

- Implemented command surface: `brigade center report review/compare/closeout`.
- Report review groups pending items into an action plan with suggested commands.
- Report compare checks changed HEAD, missing receipts, newer local activity, newer subsystem receipts, and changed review queues.
- Report closeout stores reviewed, deferred, superseded, or archived metadata without taking actions on queued items.

Phase 39 status:

- Implemented command surface: `brigade center actions plan/build/list/show/start/done/defer/archive`.
- Action queues persist reviewed report action items under `.brigade/center/actions/`, dedupe repeated builds by report fingerprint and source item id, and require reviewed or deferred report closeout unless explicitly overridden.
- Action state changes update local metadata only. They do not promote imports, dismiss findings, execute tools, run scanners, run reviewers, or mutate remotes.
- Center status, center reviews, work brief, work doctor, and release doctor surface open action queue health.

Phase 101 status:

- Implemented command surface: `brigade daily status/plan/review/run/closeout`.
- `daily status` summarizes the current local operating state across work, imports, center reviews, daily actions, readiness, handoff drafts, memory-care, security, tools, release readiness, and operator reports.
- `daily plan` ranks pending accepted tasks, reviewed imports, center actions, readiness blockers, and stale handoff, memory, or security issues, chooses exactly one recommended safe action, and writes no state unless `--record` is passed.
- `daily review` previews the selected action, safe evidence references, acceptance criteria, risk, approval boundary, context pack plan, and likely next command.
- `daily run` handles one bounded local action with a receipt under `.brigade/daily/runs/`, refusing approval-required actions unless `--approved` is passed.
- `daily closeout` marks the latest run reviewed, deferred, blocked, or archived and can write a linted Memory Handoff draft without editing canonical memory.
- The daily driver is the agent-facing entrypoint over the operator system. It does not run arbitrary commands, start scanners or reviewers, run tools, run fleet sweeps, mutate remotes, push, tag, publish, or edit canonical memory.

Phase 102 status:

- Hardened the existing daily driver without adding a new subsystem.
- Added command surface: `brigade daily init/schema/history/show/doctor`.
- Added gitignored `.brigade/daily.toml` defaults for preferred mode, risk policy, context pack building, operator report building, readiness imports, import promotion, work runs, and stale receipt thresholds.
- Daily planning now respects task-first, inbox-first, and readiness-first modes without bypassing approval or remote-mutation guards.
- Daily review now shows the selected adapter, config blockers, evidence blockers, acceptance, risk, approval boundary, and context pack intent.
- Daily run now refuses disabled adapters, stale recorded plans, approval-required actions without `--approved`, and missing source evidence, while preserving clean JSON output from wrapped commands.
- Daily health now surfaces lightly in work brief, work doctor, center status, and center reviews.

Phase 104 status:

- Added local daily approval requests under `.brigade/daily/approvals/` for approval-required selected actions.
- Added command surface: `brigade daily approvals list/show/approve/reject/hold`.
- Added `brigade daily run --approval <approval-id>` so an approved, unconsumed request can resume the selected daily action without reassembling context.
- Approval requests preserve selected adapter, source evidence, acceptance criteria, risk, config snapshot, source fingerprint, and suggested next command.
- Daily run now creates or reuses a pending approval request when it blocks on approval, revalidates config and source fingerprints before approved runs, marks requests consumed after the run starts, and records the approval id in daily run receipts.
- Daily doctor, daily status, daily review, work brief, and center reviews surface pending, stale, held, rejected, approved, or changed-evidence approval requests.
- Approval commands are local review actions only. They do not execute selected actions, run arbitrary commands, mutate remotes, or edit canonical memory.

Phase 105-114 status:

- Hardened the daily driver as the wrapper-facing agent loop instead of adding a broad new subsystem.
- Normalized `daily run` adapter results for work task runs, import promotion, center action starts, readiness imports, operator report builds, and context pack builds.
- Daily planning now exposes ranked candidates, selection reasons, rejection reasons, safety blockers, approval blockers, stale evidence blockers, missing acceptance/provenance penalties, and noisy-source penalties.
- Added recovery command surface: `brigade daily resume`, `brigade daily repair`, and `brigade daily unblock`.
- Daily context packs now include selected daily action context and expanded excluded-private-evidence summaries.
- Daily closeout records verification expectations, latest verification, changed-file summaries, work closeout state, review closeout state, handoff state, and release-readiness impact.
- Added `brigade daily approvals compare <approval-id>` and `brigade daily approvals archive --consumed`.
- Added `brigade daily protocol` for the stable JSON-first external agent loop.
- Added local-only daily telemetry with `brigade daily telemetry` and `brigade daily telemetry doctor`.
- Release readiness and release candidate evidence now include latest daily plan, run, health, and telemetry state.
- Deferred: automatic execution inside `daily resume`. Reason: recovery remains explicit and local; `resume` returns the safe next command instead of running an approval or repair path implicitly.

Phase 115-164 status:

- Added `docs/phase-115-164-plan.md` as the source of truth for five production-hardening workstreams.
- Added command surface: `brigade daily hardening plan/audit/import-issues/closeout`.
- The hardening plan covers 50 phases across daily production hardening, operator-center contract cleanup, inbox evidence quality, repo-fleet daily use, and the self-dogfood release loop.
- The hardening audit checks daily adapter receipts, plan explainability, approval hygiene, telemetry, center schema/review contracts, inbox quality scoring, repo-fleet daily-use health, and release dogfood evidence.
- Release readiness and release candidates include compact hardening, center contract, inbox quality, repo-fleet daily-use, and release dogfood summaries.
- `daily hardening import-issues` routes audit findings into reviewed `source: daily-hardening` work imports with stable fingerprints and acceptance criteria.
- `daily hardening closeout` writes local reviewed, deferred, blocked, or archived closeout receipts under `.brigade/daily/hardening/`.
- Deferred: automatic hardening repair. Reason: this tranche keeps hardening explicit, local, and reviewable through imports and closeouts instead of applying fixes.

## Suggested Execution Order

1. Roadmap audit, inspiration pattern registry, and repo-fleet readiness.
2. Scanner inbox and sweep closeout.
3. Chat surface export completion.
4. Backup review closeout.
5. Tool catalog packs, skill/plugin sync, and parity closeout.
6. Context engineering packs.
7. Side project consolidation and org-move planning.
8. Self-learning loop closure.
9. Security report bundles and closeout.
10. Issue/TDD and acceptance rollups.
11. Memory/handoff closeout.
12. Release candidate final integration.

This order starts by making the remaining work visible, then closes noisy daily-loop surfaces, then adds final release integration.

## Stop Conditions

The implementation goal should stop when all of the following are true:

- Every command named in the selected sections exists or is explicitly documented as deferred.
- Every selected section has focused tests.
- README, CHANGELOG, ROADMAP, and relevant docs are updated.
- The selected verification commands pass.
- A Memory Handoff is written and linted for durable workflow changes.
- The work is committed and pushed if the active goal includes commit and push.

The goal should not continue by inventing new features after those conditions are met.

## Verification Baseline

Run at minimum:

```bash
PYTHONPATH=src python3 -m pytest tests/test_release_cmd.py tests/test_work_cmd.py tests/test_handoff_cmd.py -q
PYTHONPATH=src python3 -m pytest -q
git diff --check
```

Add narrower tests for each subsystem touched, for example:

```bash
PYTHONPATH=src python3 -m pytest tests/test_security_cmd.py -q
PYTHONPATH=src python3 -m pytest tests/test_tools_cmd.py -q
```

## Recommended Next Goal Prompt

```text
/goal Brigade phase 35: roadmap completion tranche one

Use docs/roadmap-completion-plan.md as the source of truth.

Implement sections 1 through 4 only:
- Roadmap State Audit And Closure Map
- Repository Fleet Readiness
- Inspiration Pattern Registry
- Scanner And Inbox Closure

Keep the boundaries and stop conditions from docs/roadmap-completion-plan.md.

Scope:
- Add roadmap audit commands and JSON output.
- Add repo fleet config, scan, doctor, and inbox imports.
- Add an inspiration pattern registry and coverage audit for command-harness patterns, delivery loops, durable memory/eval loops, portable skills, agent-security guardrails, context packs, cross-harness sync tools, relevant local side-project categories, MCP tooling, portable tools, security gates, self-learning, and release gates.
- Add scanner sweep closeout and tighter inbox hygiene integration.
- Update work brief and work doctor only as needed to surface the highest-priority unresolved roadmap, pattern coverage, repo-fleet, or sweep closeout issue.
- Preserve all existing scanner, release, handoff, review, security, backup, tool, and work-task behavior.

Out of scope:
- No live chat adapters.
- No daemon or scheduler mutation.
- No remote mutation.
- No automatic promotion.
- No automatic memory edits.
- No security SARIF, context pack, tool pack, or sync apply work yet.
- No implementation of other repos' product roadmaps.
- No copying raw private reference repo contents into Brigade docs or fixtures.
- No GitHub repo transfer, archive, visibility, or owner mutation.
- No new dependencies.

Acceptance:
- Tests cover roadmap audit parsing, command mismatch detection, JSON output, and roadmap-audit imports.
- Tests cover repo fleet init/list/show/scan/doctor/import-issues text and JSON.
- Tests cover AGENTS and CLAUDE fallback detection without copying private contents.
- Tests cover repo-fleet imports with stable fingerprints, dedupe, and dismissed-until-changed behavior.
- Tests cover inspiration pattern registry text and JSON, named source coverage, missing-owner warnings, and missing-test warnings.
- Tests cover sweep closeout clean, blocked, stale, and missing-reference states.
- Tests cover work brief, work doctor, and work inbox doctor integration.
- Existing release readiness and release candidate workflows still pass.
- Existing scanner, handoff, review, security, backup, tool, and work-task workflows still pass.
- README, CHANGELOG, ROADMAP, docs/import-schema.md, docs/scanner-registry.md, and this plan document are updated.
- A Memory Handoff is written and linted.
- PYTHONPATH=src python3 -m pytest tests/test_release_cmd.py tests/test_work_cmd.py tests/test_handoff_cmd.py -q passes.
- PYTHONPATH=src python3 -m pytest -q passes.
- Commit and push the phase when complete.
```
