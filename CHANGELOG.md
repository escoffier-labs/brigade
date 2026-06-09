# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- Quickstart and `brigade tools defaults` now scope built-in tool projections to the workspace's selected harnesses plus the neutral `scripts/` folder, so a `--harnesses codex` setup no longer writes folders for every supported harness.
- The workspace `AGENTS.md`, `INSTALL_FOR_AGENTS.md`, and `handoff-flow` card templates now render the selected writer handoff inboxes instead of hardcoding `.claude/memory-handoffs/`, and the missing-template fallback now points at `brigade handoff-template` instead of a host-private path.
- The Hermes adapter README now opens issues against the correct repository.

### Documentation
- Added a first-10-minutes guide and compact support response templates for install, quickstart, doctor, commit-scope, homegrown setup, and security questions after launch.

### Added
- Dogfood runs can now use configured `--agent-cli` adapters for Claude Code, Codex, OpenCode, Antigravity through `agy --print`, Pi through `pi -p`, Cursor through `cursor-agent -p`, Aider, Goose, Continue, GitHub Copilot CLI, Qwen Code, Kimi Code, AdaL, OpenHands, or `ollama:<model>` instead of being hardwired to Codex.

## [0.8.1] - 2026-06-08

### Added
- Operator adoption migration rollup: `brigade operator migration status/doctor/import-issues/consolidate` summarizes redacted adoption progress across operator config, external surfaces, review receipts, pending imports, and pending tasks, then lets current migration rollups supersede tiny record-level follow-ups without exposing raw scheduler or process details.
- Redacted operator surface registry: `brigade operator surfaces capture/list/doctor/review/reviews/import-issues` captures shell crontab, OpenClaw cron, and PM2 coverage as counts, status totals, ordinal labels, review decisions, and fingerprints under `.brigade/operator/surfaces/`, omitting raw cron lines, job names, process names, command paths, host details, and environment values.
- Existing-operator adoption loop: `brigade operator adopt plan/capture/import-issues` inventories guidance files, harness roots, handoff inboxes, local state folders, and count-level external scheduler/process surfaces before changing a homegrown operator workspace.
- Research handoff export: `brigade research export-handoff`, `brigade research handoffs doctor`, and `brigade research handoffs import-issues` route completed research runs into selected writer harness inboxes as linted Memory Handoffs, track export fingerprints, and surface missing or stale exports without ingesting memory.
- Operator notification visibility: new `brigade notifications status` and `brigade notifications setup plan` commands inspect optional `agent-notify` wiring and print reviewed Codex/Claude hook snippets without sending messages, editing hook files, or storing channel secrets. Notification health now appears in `brigade doctor`, `brigade center status`, `brigade work brief`, and `brigade daily status/plan` as an advisory setup item.
- Internal dogfood bootstrap: `brigade operator init --profile internal-dogfood` writes repo-local production dogfood config defaults and refreshes read-only security evidence, while `brigade operator status --profile internal-dogfood` reports machine-vs-repo wiring, gitignore state, dogfood readiness, daily health, security evidence, notification config, and local readiness.
- Internal dogfood guidance: `brigade operator guide`, `docs/internal-dogfood.md`, and the workspace `AGENTS.md` template now document the explicit Brigade loop, repo onboarding command, handoff expectations, and no-daemon/no-remote-mutation boundaries.
- Cross-harness operator tool sync: `brigade operator sync-tools` projects tracked `tools/*.md` sources into local Claude, Codex, and OpenCode folders while keeping generated harness files ignored.
- Portable workspace bootstrap: `brigade operator bootstrap-portable` imports optional tool and skill packs, merges built-in portable tools, writes missing built-in source files, projects managed tool outputs across local harness folders, and reports tool plus skill health.
- Security scanner secret response guidance: secret findings now include redacted response options for `.env` or environment storage, scrub/rotate, KeePass review, and session transcript redaction. Session and chat transcript paths are classified as `surface: session-chat` for exposed API keys, tokens, passwords, and private keys.
- Reviewed self-learning skill proposals: `brigade learn skill-candidates` groups repeated learning evidence into reusable skill candidates, and `brigade learn propose-skill <candidate-id>` writes an unreviewed generated skill source plus a normal skill inbox proposal without accepting, installing, publishing, or editing memory automatically.
- New-user quickstart: `brigade operator quickstart` runs the repo template install, local operator config, portable tool and skill bootstrap, and selected harness checks as one local-only first-user flow, with `--dry-run` support.
- New-user issue support: quickstart JSON now includes a compact `issue_report`, the README links a first-run troubleshooting guide, and GitHub issue templates use Brigade naming with a dedicated quickstart setup form.
- Learning skill proposal usability: `brigade learn skill-candidates --source <source>` filters repeated evidence by producer, security scan imports preserve safe grouping metadata and response options, and `brigade learn propose-skill --dry-run` previews generated source plus inbox writes before creating a proposal.
- Compact operator readiness verdict: `brigade operator doctor --profile internal-dogfood` prints a short ready/not-ready summary, blocker count, next command, and local tracked-vs-generated reminders.
- Handoff draft writer: `brigade handoff draft` writes a linted Memory Handoff draft using Brigade's card/no-card section style, so agents can record workflow changes without hand-writing boilerplate.
- Plan-First Operator Loop: `brigade work task plan <id> --write` now writes a plan.md + JSON plan artifact (assumptions, acceptance, risks, steps, next safe command); `--meta` writes a plan-for-the-plan; `--from-research <run-id>` attaches a research report as quarantined evidence. New `brigade work plans`, `brigade work import context` (untrusted raw-context intake), `brigade work plan-promote`/`work plan-proposals` (accepted plans -> local draft template/rule/skill proposals, never installed). `work doctor`/`work brief` surface pending tasks lacking a plan artifact.
- First-class OpenCode handoff support: `.opencode/memory-handoffs/` is now a built-in writer inbox (install scaffolding, ingest, doctor, handoff doctor source coverage, fleet sweep, security skip-list, and the interactive selector), so OpenCode handoffs ingest without a manual `--handoff-inbox` flag. The writer-inbox map is now centralized in `brigade.selection.WRITER_INBOXES`.
- Shared untrusted-context policy helper (`brigade.untrusted`): `wrap_untrusted` frames external content as data-not-instructions with a content-derived fence, and `scan_untrusted` reports injection signals. Adopted in the research extractor, and handoff ingest now routes injection-flagged content to the review inbox instead of auto-filing it into a card or document.
- New `research` command group: local-first iterative deep research grounded in a
  trusted local corpus, with an opt-in, quarantined web tier (Playwright, no API
  keys required). Emits a self-contained HTML report and a memory handoff; runs
  persist under `.brigade/research/` and are resumable/cancellable. Uses the
  roster `researcher` model (cloud); Brigade never runs a model locally.
- New `pantry` station (alias `larder`) and the `agentpantry` managed tool. `brigade add pantry` installs agentpantry via `go install`, and `brigade doctor`/`brigade status` health-check it by shelling out to `agentpantry status --json`. Like the memory satellites, agentpantry inspects host-global state, so its checks are advisory and never FAIL a workspace run: an unwired install (no config) is a `WARN`, a missing pre-shared key is a `WARN`, otherwise `OK`.

