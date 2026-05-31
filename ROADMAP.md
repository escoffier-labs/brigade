# Brigade Roadmap

Brigade is being built as a practical daily workflow first, then a portable setup other people can adapt. The core direction is an organized version of real agent work: one command to start, predictable local artifacts, reviewable memory handoffs, and enough inspection to trust the loop during normal work.

## Foundation: Daily Driver

Status: in progress.

- Local dogfood defaults live in gitignored `.brigade/dogfood.toml`.
- `brigade work bootstrap` prepares a repo for the daily loop.
- `brigade work brief` is the start-of-day entrypoint.
- `brigade work run` wraps a dogfood run in local work-session artifacts and handoffs.
- `brigade work tasks` plus `brigade work task add/show/done` provide a gitignored local task ledger.
- `brigade work run --queue-next` queues extracted follow-up work without duplicating equivalent pending tasks.
- `brigade work import add/list/show/promote` gives scanners and wrappers a stable local inbox for candidate work.
- `brigade work phases` provides a gitignored execution ledger for long unattended multi-phase work, including no-silent-compression checks, completion evidence requirements, range status, review closeouts, compare checks, commit reconciliation, privacy scans, handoff drafts, goal scaffolds, action queues, evidence attachments, verification matrices, daily-driver candidates, report closeouts, report compare checks, AFK execution sessions, checkpoint-aware session next/resume recovery, session risk summaries, session verification rollups, session checkpoints, session checkpoint inspection and compare checks, checkpoint issue imports, recovery notes and closeouts, checkpoint-aware daily candidates, daily checkpoint writes, session activity timelines, session progress summaries, session blocker imports, session gates, session reports, daily session candidates, inbox issue routing, and release/operator evidence. Status: strengthened through phase 236 session verification rollup.

## Foundation: Scanner-Ready Inbox

Status: active.

Goal: make Brigade a safe target for local automations that discover useful work.

- Keep raw scanner output private and gitignored under `.brigade/work/imports/`.
- Normalize imports into small records with `kind`, `source`, text, timestamps, and metadata.
- Document the scanner JSONL contract so external producers can target Brigade without importing Brigade internals.
- Validate and ingest scanner-authored JSONL files.
- Let wrappers import candidate tasks, findings, decisions, preferences, incidents, links, and commands without knowing Brigade internals.
- Convert memory-care refresh queues into local task imports. Status: implemented, including `memory-refresh` candidates with task metadata and acceptance.
- Promote selected imports into the work task ledger, with source metadata preserved. Status: implemented with task `type`, `priority`, `template`, and `acceptance` preservation plus reviewed promote-and-run.
- Dismiss noisy imports so scanners can be useful without leaving permanent queue clutter. Status: started with single-item dismissal and filtered `dismiss --all`.
- Batch-promote reviewed imports by source and kind. Status: started with source, kind, and metadata filters across list, triage, promote, and dismiss.
- Surface pending imports and grouped counts in `brigade work brief` so discovered work appears in the daily flow. Status: implemented with scanner candidate surfacing and `brigade work inbox`.
- Warn on stale, noisy, or incomplete scanner queues. Status: started in `brigade work doctor`.
- Keep scanner producer ingestion idempotent so repeated chat and memory sweeps skip equivalent pending or promoted imports, and dismissed items stay dismissed until source fingerprints change. Status: implemented for chat-sweep and memory-refresh producers.
- Describe local scanner producers and plan safe schedules without executing them. Status: implemented with gitignored `.brigade/scanners.toml`, `brigade work scanners`, daily brief visibility, work doctor checks, and scanner-health imports.
- Explicitly execute configured local scanner producers with foreground receipts, due-run selection, output snapshots, and pending import count reporting. Status: implemented with `brigade work scanners run`, `run --all`, `run --due`, `runs`, `run-show`, and scanner execution health imports, without a daemon or automatic promotion.
- Preserve scanner run provenance through the import inbox and keep the queue tidy. Status: implemented with scanner provenance metadata, explicit `--ingest-output` JSONL ingestion, `brigade work import provenance`, `brigade work inbox doctor`, and `brigade work inbox archive`.
- Run a reviewed daily scanner sweep as one explicit operator action. Status: implemented with `brigade work sweep`, `brigade work sweeps`, `brigade work sweep-show`, default configured JSONL output ingestion, local sweep reports, brief visibility, and doctor warnings, without a daemon or automatic promotion.
- Review scanner sweep results without digging through raw receipts. Status: implemented with `brigade work sweep-review`, created import references, skipped and dismissed fingerprints, grouped triage, suggested next commands, closeout health, and broken-reference inbox doctor checks.
- Promote durable non-task scanner imports into reviewed Memory Handoff drafts. Status: implemented with `brigade work import plan-handoff`, `promote-handoff`, lint-gated handoff writing, provenance preservation, redaction, daily brief surfacing, and stale handoff-ready inbox warnings, without editing canonical memory.
- Keep reviewed Memory Handoff drafts visible until the operator closes them out. Status: implemented with `brigade handoff list`, `show`, and `archive`, draft queue health in `work brief` and `work doctor`, and promoted-import handoff reference checks in `work inbox doctor`, without running the canonical ingestor.
- Connect local handoff ingestor outcomes back to draft review state. Status: implemented with normalized receipts under `.brigade/handoffs/ingest-runs/`, `brigade handoff runs`, `run-show`, `reconcile`, ingestion status in draft list/show, archive outcome metadata, and daily-loop warnings for stale unreconciled drafts, without running the ingestor.
- Run explicit multi-harness code review producers and route findings through the scanner inbox. Status: implemented with gitignored `.brigade/reviews.toml`, `brigade work review`, local receipts under `.brigade/reviews/runs/`, normalized `code-review` findings, daily brief surfacing, and work doctor checks, without automatic fixes or remote mutation.
- Close out imported review findings against downstream work. Status: implemented with `brigade work review findings`, `finding-show`, `closeout`, source-fingerprint re-review detection, task review evidence, and daily-loop warnings for unclosed review runs.
- Close out completed work against local verification evidence. Status: implemented with `brigade work verify plan/run/runs/show` and `brigade work closeout`, collecting task acceptance, test command receipts, scanner sweep state, code review closeout state, handoff draft status, and session evidence without CI or remote mutation.
- Check local release readiness before publish operations. Status: implemented with `brigade release plan`, `doctor`, `run`, `runs`, `show`, `schema`, `ci`, and `smoke`, collecting work closeout, verification, review, scanner, security, handoff, content-guard, local CI platform deprecation evidence, install smoke matrix receipts, docs, changelog, roadmap, git-state evidence, and wrapper-facing schema manifests without pushing, tagging, or mutating remotes.
- Build local release candidate packets before manual publish steps. Status: implemented with `brigade release candidate plan/build/list/show/audit/compare/import-issues/closeout/archive`, local candidate bundles, evidence JSON, release notes drafts with secret-looking value redaction, command-contract fingerprints, manual-only publish plans, candidate health warnings, audit import routing, and no remote mutation.
- Audit roadmap closure and neutral pattern coverage. Status: started with `brigade roadmap audit`, `brigade roadmap patterns`, stale phase warnings, documented command drift checks, roadmap-audit imports, and source-pattern decision records without public reference names.
- Inspect local repo fleet readiness without copying private repo contents. Status: started with gitignored `.brigade/repos.toml`, `brigade repos init/list/show/scan/doctor/import-issues`, `brigade repos discover plan`, `brigade repos health-commands`, `brigade repos sweep plan/run/runs/show/closeout`, `brigade repos report plan/build/list/show/archive/closeout`, `brigade repos actions plan/build/list/show/start/done/defer/archive`, `brigade repos actions dispatch`, `brigade repos actions reconcile`, `brigade repos actions context`, `brigade repos release plan/build/list/show/compare/closeout/archive`, `brigade repos release actions`, `brigade repos release evidence`, `brigade repos release reconcile`, `brigade repos release summary`, `brigade repos release report/matrix/checklist/hygiene/import-issues/ready/activity/manifest/audit`, and `brigade repos release waivers`, using safe metadata summaries, dry-run configured-root discovery, receipt-backed optional health command registry, explicit fleet evidence sweeps, local fleet reports, reviewed fleet action queues, per-repo work import dispatch, dispatch supersede reports, action-scoped context packs, fleet release train bundles, release matrix reports, manual publish evidence records, expirable owner-labeled waiver-aware ready gates, AGENTS and fallback guidance detection, repo-fleet imports, and daily-loop health surfacing.
- Close out scanner sweeps after review. Status: started with `brigade work sweep closeout <sweep-id|latest>`, deferred import recording, missing-reference blocking, and inbox hygiene warnings for unclosed sweeps.
- Inspect local operator state from one read-only center view and expose an agent-facing daily driver over it. Status: strengthened with `brigade center status/activity/reviews/templates/schema`, `brigade center report plan/build/list/show/archive/review/compare/diff/closeout`, `brigade center actions plan/build/list/show/doctor/import-issues/start/done/defer/archive`, `brigade center readiness plan/closeout/list/show/import-issues`, and `brigade daily init/status/plan/review/run/closeout/history/show/doctor/schema/approvals/resume/repair/unblock/protocol/telemetry/hardening`, aggregating local work, imports, sweeps, reviews, handoffs, tools, learning, context packs, projects, security, release state, docs command contracts, waivers, and manual-only readiness checklists into JSON, wrapper-facing schema manifests, reviewed action queues, action aging policy imports, report diff receipts, closeouts, local report bundles, config-gated daily receipts, reusable approval requests, explainable planning, recovery metadata, local telemetry, phase-aware production-hardening audits, release-readiness evidence, and one bounded safe daily action loop without a daemon or server.