### Fixed
- `brigade operator quickstart` now scopes `.brigade/handoff-sources.json` to the selected writer harnesses and writes an initial local handoff-ingest latest-run log, so a fresh one-harness setup does not warn about unwired side harnesses or a missing ingestor log.
- `brigade operator doctor --profile local-operator` no longer points brand-new local setups at release-readiness import work as the primary next command when the only readiness item is a missing release receipt.
- `brigade daily status` now uses a lightweight daily center snapshot and bounded status sections so slow readiness subsystems report warnings instead of hanging the daily loop.
- Operator migration imports now supersede stale rollup imports when a source fingerprint changes, preventing older replacement batches from staying ahead of current rollups in the daily queue.
- `brigade operator doctor --profile local-operator` no longer treats a generated but unenabled Content Guard hook as a blocking issue in fresh quickstart installs. Missing Content Guard remains visible as advisory setup state.
- Removed an unsafe temp-directory cleanup example from the init failure issue template and contributor smoke-test docs, replacing it with `mktemp -d` fresh target setup.
- The security scanner no longer reports its own plaintext-password detector variable names as findings while scanning Brigade source.
- Corrected the stale `github.com/solomonneas/...` install URLs for the `memory-doctor` and `bootstrap-doctor` managed tools to their actual `escoffier-labs` org, so `brigade add memory` installs from the right repos.
- Dedupe guard in `brigade ingest` document routing. A `no-card` route whose content (or its first meaningful line/anchor) is already present in the target document is now sent to the review inbox instead of being appended again, matching the canonical pipeline and preventing duplicate content on re-routed handoffs.

### Documentation
- Documented the existing-operator adoption path, migration rollup, redacted surface reviews, and security scanner first-response workflow in the README, quickstart, technical guide, and agent-assisted setup guide.
- Added a first-response checklist for likely real credential findings, covering redacted review, `.env` or environment storage, KeePass preservation, transcript scrubbing, rotation, and closeout.
- Refreshed contributor setup guidance for Brigade naming, current source paths, quickstart issue reports, and intended GitHub label taxonomy for public support triage.
- Added expected quickstart and operator doctor output to the new-user quickstart guide.


## [0.8.0] - 2026-06-01

### Added
- Canonical flat bootstrap thresholds in `brigade.budgets` (`DEFAULT_BOOTSTRAP_SOFT_LIMIT`, `DEFAULT_BOOTSTRAP_HARD_LIMIT`, `BOOTSTRAP_HARD_LIMIT_CEILING`) for the whole-file auditor model, so downstream bootstrap tooling can source one set of limits instead of redeclaring its own.
- Handoff backlog detection. `brigade handoff doctor` (and the memory station in `brigade doctor`) now emits a `handoff_backlog` warning when an inbox has pending handoffs whose oldest entry is older than three days, i.e. handoffs are being written but nothing is ingesting them. `InboxHealth` gained an `oldest_pending_age_seconds` field. At the fleet level, `brigade repos scan`/`doctor` now emit a `repo_handoff_backlog` warning for any fleet repo with an un-ingested, stale handoff pile-up. This catches the silent gap where a repo's inbox is never reached by the canonical ingester (for example an uncovered repo missing from the ingest config).
- Canonical budgets module `brigade.budgets` is now the single source of truth for bootstrap-file byte budgets, memory-card budgets, the MEMORY.md index line limit, and the handoff-backlog and memory-care staleness thresholds. `doctor`, `ingest`, `handoff`, and `repos` all consume it so preventive guards and post-hoc warnings can never disagree. Satellite tools (bootstrap-doctor, memory-doctor) are intended to depend on brigade and consume these definitions rather than redeclaring them.
- Bootstrap budget guard in `brigade ingest`. A `no-card` handoff that would push a bootstrap file (e.g. `TOOLS.md`, `USER.md`) past its byte budget is now routed to the review inbox instead of appended, so the ingester can no longer silently bloat a session-prefix file past its truncation ceiling. Non-bootstrap targets such as `.learnings/*` are unaffected.
- `brigade repos ingest` fleet driver. Sweeps every registered, reachable fleet repo, routing each repo's handoffs into the canonical owner's memory and archiving the processed handoffs back in the source repo. Defaults to a dry run; pass `--apply` to write. `ingest.run` gained an `owner` parameter and a reusable `ingest.ingest_into` core to support the many-writers/one-owner model.

### Changed
- `brigade work backup init` now writes wider staleness thresholds for the `cloud` destination (`snapshot_stale_hours = 192`, `check_stale_hours`/`prune_stale_hours = 336`) so an off-site copy on a slower cadence such as a weekly backup does not report stale every day. The `nas` destination defaults are unchanged. Existing `.brigade/backups.toml` files are not modified.

### Documentation
- `docs/backup-health.md` now documents the two-tier NAS-frequent plus cloud-weekly threshold pattern and clarifies that backup health monitors snapshot-history backups (restic/borg), not bidirectional last-writer-wins file syncs such as a KeePass database mirror.

## [0.7.0] - 2026-05-31

### Added
- `brigade work phases session checkpoint <session-id|latest>` for local AFK session recovery points that record safe summaries, notes, current next-step state, and suggested commands without executing anything.
- `brigade work phases session checkpoints list/show/compare` for text and JSON inspection of local AFK session recovery points and stale next-step detection.
- `brigade work phases session checkpoints import-issues` for routing blocked or stale checkpoint issues into deduped `source: phase-session-checkpoint` work imports.
- `brigade work phases session next/resume` now include the latest checkpoint summary and checkpoint issue counts when a session has recovery metadata.
- `brigade work phases session recovery-note` plus `recovery-notes list/show` for local AFK recovery notes with safe summaries, notes, evidence labels, session references, and activity timeline events.
- `brigade work phases session recovery-notes closeout` for reviewed, deferred, blocked, or archived closeout metadata on AFK recovery notes.
- `brigade daily plan` now includes active phase session checkpoint issues as `phase-session-checkpoint` candidates with suggested checkpoint import commands.
- `brigade daily run` can write one safe local phase session checkpoint as its selected bounded action.
- `brigade work phases session risk` for a read-only AFK session risk summary across next-step blockers, checkpoint drift, recovery notes, and phase doctor issues.
- `brigade work phases session verification` for read-only verification rollups across the phase records in an AFK session.
- `brigade work phases session privacy` for read-only privacy-check rollups across the phase records in an AFK session.
- `brigade work phases session handoffs` for read-only handoff coverage rollups across the phase records in an AFK session.
- `brigade release doctor` now warns when active phase-session checkpoint evidence is blocked or stale.
- Release candidate evidence now includes the latest phase session checkpoint and checkpoint compare summary.
- `brigade center reviews` now includes blocked or stale phase-session checkpoint review items.
- `brigade work brief` now includes latest phase-session checkpoint and checkpoint compare evidence.
- `brigade work phases actions plan/build` now creates local actions for blocked or stale phase-session checkpoints.
- `brigade work phases session checkpoints archive` for moving an old checkpoint into local archive metadata.
- Phase session reports now include recovery evidence for latest checkpoints, checkpoint compare issues, and recovery notes.
- `brigade work phases schema` now publishes AFK session health contracts for session next, resume, checkpoints, recovery notes, risk, verification, privacy, handoffs, reports, progress, and gate outputs.
- `brigade work phases session protocol <session-id|latest>` for a wrapper-safe AFK resume protocol that summarizes next step, risk, progress, checkpoint state, completion gate, allowed command prefixes, forbidden actions, and whether `session resume` is safe to record.
- `brigade release candidate compare` now detects phase session checkpoint, checkpoint-compare, and completion-gate drift after a candidate bundle is built.
- `brigade work phases session audit <session-id|latest>` for a read-only AFK session self-audit across resume protocol, progress, risk, verification, privacy, handoff, and completion-gate evidence.
- Phase 226-250 AFK hardening closeout recorded the final session gate path for checkpoint, recovery, wrapper protocol, release compare, and self-audit work.
- `brigade work phases session start/list/show/closeout` for local AFK phase execution sessions that track a requested range, current phase, phase status, commit and test counts, report references, closeout state, and next command.
- `brigade work phases session next/resume` for read-only or metadata-only AFK session recovery that identifies the safest next phase command without executing it.
- `brigade work phases session report build/list/show` for local Markdown and JSON evidence bundles over phase execution sessions.
- `brigade work phases session activity <session-id|latest>` for a chronological read-only activity ledger across phase records, starts, completions, tests, commits, reports, actions, imports, closeouts, handoffs, and resumes.
- `brigade work phases session progress <session-id|latest>` for read-only percent complete, status counts, blockers, current phase, next command, test coverage, commit and push coverage, and remaining-step summaries.
- `brigade work phases session import-issues <session-id|latest>` for routing unresolved AFK session blockers into deduped `source: phase-session` work imports.
- `brigade work phases goal scaffold --range <range>` for local editable `/goal` drafts from ledger state, session evidence, blockers, and roadmap references.
- `brigade work phases session gate <session-id|latest>` for the final read-only AFK session claim check, with release doctor and candidate evidence carrying the latest gate result.
- `brigade daily status/plan/review/run/doctor` now surface active phase sessions, and daily run can build one phase session report or close out one completed reviewed session as its single safe step.
- Release doctor, release candidate evidence, center status/reviews, and work brief/doctor now include compact phase session and session report state.
- `brigade work phases evidence add <phase-id>` for appending local evidence attachments to phase records, with doctor warnings for missing referenced evidence files.
- `brigade work phases verify plan/record` for local phase verification matrices that show expected commands and record operator-supplied outcomes without executing tests.
- `brigade work phases reconcile <phase-id|range|latest>` for read-only local git reconciliation of phase commit hashes, push refs, branch containment, and dirty worktree state.
- `brigade work phases privacy <phase-id|range|latest>` for local phase evidence privacy scans with redacted findings and recorded clean or blocked summaries.
- `brigade work phases handoff <phase-id|range|latest>` for drafting and optionally linting a Memory Handoff from selected phase evidence without editing canonical memory.
- `brigade work phases closeout <phase-id|range|latest>` for local reviewed, deferred, blocked, or archived phase ledger closeouts, plus stale unreviewed completed-phase doctor warnings.
- `brigade work phases compare <phase-id|range|latest>` for read-only phase evidence freshness checks against local HEAD, referenced files, report age, test evidence, and doctor issue counts.
- `brigade work phases actions plan/build/list/show/start/done/defer/archive` for metadata-only phase-ledger action queues sourced from doctor issues and closeout blockers.
- `brigade daily plan/review/run` now considers phase-ledger actions and unresolved phase issues, and can start one phase action or build one phase report as a bounded local daily step.
- Release doctor, release candidate bundles, and release candidate compare now include phase-ledger closeout and report evidence, with warnings for unresolved closeouts, stale reports, and unreviewed pushed phases.
- `brigade work phases report closeout <report-id|latest>` for local reviewed, deferred, superseded, or archived phase report bundle closeout metadata.
- `brigade work phases report compare <report-id|latest>` for read-only checks of saved phase report freshness against current ledger status, doctor issues, HEAD labels, and report closeouts.
- Phase ledger health now includes phase action queue counts and top action details, with visibility in daily status, work brief, work doctor, and center status.
- `brigade work phases actions import-issues` for routing open phase action records into the work inbox as deduped `source: phase-ledger-action` task imports.
- Phase health and release candidate evidence now include the latest phase report compare summary, and release doctor warns when report compare has open issues.
- `brigade work phases init/plan/list/schema/status/next/show/start/complete/defer/doctor/import-issues` plus `brigade work phases report build/list/show` for a gitignored phase execution ledger that makes unattended multi-phase work auditable, detects silent compression, writes local reports, routes ledger issues into the work inbox, and surfaces phase-ledger health in daily, work, center, and release views.
- Daily hardening audits now perform phase-aware checks across daily receipts, center contracts, inbox evidence quality, repo fleet daily-use state, and release self-dogfood evidence, with release readiness and release candidates carrying compact summaries for those checks.
- `brigade daily hardening plan/audit/import-issues/closeout` plus `docs/phase-115-164-plan.md` for the production-hardening queue across daily reliability, operator-center contracts, inbox evidence quality, repo-fleet daily use, and self-dogfood release evidence.
- `brigade daily resume/repair/unblock/protocol`, `brigade daily telemetry doctor`, daily approval compare/archive commands, normalized daily adapter receipts, explainable plans, verification-aware closeout fields, local telemetry, and release evidence for the daily driver.
- `brigade daily approvals list/show/approve/reject/hold` plus `brigade daily run --approval <approval-id>` for local approval requests that preserve daily-driver context across approval-required boundaries.
- `brigade daily init/schema/history/show/doctor` plus local `.brigade/daily.toml` settings for daily-driver config, JSON contracts, receipt inspection, and stale or blocked run health.
- `brigade daily status/plan/review/run/closeout` for an agent-facing daily driver that ranks local operator evidence, selects one safe action, runs or stages one bounded item with receipts, and closes out the day without arbitrary execution or remote mutation.
- `brigade work import provenance` for a read-only cross-producer import provenance audit with text and JSON output.
- `brigade roadmap commands` for a parser-derived public command documentation contract with text, JSON, generated `docs/command-inventory.md`, and stale-inventory checks.
- `docs/phase-61-100-plan.md` as the public, testable phase queue for roadmap completion hardening.
- Public `templates/` index that points fresh-start users at the packaged starter templates without exposing local dogfood workspace files.
- Built-in `brigade doctor` bootstrap budget checks that fail hard when installed bootstrap files exceed conservative byte limits.
- Built-in `brigade doctor` memory-card budget checks that fail when `memory/cards/*.md` cards become too large.
- Built-in `brigade doctor` memory-index checks that fail when `MEMORY.md` links to missing `memory/cards/*.md` files.
- `brigade doctor` memory-care freshness checks for stale decay scans, plus hard failures for corrupt scan or refresh-queue JSON.
- `brigade run "<task>"`, a bounded aboyeur flow that asks one rostered orchestrator to plan assignments, dispatches worker CLIs in parallel, then asks the orchestrator to synthesize the final answer.
- `.brigade/roster.toml` loading for cross-model agent rosters using the user's installed CLIs (`codex`, `claude`, or `ollama:<model>`). Claude is optional, not required.
- `brigade roster init` and `brigade roster doctor` to scaffold a Codex/Ollama starter roster and validate roster syntax plus installed CLI availability.
- `brigade dogfood` for a built-in Codex-only, prompt-level read-only, inspected run with artifacts and optional handoff.
- `brigade dogfood init` to persist machine-local dogfood defaults in gitignored `.brigade/dogfood.toml`, enabling a one-command daily `brigade dogfood` path.
- `brigade dogfood status` to report local dogfood readiness, effective paths, CLI availability, ignore coverage, sandbox mode, and latest run.
- `brigade dogfood latest`, `brigade dogfood next`, and per-run dogfood `summary.md` artifacts for turning the latest run into the next work item without copying artifact paths.
- `brigade run --show-plan` and `--verbose` visibility modes, plus defensive runtime enforcement of roster `allow_models`.
- `brigade run --inspect` to print a readable artifact summary immediately after a run completes.
- `brigade run --cwd`, `--output-dir`, and default `.brigade/runs/<id>` artifacts for dogfooding auditable runs.
- Start, finish, and duration metadata in `run.json` artifacts.
- `roster.json` run artifacts that capture the effective orchestrator, agents, limits, allow-list, and timeouts for later review.
- `plan-attempts.json` run artifacts that capture raw planner outputs and parse errors for debugging failed planning runs.
- `synthesis.json` run artifacts that capture orchestrator synthesis status, detail, and raw text for non-dry runs.
- Successful `--handoff` runs now record the written handoff path in `run.json`.
- `brigade run --handoff` to write a Memory Handoff for successful runs, with `--handoff-inbox` override.
- `brigade runs list` to print recent run artifact directories from `.brigade/runs`.
- `brigade runs latest` to show the newest run summary without copying a run path from `brigade runs list`.
- `brigade runs show <run-dir>` to print a readable summary of one run artifact directory.
- `brigade work status` to report the current repo branch, dirty files, dogfood readiness, latest run, and extracted next step for daily work sessions.
- `brigade work start` and `brigade work end` to create local `.brigade/work/` session artifacts for normal daily work loops.
- `brigade work end --handoff` to write a Memory Handoff from closed work session artifacts.
- `brigade work list`, `brigade work latest`, and `brigade work show` to inspect local work session artifacts.
- `brigade work recap` to summarize recent or date-filtered work sessions.
- `brigade work run` to start a work session, run dogfood, close the session, write a work handoff, and print a recap in one command.
- `brigade work resume` to show the active or latest work session, latest dogfood run, extracted next step, and suggested command.
- `brigade work next` to resolve the next daily task without inspecting artifacts, plus `brigade work run` now uses the latest extracted next step when no task is passed.
- `brigade work next --json` to expose the resolved daily task, active session, dogfood snapshot, and suggested command to wrappers.
- `brigade work bootstrap` to initialize and verify the dogfood-backed daily work loop in one command.
- `brigade work brief` and `brigade work brief --json` as a start-of-day entrypoint with git state, latest sessions, latest dogfood run, resolved next task, and suggested command.
- `brigade work tasks` plus `brigade work task add/show/done` to manage a gitignored local task ledger under `.brigade/work/tasks.json`.
- Typed task metadata and repeatable acceptance criteria for `brigade work task add`, plus `brigade work task plan` for the completion checklist.
- `brigade work task add --template` for `vertical-slice`, `bugfix`, `red-green-refactor`, `docs`, and `security-follow-up` defaults.
- `brigade work task add --from-issue <issue-url-or-number>` to import GitHub issue title and metadata through the existing `gh` CLI when available.
- `brigade work task add --from-issue` now imports acceptance criteria from GitHub issue-body checkboxes and acceptance/test sections into the local task acceptance field without storing the raw body.
- Repo installs now include public-safe workflow rule templates under `rules/issue-tdd-loop.md` and `rules/acceptance-driven-work.md`, and `brigade work doctor` reports missing rule templates.
- `brigade work import issue-repairs` to route incomplete, stale, unchecked, or closed-remote issue-backed local tasks into repairable local imports without GitHub mutation.
- `brigade work acceptance` now includes completion-time acceptance gaps, code-review finding outcomes, latest work closeout status, and fuller release readiness plus release candidate evidence.
- Handoff ingestor log parsing now recognizes skipped, failed, malformed, unreachable-source, and no-reply warning states in issues and normalized reconcile receipts.
- Handoff source coverage issues now carry source keys and fingerprints, so uncovered writer inbox repairs dedupe and dismissed items stay quiet until coverage changes.
- `brigade release schema` for a wrapper-friendly local manifest of release readiness, candidate, fleet train, waiver, and manual evidence JSON record contracts.
- `brigade release candidate audit` and `import-issues` for local release candidate provenance checks and work-inbox routing without publishing or remote mutation.
- `brigade center schema` for a read-only wrapper-facing manifest of operator center status, activity, reviews, templates, report, report review, and action queue JSON contracts.
- `brigade center readiness plan/closeout/list/show/import-issues` for final local operator readiness closeouts over roadmap, docs command inventory, center, release, repo fleet, security, memory, tools, context, learning, waivers, and a manual-only publish checklist.
- `brigade center report diff <base> <compare> --record` for local operator report diff receipts that track new review items, resolved items, new blockers, and stale receipt references.
- `brigade center actions doctor` and `import-issues` for local operator action aging policy warnings and explicit work-inbox routing.
- `brigade repos discover plan` for dry-run repository discovery under explicit configured roots with include/exclude rules, safe labels, path redaction, and no cloning.
- `brigade work run` now records consumed task snapshots in work-session artifacts and stores completed session, dogfood run, and acceptance metadata on completed ledger tasks.
- `brigade work run --queue-next` to queue the successful run's extracted next step, with duplicate pending task protection.
- `brigade work import add/list/show/promote` to manage a gitignored local import inbox for scanner-discovered candidate work.
- `brigade work inbox` to group pending scanner imports by source, kind, priority, age, and acceptance coverage with suggested next commands.
- `brigade work import validate` and `brigade work import ingest` for scanner-authored JSONL import files.
- Scanner-authored task imports can now carry `type`, `priority`, `template`, and `acceptance`, and promotion preserves those fields on local ledger tasks.
- `brigade work import plan <import-id>` to preview the exact task a reviewed import would create.
- `brigade work import promote --run <import-id>` to promote one task import and immediately run it through the work-session loop.
- `brigade work import memory-care` to convert `memory/cards/decay/refresh-queue.json` into local work imports.
- `brigade memory care init/scan/status/doctor/import-issues` for read-only local memory card decay scanning, refresh queue production, daily-loop health, and reviewed work inbox routing.
- Memory-care scans now flag missing reviewed dates and missing freshness dates, and status output summarizes reviewed, freshness, confidence, and evidence metadata coverage without editing cards.
- `brigade memory care plan-fixes` for planning-only reviewed/freshness metadata repair candidates with blockers, import metadata, and daily brief visibility.
- `brigade work import chat-sweep` to convert `.brigade/chat-memory-sweeps/latest.json` issues into local work imports.
- `brigade work import memory-refresh` to convert memory-refresh candidates into TDD-ready scanner task imports with card identity, refresh reason, evidence summary, and acceptance criteria.
- Chat sweep imports now convert actionable sweep issues into task imports, preserve local provider/channel/thread/confidence metadata, and omit raw private chat fields.
- `brigade chat surfaces init/list/show/doctor` plus `brigade chat sweep validate/ingest/import-issues` for local chat export fixtures that normalize safe findings into scanner inbox imports without live chat APIs.
- Chat surface providers now support aliases for common export names, including Discord, Slack, Telegram, ClickClack, generic JSON, and JSONL, normalized to canonical provider families.
- Scanner producer imports now use source item keys and fingerprints for idempotency, including dismissed-import protection until a source item materially changes.
- Inbox doctor now reuses the cross-producer provenance audit to flag producer imports missing source identity, source fingerprints, safe summaries, evidence references, or scanner run metadata.
- Context packs now summarize docs and guidance files by presence and safe metadata instead of copying file contents, learning candidates avoid raw import text fallback, and release note inputs redact secret-looking values.
- Memory-care scan issues include stable source fingerprints for stale, expired, undersourced, contradictory, missing-index-link, orphaned-card, oversized-card, and missing-frontmatter findings, while keeping memory card edits explicit.
- `brigade work scanners init/list/show/plan/doctor` for a gitignored local scanner registry and schedule planner that never executes scanners automatically.
- `brigade work scanners doctor --import-issues` to route scanner registry health warnings into the existing local work inbox.
- `brigade work scanners run <scanner-id>`, `run --all`, `run --due`, `runs`, and `run-show <run-id>` for explicit local scanner producer execution with gitignored receipts, stdout/stderr logs, output snapshots, due-run planning, pending import count reporting, and scanner-health imports for failed, stale, due, or malformed runs.
- Scanner runs can now attach provenance to matching new imports and can explicitly ingest configured JSONL output with `brigade work scanners run ... --ingest-output`.
- `brigade work sweep`, `brigade work sweeps`, and `brigade work sweep-show <sweep-id>` for explicit daily scanner sweeps that run due producers, ingest configured JSONL outputs by default, write gitignored sweep reports, and keep promotion manual.
- `brigade work sweep-review <sweep-id>` and `sweep-review latest` for read-only triage of sweep-created imports, skipped and dismissed fingerprints, provenance health, grouping, and suggested next commands.
- `brigade work inbox doctor` and `brigade work inbox archive` for scanner inbox hygiene checks and archiving old promoted, dismissed, or superseded imports.
- `brigade work import plan-handoff` and `promote-handoff` for lint-gated Memory Handoff drafts from durable non-task scanner imports, with provenance preservation and raw chat privacy checks.
- `brigade handoff list/show/archive` for local Memory Handoff draft queue visibility, stale or invalid draft health, and reviewed archive records without running the ingestor.
- `brigade handoff runs`, `run-show`, and `reconcile` for local handoff ingestion receipt visibility, draft outcome reconciliation, and archive outcome metadata without running the ingestor.
- `brigade work review init/plan/run/runs/show/import-findings/findings/finding-show/closeout` for explicit local multi-harness code review producers, receipts, normalized findings, imported finding resolution, local closeout records, and `code-review` work inbox imports without automatic fixes or remote mutation.
- `brigade work verify plan/run/runs/show` and `brigade work closeout <session-id-or-latest>` for local verification receipts and work closeout records that collect task acceptance, test command results, scanner sweep status, code review closeout state, handoff draft status, and session evidence without CI or remote mutation.
- `brigade release plan/doctor/run/runs/show` for local release-readiness receipts that collect work closeout, verification, review closeout, scanner sweep, security, handoff, content-guard, docs, changelog, roadmap, and git-state evidence without pushing, tagging, or mutating remotes.
- `brigade release ci doctor/import-issues` for local GitHub Actions platform deprecation warnings from workflow files or saved CI summaries, including redacted safe excerpts, release-readiness evidence, and work-inbox routing.
- `brigade release smoke plan/record/list/show/doctor` for local install smoke matrix receipts across supported depth and harness combinations, including stale and missing smoke warnings in release readiness and center activity.
- `brigade release candidate plan/build/list/show/archive` for local release candidate bundles with readiness evidence, release notes drafts, manual-only publish plans, changed file lists, blockers, warnings, and content-guard summaries without pushing, tagging, or creating releases.
- `brigade release candidate compare` and `closeout` for local candidate freshness checks and reviewed, superseded, archived, or draft closeout metadata.
- `brigade context plan/build/list/show/archive` for local context engineering packs with safe summaries, task acceptance, recent evidence, and explicit private-evidence exclusions.
- `brigade context sync plan/record` for read-only context sync planning receipts against configured harness destinations, with conflict and freshness checks but no context file writes.
- `brigade context doctor/import-issues` for stale context packs, missing source references, stale task acceptance, stale tool references, and `source: context-pack` work imports.
- `brigade projects audit/import-issues` plus `brigade projects readiness plan/record/list/show` for gitignored local project consolidation decisions, manual-only migration planning, and local readiness receipts covering docs, license, security, release, ownership, and migration blockers.
- `brigade projects closeout/closeouts/closeout-show` for reviewed, deferred, superseded, or archived project migration closeouts that quiet unchanged readiness issues and resurface changed fingerprints.
- `brigade learn plan/doctor/import-issues` plus `brigade learn closeout/closeouts/closeout-show` for bounded local learning candidates that become reviewed tasks, handoffs, suppressions, accepted risk, archive, deferral, or dismissal, with unchanged closeouts quieted and changed fingerprints resurfaced.
- `brigade learn replay export/list/show/compare` for safe local before/after learning replay receipts, redacted summaries, compare receipts, release evidence, and operator-center review surfacing.
- `brigade security sarif` and security scan SARIF bundle output for dependency-free SARIF 2.1.0 evidence generated from redacted local findings.
- `brigade security template-audit` for focused public template and docs privacy checks, with placeholder allowlists, doctor integration, and release-readiness evidence.
- Security guardrail coverage now labels repo guidance, skills, slash commands, subagents, and tool wrappers separately, including prompt-injection and environment-exfiltration patterns with template confidence handling.
- Security policy presets now include `ci`, and security closeouts record policy-pack blocker, warning, and accepted-risk evidence for release readiness and candidate packets.
- `brigade tools pack build/list/show/archive` and `brigade tools sync plan/apply` for portable tool evidence bundles and reviewed projection sync over the existing managed projection path.
- `brigade tools parity status/closeout` for local reviewed projection parity receipts that quiet unchanged missing, stale, unmanaged, conflicted, or parity-gap projection issues while resurfacing changed fingerprints.
- Release readiness and release candidate evidence now include tool pack freshness, projection parity closeout state, sync-plan blockers, approval queue counts, run history, and checkpoint state without applying projections.
- `brigade work backup closeout`, `brigade security closeout`, `brigade handoff closeout`, `brigade memory care closeout`, and `brigade work acceptance` for reviewable local closeout and acceptance rollup receipts.
- Backup health status now separates raw, active, quieted, changed-fingerprint, and restore rehearsal issue counts, and release evidence includes the safe backup operator summary without copying private destination values.
- `brigade center status/activity/reviews/templates`, `brigade center report plan/build/list/show/archive/review/compare/closeout`, and `brigade center actions plan/build/list/show/start/done/defer/archive` for local operator-center summaries, local report bundles, reviewed daily action queues, freshness comparison, and report closeout over work, scanner, review, handoff, tool, learning, context, project, security, and release state.
- `brigade roadmap audit` and `brigade roadmap patterns` for roadmap closure checks, stale phase warnings, documented command drift, neutral pattern-family coverage, and source-pattern decisions.
- `brigade repos init/list/show/scan/doctor/import-issues` for gitignored local repo-fleet readiness checks, safe setup metadata, fallback guidance detection, and `repo-fleet` work inbox imports.
- `brigade repos health-commands` for read-only inspection of optional fleet health command labels, timeouts, latest sweep receipt status, stale receipts, and failed command receipts.
- `brigade repos sweep plan/run/runs/show/closeout` for explicit repo-fleet evidence refresh sweeps that run configured local read/report commands, write gitignored sweep receipts, track per-repo command status, and surface stale, failed, or unclosed sweeps through repo, center, work, and release health.
- `brigade repos report plan/build/list/show/archive/closeout` and `brigade repos actions plan/build/list/show/start/done/defer/archive` for local repo-fleet operator rollups and reviewed fleet action queues using safe labels, counts, statuses, fingerprints, and receipt labels only.
- `brigade repos actions dispatch plan/apply/report`, `dispatch --all-reviewed`, `reconcile`, and `context plan/build` for routing reviewed fleet actions into target repo work imports, explaining dismissed, superseded, changed, or broken dispatch state, building action-scoped context packs, and reconciling target repo completion evidence back into the local fleet queue.
- `brigade repos release plan/build/list/show/compare/closeout/archive` for local fleet release train bundles that collect per-repo release readiness, release candidates, fleet action reconciliation, verification, review, security, and operator evidence into manual-only publish plans without remote mutation.
- `brigade repos release actions` and `brigade repos release evidence` for reviewed release train action queues and manual publish evidence records that stay local, explicit, and non-executing.
- `brigade repos release reconcile` and `summary` for resolving fleet release actions against manual evidence and including reconciliation summaries in release train closeouts.
- `brigade repos release report/matrix/checklist/hygiene/import-issues/ready` for local release train review reports, matrix tables across readiness, evidence, actions, and waivers, evidence checklists, hygiene checks, unresolved evidence imports, and manual publish readiness gates.
- `brigade repos release waivers`, `activity`, `manifest`, and `audit` for explicit release-train waivers, chronological train activity, bundle manifests, bundle audits, and waiver-aware manual publish readiness.
- Release-train waivers now support expiry, owner labels, policy templates, renewal, health checks, work-inbox import routing, and ready/audit visibility for expired, stale, missing-expiry, missing-owner, weak-reason, invalid-scope, repo-drift, or train-changed waivers.
- `brigade work sweep closeout <sweep-id|latest>` for reviewable sweep closeout records that block unresolved pending imports, support explicit deferrals, and surface unclosed sweeps through inbox hygiene.
- `brigade work backup init/status/doctor/import-issues` for read-only local backup health summaries and `backup-health` inbox imports.
- Backup health checks for stale snapshots, failed or stale checks, failed or stale prunes, missing summaries, overdue restore rehearsals, and unsafe private summary fields.
- `brigade tools init/list/show/search/describe/contracts/call plan/call queue/call list/call show/call approve/call reject/call hold/call run/runtime/policy/plan/apply/doctor/import-issues`, plus `brigade tools run list/show/latest/replay` and `brigade tools checkpoint list/show/approve/reject/resume`, for portable tool, slash command, skill, superpower, script, and MCP catalog discovery plus explicit projection writes, read-only call planning, local call approval review, explicit approved script and local MCP execution, run history inspection, replay review, checkpointed resume, runtime supervision, and host-local execution policy.
- Tool catalog health checks for missing sources, missing manifests or schemas, invalid schema JSON, invalid contract schemas, missing examples, bad argument templates, missing contracts, parity gaps, missing projections, unmanaged projections, locally edited managed projections, stale projections, MCP config issues, stale health files, unsafe auth/env fields, and high-risk command shapes.
- Schema-backed call plans validate local JSON args against a dependency-free JSON Schema subset, render configured argument templates, report blockers, and redact secret-looking fields without invoking tools.
- Portable tool call approvals are stored in gitignored `.brigade/tools/calls.jsonl`, dedupe equivalent pending or approved calls, reject blocked approvals, and surface stale pending or stale approved calls in doctor, brief, and `tool-catalog` imports.
- Approved portable script calls can now be run explicitly with `brigade tools call run <call-id>` or `--next`, with local receipts and stdout/stderr logs written under gitignored `.brigade/tools/runs/`.
- `brigade tools run list/show/latest/replay` inspects local execution receipts and queues reviewed replay candidates without direct reruns or bypassing approval, runtime, or policy gates.
- `brigade tools checkpoint list/show/approve/reject/resume` records script-requested local checkpoints, reviews allowed resume choices, and resumes only after revalidating approval, runtime, policy, contract, source, and projection gates.
- Approved local MCP calls can now run through `brigade tools call run` via a configured local stdio command, already-running managed runtime, JSON-RPC `initialize` / `tools/list` / `tools/call`, and receipts with redacted MCP request and response summaries.
- `brigade tools runtime init/list/show/status/start/stop/restart/doctor` for explicit local runtime supervision with PID files, logs, stale PID detection, port conflict checks, health checks, and tool-call runtime gating.
- `brigade tools policy init/show/doctor` for host-local execution policy, including allowed families/effects, denied effects, required approval modes, timeout caps, runtime allow-lists, and env label bindings without storing secrets.
- Managed tool projections record source and projection fingerprints so `brigade tools plan`, `apply`, and `doctor` can distinguish missing, current, stale, unmanaged, and conflicted projection states.
- `tool-catalog` inbox imports with stable source fingerprints and dismissed-import protection until a catalog issue materially changes.
- `brigade work import triage` to group pending imports by source and kind.
- `brigade work import dismiss` to close noisy imports without promoting them.
- `brigade work import promote --all` with optional `--source` and `--kind` filters for batch promotion.
- `brigade work import list/triage/promote/dismiss` metadata filters for scanner-specific fields such as `handoff_issue_category`.
- `brigade work import dismiss --all` for filtered bulk dismissal of pending imports.
- `brigade handoff doctor` to compare pending `.claude` and `.codex` memory handoffs against gitignored local source config.
- Repo installs now include `.brigade/handoff-sources.example.json` as the local handoff ingestor source-list contract.
- `brigade handoff doctor` ingestor-log checks for stale latest-run logs, skipped malformed handoffs, warning summaries, and no-reply/no-update masking signals.
- `brigade handoff issues` and `brigade handoff import-issues` to turn handoff ingest warnings into grouped repair guidance and local work imports.
- `brigade handoff issues --category` and `brigade handoff import-issues --category` for category-limited handoff issue review/import.
- `brigade handoff lint` to validate pending or explicit handoff files before ingest and catch card/document action mismatches that would be skipped later.
- `brigade handoff sync-issues` to import new handoff-ingest issues without resurrecting dismissed ones and close stale local handoff tasks/imports.
- `docs/import-schema.md` documenting the local import JSONL contract for scanners and wrappers.
- Cybersecurity plugin roadmap covering broad agent-workspace security checks plus Brigade-specific scanner, doctor, import, and multi-harness security checks.
- Built-in `security` station and `brigade security scan` for read-only agent workspace security checks.
- Deeper MCP security checks for unpinned `npx`, shell metacharacters, secret-looking env values, sensitive or broad file args, high-risk local commands, large server sets, and missing timeouts.
- Supply-chain security checks for package scripts, GitHub Actions permissions and action refs, Python URL dependencies, and legacy install hooks.
- `brigade security enrich` for explicit post-scan enrichment artifacts, with an offline local provider and opt-in MISP provider config.
- `brigade security scan --import-findings` to route security findings into the local work import inbox for review, with source `security-scan`, stable source fingerprints, safe metadata, evidence paths, and dismissed-import protection.
- `brigade security init` to write gitignored local defaults to `.brigade/security.toml`, including scan profiles, enabled checks, include/exclude paths, severity thresholds, suppressions, and output paths.
- `brigade security config`, `brigade security doctor`, `brigade security findings`, and `brigade security show <finding-id>` for local config inspection, health checks, grouped finding review, and single-finding inspection.
- `brigade security fix` to create the local security artifact directory and refresh the managed `.gitignore` block.
- `brigade security review`, `brigade security suppress`, and `brigade security unsuppress` for a local finding review lifecycle with required suppression reasons. Suppress and unsuppress accept finding ids, id prefixes, or fingerprints.
- Security policy presets (`personal`, `public-repo`, `strict`), scan profiles (`public-repo`, `internal-workspace`, `local-only-audit`), template scanning controls, stable finding ids and fingerprints, and fingerprint suppressions.
- `brigade security scan --output-dir <dir>` to write redacted `security-report.json` and `security-report.md` evidence bundles.
- `brigade work brief`, `brigade doctor`, and `brigade work doctor` now report security config health, latest security evidence bundle status, open finding health, and local security artifact ignore coverage.
- `brigade doctor` and `brigade work doctor` now warn on stale security suppressions and suppressions missing reasons.
- Security scan secret evidence is redacted before reports, docs, session artifacts, or work imports are written.
- `ROADMAP.md` covering the daily-driver path, scanner-ready inbox, chat-surface scanners, memory-card decay refresh, and portable operator setup.
- `brigade work note` to append timestamped checkpoints to the active work session without ending it.
- `brigade work doctor` to check dogfood config, Codex availability, local artifact paths, handoff inbox, ignore coverage, and latest run context for the daily work loop.
- Workspace installs now include `.brigade/memory-care.example.json` as a scanner wiring contract for memory-care decay output.
- Workspace installs now include `.brigade/chat-memory-sweep.example.json` plus an OpenClaw memory-sweep cron fragment for nightly chat/session sweep wiring.
- Roster-level and per-agent `timeout_seconds` controls for bounded CLI calls.
- `brigade run --read-only` prompt policy for planning and review runs that should inspect and recommend only, with native `codex exec --sandbox read-only` enforcement for Codex agents.