## Later Phase: Chat Surface Scanners

Goal: support the common places agent work happens without making any one chat product mandatory.

- Build adapters for Discord, Slack, ClickClack, Telegram, and export-based chat archives as separate scanner layers.
- Convert surface-specific events into the local import inbox instead of writing memory directly.
- Summarize private chat evidence, do not quote raw third-party messages into public docs or handoffs.
- Use promotion gates so only reviewed, durable, or actionable items become tasks or memory handoffs.
- Keep source metadata such as workspace, channel, thread, message range, and confidence local unless explicitly exported.
- Maintain a local provider registry for OpenClaw, Peter S, Vincent, and other chat plugins instead of hardcoding one product list. Seeded channel families include Discord, Slack, ClickClack, Telegram, WhatsApp, Signal, iMessage, BlueBubbles, Google Chat, Microsoft Teams, Matrix, Mattermost, Nextcloud Talk, Feishu, Line, QQ bot, Zalo, Nostr, IRC, Twitch, Tlon, Google Meet, voice-call transcripts, webhooks, and QA channels.
- Import nightly memory sweep `issues` into Brigade with `brigade work import chat-sweep`. Status: implemented for the local producer contract, including actionable task imports, wrapper JSON counts, source metadata, idempotency, and raw-chat privacy filtering.
- Describe local chat export surfaces and normalize safe exported findings into scanner inbox imports. Status: implemented with `.brigade/chat-surfaces.toml`, `brigade chat surfaces`, `brigade chat sweep validate/ingest/import-issues`, canonical provider fixtures and aliases for Discord, Slack, Telegram, ClickClack, and generic JSONL, task promotion, handoff promotion, scanner sweep compatibility, plus default raw-chat rejection.
- Add scheduler rules that spread memory ingest, crawler repair, chat sweeps, and OpenClaw updater jobs around update windows so upgrades do not race plugin or extension loads. Status: started with local scanner schedule planning and conflict warnings, without cron mutation or daemon execution.

## Later Phase: Backup And Recovery Visibility

Goal: make backup health part of the same daily operator loop as chat, memory, and work imports.

- Track restic backups to both NAS and cloud destinations.
- Surface latest NAS snapshot age, latest cloud snapshot age, prune result, `restic check` result, and restore rehearsal date. Status: started with read-only local backup summary contracts and `brigade work backup`.
- Send compact private backup summaries to the operator chat/status surface, including Discord or ClickClack when configured.
- Route stale snapshot, failed check, failed prune, missing mount, and restore-rehearsal overdue signals into `brigade work import` as incidents. Status: started with `backup-health` imports and source fingerprints.
- Close out reviewed backup risk without hiding changed issues. Status: strengthened with `brigade work backup closeout`, raw versus active issue counts, quieted reviewed counts, changed fingerprint surfacing, and restore rehearsal evidence in release readiness.
- Keep real hostnames, remote names, mount paths, webhook URLs, channel ids, and backup passwords out of public templates. Status: started with unsafe summary field warnings and public docs.

## Later Phase: Shared Tool Catalog And Runtime

Goal: make Brigade able to reason about callable tools across agent harnesses without making each harness own separate tool config.