### Changed
- `brigade roadmap audit` now reads documented commands from command snippets instead of prose and normalizes parameterized examples such as `brigade tools show <id>` to their CLI command path.
- `brigade roadmap audit --json` now includes deferred roadmap ownership records with subsystem, owner, reason, source section, status, and suggested phase.
- Roadmap phase headings now distinguish foundations, active work, and the phase queue so stale Current/Next warnings are actionable.
- Public repo contents now keep live dogfood workspace files, internal planning notes, and root memory cards untracked; public templates remain under `src/brigade/templates/`.
- Dogfood handoff defaults now use `.codex/memory-handoffs/` for new Codex-driven local configs while preserving explicit configured inbox paths such as `.claude/memory-handoffs/`.
- Bootstrap truncation is now treated as a hard doctor failure to prevent by moving durable detail into memory cards before agents load context.
- Dogfood runs now default to a 600 second per-agent timeout for practical daily repo reviews.
- Dogfood next-step extraction now handles markdown `## Next` sections and can fall back to `summary.md` when `final.txt` does not contain a next-step label.
- `brigade work run` now consumes the oldest pending ledger task before falling back to the latest extracted dogfood next step, and marks consumed tasks done after successful runs.
- `brigade work task add --from-next` now reuses an equivalent pending task instead of adding duplicates.
- `brigade work brief` now reports acceptance coverage for the next ledger task, and `brigade work run` passes accepted ledger criteria into the dogfood task prompt.
- `brigade work brief` now includes pending local work imports and import counts in both text and JSON output.
- `brigade work brief` now surfaces issue-backed next-task context.
- `brigade work doctor` now warns on pending tasks without acceptance criteria, unchecked or closed issue-backed tasks, and active work sessions left open too long.
- `brigade work doctor` now warns on stale scanner imports, task imports missing acceptance criteria, and noisy scanner sources with many dismissed imports.
- `brigade work brief` now surfaces pending handoff ingest issue counts when the local handoff source config has an ingestor latest-run log.
- The managed gitignore block now treats `.brigade/dogfood.toml`, `.brigade/security.toml`, `.brigade/runs/`, and `.brigade/security/` as local state.
- The managed gitignore block now treats `.brigade/handoff-sources.json` as host-local state.
- Live smoke docs now keep Codex agent execution in a trusted repo cwd while writing temporary roster, artifacts, and handoff output under `/tmp`.
- Handoff write failures now preserve final run artifacts, print the final answer, return nonzero, and mark `run.json` as `handoff-failed`.
- Dogfood runs default to prompt-level read-only plus Codex's `danger-full-access` sandbox setting for trusted-workspace use so repo inspection works on hosts where native read-only sandboxing blocks shell inspection; `--native-read-only-sandbox` opts into stricter native enforcement.