- Build a local tool catalog abstraction with source records, tool counts, search, describe, and call surfaces. Status: started with gitignored `.brigade/tools.toml` and read-only `brigade tools` discovery commands.
- Support source families such as MCP, OpenAPI, GraphQL, local scripts, and custom adapters through a registry contract. Status: started for `skill`, `slash-command`, `superpower`, `mcp`, `openapi`, `graphql`, `script`, and `custom` catalog entries.
- Materialize reviewed harness projections from one local source of truth. Status: started with explicit `brigade tools plan` and `brigade tools apply`, managed projection fingerprints, dry-run support, and unmanaged or locally edited conflict protection.
- Prefer schema-first tool descriptions so agents can discover by intent, inspect arguments, and produce typed calls. Status: started with `brigade tools describe`, `brigade tools contracts`, and read-only `brigade tools call plan`.
- Track resumable executions for tools that pause for auth, approval, or human confirmation. Status: started with local non-executing call approval records in `.brigade/tools/calls.jsonl` and explicit checkpoint review/resume through `.brigade/tools/checkpoints/`.
- Execute reviewed local tool calls only behind explicit approval gates. Status: started with `brigade tools call run` for approved `script` calls and approved local `mcp` stdio calls, local receipts under `.brigade/tools/runs/`, and `brigade tools run list/show/latest/replay` for receipt review and replay queueing without direct reruns.
- Add a local daemon option with status, stop, restart, port tracking, and safe local auto-start for commands that need a runtime. Status: started with explicit local runtime supervision through `brigade tools runtime`, without auto-start from doctor, brief, or work run.
- Keep shared auth, secrets, and policy decisions host-local and gitignored, while publishing only safe example configs. Status: started with `.brigade/tools/policy.toml`, policy-gated call planning and execution, and env label bindings from the current process environment.
- Expose catalog health through `brigade doctor` and route broken source/auth/policy states into `brigade work import`. Status: started through `brigade work brief`, `brigade work doctor`, and `tool-catalog` imports.
- Build local portable tool evidence bundles and reviewed sync plans. Status: strengthened with `brigade tools pack build/list/show/archive`, `brigade tools sync plan/apply`, and `brigade tools parity status/closeout` for reviewed projection parity receipts, dry-run sync by default, managed projection conflict safety, changed-fingerprint resurfacing, and release evidence for pack freshness, sync blockers, approvals, run history, and checkpoints.

## Later Phase: Context, Projects, And Learning

Goal: make context preparation, project consolidation, and self-learning local, explicit, and reviewable.

- Build local context engineering packs for task, repo, release, and tool-use scenarios. Status: started with `brigade context plan/build/list/show/archive`, `brigade context sync plan/record`, `brigade context doctor/import-issues`, safe summaries, task acceptance, recent evidence, private evidence exclusions, read-only configured harness sync planning receipts, and reviewable context freshness imports.
- Audit configured related project records without cloning or mutating remotes. Status: started with gitignored `.brigade/projects.toml`, `brigade projects audit/import-issues`, `brigade projects readiness plan/record/list/show`, `brigade projects closeout/closeouts/closeout-show`, decision records for bake-in, integrate, catalog-only, move-candidate, and leave-alone, plus manual-only migration readiness and closeout receipts.
- Aggregate local learning candidates without self-modification. Status: started with `brigade learn plan/doctor/import-issues`, `brigade learn closeout/closeouts/closeout-show`, `brigade learn replay export/list/show/compare`, candidate routing into the scanner inbox, raw import text avoidance, accepted-risk or dismissal quieting, changed-fingerprint resurfacing, and safe replay compare receipts.

## Later Phase: Cybersecurity Plugin

Goal: ship a Brigade cybersecurity plugin with broad coverage for agent workspaces, then go deeper on Brigade's multi-harness, memory, scanner, and dogfood workflows.

Baseline coverage targets:

- Scan agent workspace configs for hardcoded secrets, exposed tokens, private keys, database URLs, and unsafe environment-variable handling.
- Audit tool permissions for broad mutable access, wildcard shell access, missing deny lists, dangerous flags, destructive git commands, and unrestricted network commands.
- Analyze hooks and startup automation for command injection, remote execution, data exfiltration, silent failures, package installs, container escape, reverse shells, clipboard access, log tampering, and persistence behaviors.
- Audit MCP server configs for high-risk server types, remote transports, shell metacharacters, unpinned `npx` usage, hardcoded env secrets, sensitive file args, excessive server counts, missing timeouts, and auto-approve behavior. Status: started with structural JSON MCP checks for transports, auto-approval, `npx`, shell metacharacters, env secrets, sensitive or broad args, high-risk commands, server count, and timeouts.
- Review agent prompts, skills, subagents, slash commands, and workspace instructions for prompt-injection patterns, hidden instructions, URL execution, data harvesting, output suppression, time bombs, and unsafe auto-run language. Status: started with guardrail surfaces for repo guidance, skills, slash commands, subagents, and tool wrappers, including template confidence handling.
- Emit graded reports with severity, category scores, evidence snippets, suggested fixes, JSON output, markdown output, SARIF output, HTML or bundle output, and CI-friendly exit codes. Status: started with redacted JSON, Markdown, and SARIF evidence bundles, stable finding ids, rule ids, safe excerpts, and remediation hints.
- Support CLI use, GitHub Action use, and local evidence packs.
- Add optional threat-intel enrichment, including MISP as an opt-in provider, without changing the default no-network local scan behavior. Status: started with explicit `brigade security enrich`, offline local enrichment, MISP provider config, and separate enrichment artifacts.