### Fixed
- `brigade init` now collapses mixed current and legacy managed `.gitignore` blocks into one regenerated Brigade block.

## [0.6.0] - 2026-05-24

### Added
- Managed tools: external CLIs that Brigade can install and wire per station via `brigade add <station>`. Brigade shells out to each tool, never importing it in process.
- `memory-doctor` and `bootstrap-doctor` attached to the `memory` station.
- `content-guard` attached to the `guard` station.
- New `tokens` station with `tokenjuice` for output compaction.
- `brigade doctor` folds installed managed tools into its report and surfaces each tool's own health. Tools that are not installed are reported as non-failing `[todo]` hints, so doctor stays green on a bare host.
- `memory-doctor` and `bootstrap-doctor` inspect the operator's canonical memory and bootstrap files (host-global), so their findings are labeled operator-scoped and treated as advisory `[warn]`, never failing a workspace `brigade doctor` run.

## [0.5.0] - 2026-05-24

### Changed
- Renamed the project to **Brigade**. The PyPI distribution is now `brigade-cli` and the command is `brigade`. The workspace config directory is now `.brigade`, with a `.solo-mise` read fallback so older installs keep working.

### Added
- Built-in station registry that drives the doctor checks.
- `brigade status` command, alongside `brigade init` and `brigade doctor`, reporting over the station registry.

### Deprecated
- The `solo-mise` command is kept as a deprecated alias for `brigade`.

## [0.4.0] - 2026-05-17

### Breaking
- Removed the `--profile <name>` flag from `solo-mise init`. The flag has been deprecated since v0.3.0 with a stderr migration warning. Use `--depth <minimal|standard|deep>` plus `--harnesses <list>` instead. Migration table in the v0.3.0 notes below.

### Internal cleanup
Removed `src/solo_mise/init.py`, the `templates/profiles/` directory and its six legacy profile manifests, plus `templates.load_profile` and `selection.profile_to_selection`. No user-facing impact beyond the flag removal above.

### Migration

Same as v0.3.0. If you somehow have v0.2.0-era scripts still using `--profile`, see the table in the v0.3.0 section below.

## [0.3.0] - 2026-05-16

### Added
- Two-axis selection model: `--depth {repo,workspace}` + `--harnesses {claude,codex,openclaw,hermes}` + `--include publisher`. Pick any combination of harnesses.
- Interactive prompt on bare `solo-mise init` (no flags). Defaults to claude + repo + no includes.
- `.solo-mise/config.json` is now the per-target source of truth for selection state. Read by `doctor`, `ingest`, and `reconfigure`.
- `solo-mise reconfigure --target . [--prune]` adjusts an existing install to a new selection. `--prune` removes orphaned files for deselected harnesses.
- Per-writer handoff inboxes: `.codex/memory-handoffs/` for Codex (in addition to existing `.claude/memory-handoffs/`).
- Ingester now scans all configured writer inboxes.
- Doctor reports apparent harness shape, checks per-writer inbox, warns on orphaned inbox dirs from unselected harnesses.