Brigade-specific additions:

- Scan Claude Code, Codex, OpenCode, Gemini, Hermes, OpenClaw, VS Code, Zed, dmux, and generic repo-local agent harness surfaces with explicit runtime-confidence labels.
- Understand Brigade installs: `.brigade/`, `.codex/`, `.claude/`, memory handoff inboxes, roster files, dogfood configs, run artifacts, work imports, memory-care decay files, and public template folders.
- Treat public-template findings differently from active runtime findings so docs and starter templates do not score like live credentials or enabled tools.
- Integrate with `brigade doctor` as a security station and with `brigade work import` so findings can become reviewable local tasks instead of only console output. Status: started with doctor checks, work brief and work doctor checks, `--import-findings`, source `security-scan` imports, dedupe, and dismissed-import protection.
- Provide safe auto-fix only for narrow cases such as replacing obvious hardcoded sample secrets, tightening generated allow-list examples, or adding missing ignore rules. Status: started with `brigade security fix` for local artifact directory, managed `.gitignore` hygiene, `brigade security template-audit` for public template and docs privacy checks, and parser-derived command inventory checks through `brigade roadmap commands --write` and `--check`.
- Produce Memory Handoffs for durable security findings while keeping raw secret evidence redacted.
- Add policy packs for personal dogfooding, public-repo release checks, CI gates, and strict enterprise workspaces. Status: started with `personal`, `public-repo`, `ci`, and `strict`, local scan profiles for `public-repo`, `internal-workspace`, and `local-only-audit`, plus policy-pack closeout evidence in release readiness.
- Include dependency and package-manager hardening checks for agent plugin ecosystems, MCP packages, skills, and local tool wrappers. Status: started with package scripts, GitHub Actions refs and permissions, Python URL dependencies, and legacy install hooks.
- Enrich reviewed indicators and suspicious package or domain findings through optional providers such as MISP, then route enriched findings into local evidence bundles and work imports. Status: started with `security-enrichment.json`, `security-enrichment.md`, and review/doctor visibility.
- Track false-positive taxonomy, runtime-confidence rules, suppressions, and regression fixtures as first-class project artifacts. Status: started with `brigade security findings`, `show`, `review`, reasoned suppressions, unsuppress, and stale-suppression doctor warnings.
- Close out reviewed security findings and accepted risk. Status: started with `brigade security closeout` and local receipts that preserve safe finding ids, fingerprints, suppressions, and accepted-risk status.

## Active Phase: Issue And TDD Work Loop

Goal: make Brigade support a narrow issue lifecycle for daily work: pick one task, define acceptance, test first when practical, implement, review, refactor, and close.

- Add task templates for vertical-slice work, bugfix work, RED/GREEN/REFACTOR loops, docs work, and security follow-ups. Status: implemented in the local task ledger.
- Import GitHub issues into the local task ledger without building a sync engine. Status: implemented through the existing `gh` CLI, including issue-body acceptance extraction.
- Let `brigade work run` consume structured acceptance criteria from the local task ledger or a GitHub issue mirror. Status: started with local ledger acceptance criteria and issue-body criteria imported into the ledger.
- Record completed task evidence locally. Status: started with consumed task snapshots in work-session artifacts, completion metadata for session path, dogfood run path, acceptance criteria, hardened acceptance rollups, review finding outcomes, and release candidate task outcome evidence.
- Keep repo-shareable workflow rules separate from gitignored personal/global preferences. Status: implemented with public-safe `rules/issue-tdd-loop.md` and `rules/acceptance-driven-work.md` install templates plus `brigade work doctor` visibility.
- Add doctor checks for missing acceptance criteria or stale active issue context. Status: started with missing acceptance, closed remote issues, unchecked issue-backed tasks, stale active sessions, and `brigade work import issue-repairs` for local repair imports.