### Changed
- README reframed around the two-axis model. New "Picking your harnesses" section walks through four common combos.
- CONTRIBUTING.md "Adding a profile" replaced by "Adding a harness" + "Adding a depth" + "Adding an include".

### Deprecated
- `solo-mise init --profile <x>` still works but prints a stderr deprecation note pointing at the new flags. Will be removed in v0.4.0.

### Migration

If you have v0.2.0 scripts using `--profile`:

| v0.2.0 | v0.3.0+ |
|---|---|
| `--profile repo` | `--depth repo --harnesses claude` |
| `--profile workspace` | `--depth workspace --harnesses claude` |
| `--profile openclaw` | `--depth workspace --harnesses claude,openclaw` |
| `--profile hermes` | `--depth workspace --harnesses claude,hermes` |
| `--profile generic` | `--depth workspace --harnesses none` |
| `--profile publisher` | `--depth repo --harnesses claude --include publisher` |

## [0.2.0] - 2026-05-16

### Added
- Memory-care staleness scaffolding: `memory/cards/decay/` layout and a doctor
  warning when the decay folder is missing, so durable cards do not quietly rot.
- Multi-workspace handoff patterns for users administering more than one agent
  home; secondary workspaces write into their own `.claude/memory-handoffs/`,
  the owner pulls those into a staging inbox.
- TokenJuice output-compaction guidance card covering Claude Code's PreToolUse
  wrapper path, Codex hook setup, and realistic savings expectations.
- Obsidian `/note` skill template under `skills/note/` for the `workspace`
  profile.
- `scripts/backup-restic.sh` template, exposed via the `workspace` profile.
- Managed `.gitignore` block: `solo-mise init` now creates or updates a
  `# >>> solo-mise gitignore block >>>` section in the target's `.gitignore`.
  Re-runs replace only the content between the markers, so user-authored rules
  are preserved. Skip with `--no-gitignore`.
- Release pipeline: `.github/workflows/publish.yml` builds an sdist + wheel on
  every `v*` tag and pushes to PyPI.
- CI matrix: `install-from-source` smoke now runs against all six profiles
  (`repo`, `workspace`, `openclaw`, `hermes`, `generic`, `publisher`).
- Project meta: `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, and
  `.github/ISSUE_TEMPLATE/` (bug, profile-init-fails, ingester-misclassified).

### Changed
- Deepened the `workspace` profile's bootstrap files (`AGENTS.md`, `CLAUDE.md`,
  `IDENTITY.md`, `SOUL.md`, `HEARTBEAT.md`, `MEMORY.md`, `SAFETY_RULES.md`,
  `TOOLS.md`, `USER.md`, `INSTALL_FOR_AGENTS.md`).
- README: centered banner, refreshed badges, added a sample `doctor` run,
  noted that solo-mise makes no network calls, called out `init` idempotency.
- CI now pins the `content-guard` checkout to `v0.1.1` instead of tracking the
  default branch.
- `solo-mise init --profile hermes` prints a louder experimental-status notice
  on stderr in addition to the post-install note.

### Removed
- Stale `DREAMS.md` from the repo root and lingering references in templates.

## [0.1.0] - 2026-05-13

Initial release.

### Added
- `solo-mise` CLI with `init`, `doctor`, `scrub`, and `handoff-template`
  subcommands.
- Six profiles: `repo` (default), `workspace`, `openclaw`, `hermes`,
  `generic`, `publisher`.
- Conservative handoff ingester at `.claude/memory-handoffs/`: safe card
  handoffs become cards, targeted updates append, ambiguous material is
  kicked out for review.
- Content-guard pre-push hook for public-leak prevention.
- Sanitized bootstrap file set, starter memory cards, routing rules.
- OpenClaw adapter fragments and harness-aware doctor checks.
- Experimental Hermes adapter fragments.

[Unreleased]: https://github.com/escoffier-labs/brigade/compare/v0.8.1...HEAD
[0.8.1]: https://github.com/escoffier-labs/brigade/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/escoffier-labs/brigade/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/escoffier-labs/brigade/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/escoffier-labs/brigade/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/escoffier-labs/brigade/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/escoffier-labs/brigade/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/escoffier-labs/brigade/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/escoffier-labs/brigade/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/escoffier-labs/brigade/releases/tag/v0.1.0