First build slice:

- Create a plugin scaffold and security scan contract. Status: started with built-in `security` station, `brigade security init`, and `brigade security scan`.
- Start with config discovery and read-only reporting for Brigade, Claude Code, Codex, and MCP config files. Status: started, including structural `mcpServers` checks for JSON configs.
- Add core rule categories for secrets, permissions, hooks, MCP servers, supply-chain patterns, and agent instructions. Status: started.
- Output JSON plus readable text, redacted evidence bundles, then route selected findings into `brigade work import`. Status: started with `--output-dir`, doctor evidence status, and `--import-findings`.
- Keep all raw findings local and gitignored unless the operator explicitly exports an evidence pack. Status: current default.
- Add local policy defaults, stable finding fingerprints, and suppressions. Status: started with `.brigade/security.toml`.

## Later Phase: Memory Card Decay And Refresh

Goal: prevent durable memory from silently rotting.

- Track freshness metadata, confidence, evidence, and review dates for memory cards. Status: started with memory-care metadata coverage summaries, missing reviewed-date issues, missing freshness-date issues, confidence counts, evidence metadata counts, reviewed imports, and planning-only safe metadata repair output.
- Run memory-care scanners that detect expired, stale, contradictory, or undersourced cards. Status: implemented with `brigade memory care scan`, local `.brigade/memory-care.toml`, and stable refresh queue output.
- Import refresh candidates into Brigade as local work imports. Status: implemented with `brigade memory care import-issues`, source `memory-care`, safe metadata, source fingerprints, and dismissed-import protection.
- Promote refresh candidates into tasks or memory handoffs after review. Status: implemented through the existing work import promotion and acceptance/evidence loop.
- Auto-fix only within safe gates where source evidence is current, low-risk, and locally reviewable. Status: started with `brigade memory care plan-fixes`, which writes no card files and reports blockers for reviewed/freshness metadata repair candidates.
- Treat bootstrap truncation as a hard failure. Bootstrap files stay slim, cards hold durable detail, and doctor checks enforce the boundary.
- Add a handoff doctor that compares repo-local writer inboxes such as `.claude/memory-handoffs/` and `.codex/memory-handoffs/` against the canonical ingestor source list, warning when handoffs exist in directories the owner is not scanning. Status: started with `brigade handoff doctor`, `.brigade/handoff-sources.example.json`, and `brigade doctor` / `brigade work doctor` integration.
- Add handoff-ingest observability checks for hidden warning states, including unreachable remote sources, malformed handoffs that are skipped, and runs that emit `NO_REPLY` despite warnings. Status: started with optional `ingestor.last_run_log` checks in `brigade handoff doctor`, hardened issue parsing, and normalized reconcile receipts for skipped, failed, malformed, unreachable-source, and no-reply states.
- Turn handoff-ingest warnings into repairable local work. Status: started with `brigade handoff issues`, `brigade handoff import-issues`, repair guidance, and `brigade work brief` issue counts.
- Catch handoff action/target mismatches before ingest. Status: started with `brigade handoff lint`, doctor warnings, issue imports, and template guidance that forces card and document handoffs into mutually exclusive branches.
- Keep the daily brief quiet after fixes land. Status: started with `brigade handoff sync-issues`, known issue suppression in `work brief`, stale handoff-ingest task/import cleanup, and fingerprinted source-coverage imports that stay dismissed until coverage changes.

## Later Phase: Portable Operator Setup

Goal: keep the system usable by the original operator while making it adaptable by others.

- Keep Codex-first defaults, with Claude Code, OpenCode, Hermes, OpenClaw, and generic harness paths supported through writer-specific inboxes.
- Make local paths configurable and gitignored.
- Provide templates for fresh-start users without publishing private workspace state.
- Keep public repo docs focused on patterns, commands, and safety contracts.
- Leave release, tag, push-to-main, and production-impacting actions behind explicit approval gates.

## Active Phase Queue: Roadmap Completion Hardening

Status: active.

The detailed working queue for phases 61-100 lives in [`docs/phase-61-100-plan.md`](docs/phase-61-100-plan.md). The queue focuses on roadmap audit precision, deferred-item ownership, command documentation contracts, cross-producer provenance, privacy regression coverage, chat export hardening, backup and tool closeouts, context and learning receipts, security report compatibility, issue/TDD repair imports, memory and handoff hardening, release evidence schemas, operator-center schemas, fleet release reports, CI platform warnings, install smoke receipts, public template privacy, and a final local operator readiness closeout.
