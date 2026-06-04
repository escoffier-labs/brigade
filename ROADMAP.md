# Brigade Roadmap

Brigade is being built as a practical daily workflow first, then a portable setup other people can adapt. The core direction is an organized version of real agent work: one command to start, predictable local artifacts, reviewable memory handoffs, and enough inspection to trust the loop during normal work.

## How to read this roadmap

Every section below has the same shape: a plain-English summary in a quote block, then the detailed technical bullets with exact command names and status tags. If the bullets read like buzzword soup, the quote block is the human version. Read that first.

A few words show up everywhere:

- **harness**: an AI agent program (Claude Code, Codex, OpenClaw, Hermes).
- **operator**: you, the human running the agents.
- **handoff**: a memory note an agent writes to be saved long-term.
- **ingest**: reading those notes and filing them into permanent memory.
- **scanner**: an automation that goes looking for useful work (in chat, backups, code, and so on).
- **import / inbox**: a holding queue where found work waits for your review.
- **promote**: move an item out of the queue into a real task or memory note.
- **receipt**: a local file logging that something happened, kept for audit and proof.
- **closeout**: marking something reviewed or done so it stops nagging you.
- **gate**: a manual approval checkpoint; nothing risky happens without your yes.
- **AFK**: away from keyboard, a long unattended run the agent does solo.
- **dogfood**: Brigade being used on itself or another trusted repo.

The one rule behind all of it: Brigade writes local files and queues, but it never publishes, edits canonical memory, runs background daemons, or touches remote servers on its own. Everything waits for an explicit command.

## Foundation: Daily Driver

> **In plain terms:** the everyday loop. `brigade work brief` each morning shows what is up, `brigade work run` does a chunk of work and auto-saves its artifacts and a memory note, and there is a private local to-do list that never gets committed. The long run-on bullet below is just the full feature list for long unattended runs; it boils down to "log every step in checkpoints so you can see what happened and resume if it crashes."

Status: in progress.

- Local dogfood defaults live in gitignored `.brigade/dogfood.toml`.
- `brigade work bootstrap` prepares a repo for the daily loop.
- `brigade work brief` is the start-of-day entrypoint.
- `brigade work run` wraps a dogfood run in local work-session artifacts and handoffs.
- `brigade work tasks` plus `brigade work task add/show/done` provide a gitignored local task ledger.
- `brigade work run --queue-next` queues extracted follow-up work without duplicating equivalent pending tasks.
- `brigade work import add/list/show/promote` gives scanners and wrappers a stable local inbox for candidate work.
- `brigade work phases` provides a gitignored execution ledger for long unattended multi-phase work, including no-silent-compression checks, completion evidence requirements, range status, review closeouts, compare checks, commit reconciliation, privacy scans, handoff drafts, goal scaffolds, action queues, evidence attachments, verification matrices, daily-driver candidates, report closeouts, report compare checks, AFK execution sessions, checkpoint-aware session next/resume recovery, wrapper-safe session protocol output, session self-audit, session risk summaries, session verification, privacy, and handoff rollups, session checkpoints, session checkpoint inspection, compare, archive checks, checkpoint issue imports, recovery notes and closeouts, checkpoint-aware daily candidates, daily checkpoint writes, session activity timelines, session progress summaries, session blocker imports, session gates, session reports with recovery evidence, session health schema manifests, daily session candidates, inbox issue routing, and release/operator evidence with session compare drift checks. Status: strengthened through phase 250 AFK hardening closeout.

## Foundation: Plan-First Operator Loop

> **In plain terms:** make the "think first, then build" habit explicit. Big tasks should produce a local plan before execution, deep work can start with a plan for making the plan, and raw links, transcripts, screenshots, or errors should enter as reviewable context instead of trusted instructions. The operator still chooses what moves forward; Brigade just keeps the plan, receipts, and resume points tidy.

Status: in progress.

Goal: make Brigade the provider-neutral plan, receipt, and review layer for agentic engineering workflows across Codex, Claude Code, OpenClaw, Hermes, and similar harnesses.

- Treat `brigade work task plan <id>` as the visible planning step for non-trivial local tasks, with plan artifacts that capture source context, assumptions, acceptance criteria, risks, next safe command, and receipt paths. Status: implemented (plan artifacts: `work task plan --write` writes plan.md + JSON receipt under .brigade/work/plans/, `work plans` lists them).
- Add a "plan for the plan" mode for deep work that writes a local meta-plan and stops before producing the final deliverable, so research synthesis, roadmap design, release planning, docs work, and memory-care repair plans do not skip the thinking step. Status: implemented (`work task plan --write --meta` writes a meta-plan with a do-not-jump banner; `--step` captures steps).
- Route raw context such as transcripts, article links, screenshots, terminal errors, chat exports, and issue text through the scanner/import inbox as untrusted local context before promotion into tasks, research runs, handoff drafts, or docs updates. Status: proposed extension of the scanner-ready inbox. Status: implemented (`brigade work import context` frames input via the untrusted-context helper and flags injection signals for review).
- Feed `brigade research run` reports into task planning as opt-in current-context evidence, keeping trusted local corpora first and web findings quarantined as untrusted source material. Status: implemented (`work task plan --write --from-research <run-id>` attaches the report as quarantined untrusted-web evidence).
- Surface plan state in `brigade work brief`, `brigade daily status`, operator reports, and release readiness so significant pending or completed work without a plan artifact is visible before review. Status: implemented (`work doctor` warns and `work brief` reports significant pending tasks without a plan artifact).
- Let repeated accepted plans and completed runs become draft workflow templates, repo rules, or skills through reviewed local proposals, without auto-installing or editing canonical memory. Status: implemented (`brigade work plan-promote <id> --as template|rule|skill` writes a local draft proposal; never installs).
- Preserve Brigade's deliberate friction: plan artifacts, imports, receipts, and review queues are local by default; publish, remote mutation, canonical memory edits, background daemons, and risky tool execution still require explicit operator commands.

## Foundation: Scanner-Ready Inbox

> **In plain terms:** make it safe for automations to drop "here is some work" into Brigade. A scanner finds something, it lands in a private inbox, and it sits there until you review and promote it. The rest is validating, deduping, grouping, dismissing noise, and turning keepers into tasks or memory notes. No daemon, no auto-promotion: things only move when you run a command.

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
- Inspect local operator state from one read-only center view and expose an agent-facing daily driver over it. Status: strengthened with `brigade center status/activity/reviews/templates/schema`, `brigade center report plan/build/list/show/archive/review/compare/diff/closeout`, `brigade center actions plan/build/list/show/doctor/import-issues/start/done/defer/archive`, `brigade center readiness plan/closeout/list/show/import-issues`, `brigade operator init/status --profile internal-dogfood`, and `brigade daily init/status/plan/review/run/closeout/history/show/doctor/schema/approvals/resume/repair/unblock/protocol/telemetry/hardening`, aggregating local work, imports, sweeps, reviews, handoffs, tools, learning, context packs, projects, security, release state, docs command contracts, waivers, and manual-only readiness checklists into JSON, wrapper-facing schema manifests, reviewed action queues, action aging policy imports, report diff receipts, closeouts, local report bundles, repo-local dogfood bootstrap/status, config-gated daily receipts, reusable approval requests, explainable planning, recovery metadata, local telemetry, phase-aware production-hardening audits, release-readiness evidence, and one bounded safe daily action loop without a daemon or server.

## Later Phase: Chat Surface Scanners

> **In plain terms:** pull work items out of chat apps (Discord, Slack, Telegram, and so on). It summarizes private messages instead of pasting raw chat into public docs, and events become inbox items rather than direct memory writes. The long platform list is just "we do not want to hardcode one chat product."

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

> **In plain terms:** fold backup health into the same daily dashboard. Show how old your latest NAS and cloud snapshots are, whether the integrity check passed, and when you last test-restored. Stale or broken backups become "incident" items in the inbox.

Goal: make backup health part of the same daily operator loop as chat, memory, and work imports.

- Track restic backups to both NAS and cloud destinations.
- Surface latest NAS snapshot age, latest cloud snapshot age, prune result, `restic check` result, and restore rehearsal date. Status: started with read-only local backup summary contracts and `brigade work backup`.
- Send compact private backup summaries to the operator chat/status surface, including Discord or ClickClack when configured.
- Route stale snapshot, failed check, failed prune, missing mount, and restore-rehearsal overdue signals into `brigade work import` as incidents. Status: started with `backup-health` imports and source fingerprints.
- Close out reviewed backup risk without hiding changed issues. Status: strengthened with `brigade work backup closeout`, raw versus active issue counts, quieted reviewed counts, changed fingerprint surfacing, and restore rehearsal evidence in release readiness.
- Keep real hostnames, remote names, mount paths, webhook URLs, channel ids, and backup passwords out of public templates. Status: started with unsafe summary field warnings and public docs.

## Later Phase: Shared Tool Catalog And Runtime

> **In plain terms:** one master list of every tool your agents can call, so each agent does not keep its own separate config. You can search, describe, and call tools from one place. Anything that changes state sits behind an approval gate, secrets stay local, and the optional local runtime never auto-starts.

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

## Later Phase: Shared Skill Registry

> **In plain terms:** skills are reusable workflow instructions, not random prompt snippets. Brigade should keep one reviewed library, then install the same skill into Codex, Claude Code, Gemini, OpenClaw, or an MCP resource shape. Agent-written skill ideas go to an inbox for review instead of becoming instant startup text.

Goal: make skills shareable across harnesses while treating them like code: provenance, linting, permissions, compatibility checks, fingerprints, tests, review, and rollback before installation.

- Maintain a canonical local skill registry where each skill pack has `SKILL.md`, `skill.json` metadata, version, source, required tools, required MCP servers, supported harnesses, trust level, tests, and optional bundled assets or scripts. Status: started with `brigade skills import`, local registry fingerprints, lint checks, and injection-signal warnings.
- Materialize one reviewed skill into multiple harness formats instead of maintaining separate prompt copies. Status: started with `brigade skills install <skill> --target codex|claude|opencode|gemini|openclaw|hermes|mcp|all`, installing Codex, Claude, OpenCode, Gemini `.agents/skills`, OpenClaw, Hermes, and MCP-resource folders with per-harness receipts.
- Keep harness support adapter-based so future targets such as Antigravity, Pi, Cursor, and similar agent surfaces can be added without changing the registry contract. Status: started with `brigade skills adapters init/list/show`, built-in adapter metadata, local adapter overlay config, and planned adapter entries.
- Search and lint skill packs before use. Status: started with `brigade skills search` and `brigade skills lint`.
- Keep the Skills Over MCP direction as an explicit experimental contract. Status: started with `brigade skills serve-mcp`, which reports planned resources such as `skill://registry/{skill_id}/SKILL.md` and tools such as `search_skills`, `get_skill`, `install_skill`, `publish_skill`, `fork_skill`, and `lint_skill` without starting a server yet.
- Publish through reviewed proposals instead of direct sharing. Status: started with `brigade skills publish <skill> --scope local|workspace|team|public`, writing local publish proposals that preserve fingerprint and review state.
- Add an agent skill inbox for proposed new skills or improvements, then lint, diff, fingerprint, and review them before import. Status: started with `brigade skills inbox add/list/show/diff/accept/reject`.
- Add compatibility and version views for Codex, Claude Code, Gemini, OpenClaw, and MCP-native installs, including diffs, changelog, source, and trust score. Status: started with `brigade skills compatibility`; version diffs, changelog, trust score, and richer install history remain planned.
- Add rollback for installed skills. Status: started with rollback snapshots captured on forced reinstall and `brigade skills rollback <skill> --target <harness>`.

## Later Phase: Explicit Runbook Execution

> **In plain terms:** Brigade can run things, but only when the operator asks for a reviewed execution lane. A runbook is a local file with steps. `plan` shows what would run, `run` executes foreground commands with logs and receipts, `resume` points at the next failed step, and `closeout` records review.

Goal: provide a clear execution lane for approved multi-step workflows without turning `doctor`, `brief`, or status views into surprise automation.

- Add a runbook contract with explicit shell steps, per-step cwd, timeouts, stdout/stderr logs, and JSON receipts. Status: started with `brigade runbook plan/run/resume/closeout`.
- Keep runbook execution foreground-only and receipt-backed. Status: started with local receipts under `.brigade/runbooks/runs/`.
- Add approval policy, allowed-command validation, dry-run rendering, retry from failed step, and import routing for failed runbooks. Status: started with approval-required execution, destructive default-deny checks, optional `allowed_commands`, `run --dry-run`, and `run --resume <run-id>`; import routing remains planned.

## Later Phase: Context, Projects, And Learning

> **In plain terms:** three related things. (1) Reusable "context packs" so you do not re-explain a repo to an agent every time. (2) Auditing related side-projects to decide keep, merge, or drop without touching their git. (3) Collecting "lessons learned" candidates, but only as suggestions you review; Brigade never rewrites itself.

Goal: make context preparation, project consolidation, and self-learning local, explicit, and reviewable.

- Build local context engineering packs for task, repo, release, and tool-use scenarios. Status: started with `brigade context plan/build/list/show/archive`, `brigade context sync plan/record`, `brigade context doctor/import-issues`, safe summaries, task acceptance, recent evidence, private evidence exclusions, read-only configured harness sync planning receipts, and reviewable context freshness imports.
- Audit configured related project records without cloning or mutating remotes. Status: started with gitignored `.brigade/projects.toml`, `brigade projects audit/import-issues`, `brigade projects readiness plan/record/list/show`, `brigade projects closeout/closeouts/closeout-show`, decision records for bake-in, integrate, catalog-only, move-candidate, and leave-alone, plus manual-only migration readiness and closeout receipts.
- Aggregate local learning candidates without self-modification. Status: started with `brigade learn plan/doctor/import-issues`, `brigade learn closeout/closeouts/closeout-show`, `brigade learn replay export/list/show/compare`, candidate routing into the scanner inbox, raw import text avoidance, accepted-risk or dismissal quieting, changed-fingerprint resurfacing, and safe replay compare receipts.

## Later Phase: Cybersecurity Plugin

> **In plain terms:** a security scanner built for AI agent setups. It hunts for hardcoded secrets, over-broad tool permissions, dangerous hooks, risky MCP configs, and prompt-injection traps in agent instructions, then grades the findings. It scores a sample secret in a template differently from a live credential, so docs do not trigger false alarms. Threat-intel enrichment is opt-in and offline by default.

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

- Scan Claude Code, Codex, OpenCode, Gemini, Hermes, OpenClaw, VS Code, Zed, dmux, and generic repo-local agent harness surfaces with explicit runtime-confidence labels. Status: started with cross-harness JSON wiring checks for `.brigade/`, `.claude/`, `.codex/`, `.opencode/`, `.openclaw/`, `.hermes/`, and Brigade templates, including path escapes, host-private paths, broad roots, private or insecure URLs, and shell-like command fields.
- Understand Brigade installs: `.brigade/`, `.codex/`, `.claude/`, memory handoff inboxes, roster files, dogfood configs, run artifacts, work imports, memory-care decay files, and public template folders. Status: started with security scan and doctor summaries for harness wiring, template privacy, local evidence, suppressions, and open findings.
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

> **In plain terms:** support a clean one-task-at-a-time flow: pick a task, write down what "done" means, write the test first when sensible, build, review, close. It can import GitHub issues into the local to-do list using plain `gh`, with no background sync engine.

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

> **In plain terms:** stop memory notes from silently rotting. Track when each note was last reviewed and how well-sourced it is, then flag stale or contradictory ones for refresh. "Bootstrap truncation is a hard failure" means the always-loaded startup files must stay small; if they get bloated and cut off, that is treated as a real error, not a shrug.

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

> **In plain terms:** make it work for other people, not just the original operator. Codex stays the default, but Claude Code, OpenCode, Hermes, and OpenClaw are all supported. Local paths are configurable and gitignored, public docs focus on patterns instead of private workspace state, and anything that publishes stays behind an approval gate.

Goal: keep the system usable by the original operator while making it adaptable by others.

- Keep Codex-first defaults, with Claude Code, OpenCode, Hermes, OpenClaw, and generic harness paths supported through writer-specific inboxes.
- Promote OpenCode to a first-class built-in handoff source. Today only `.claude/memory-handoffs/` and `.codex/memory-handoffs/` are hardcoded in the ingest, doctor, fleet sweep, and security scan source maps; OpenCode handoffs only flow through a manual `--handoff-inbox` flag. Add `.opencode/memory-handoffs/` to the same built-in source maps, ship a template scaffold, and cover it in handoff doctor source-coverage and the repo sweep so OpenCode handoffs route automatically. Status: implemented with .opencode/memory-handoffs/ wired into install scaffolding, ingest, doctor, handoff doctor source coverage, the fleet sweep, the security skip-list, and the interactive selector, plus a template scaffold.
- Make local paths configurable and gitignored.
- Provide templates for fresh-start users without publishing private workspace state.
- Keep public repo docs focused on patterns, commands, and safety contracts.
- Leave release, tag, push-to-main, and production-impacting actions behind explicit approval gates.

## Later Phase: Deep Research Lane

> **In plain terms:** turn a research question into durable, cited memory instead of a throwaway answer. `brigade research run "<question>"` loops gather, read, extract, synthesize, saves progress so a long run survives a crash, and outputs an HTML report plus a memory note. It grounds in your trusted local sources first; the web tier is opt-in and every page is treated as untrusted (it could carry injected instructions), and cost is capped so a run cannot blow up.

Goal: turn open-ended research questions into durable, cited memory instead of one-shot answers, reusing the existing researcher role and the handoff -> card pipeline.

- Add `brigade research run "<question>"` that drives an iterative, LLM-in-the-loop research loop (gather -> read -> extract -> synthesize) using the configured `researcher` model from the roster, not a new hardcoded provider. Status: implemented with `brigade research run/list/show/cancel/resume/open`, local-first trusted sourcing, and an opt-in web tier.
- Use goal-based extraction: for each fetched source, pull only the content relevant to the research goal before synthesis, to keep context small and on-topic. Status: implemented with untrusted-content framing in the extraction prompt.
- Persist runs under `.brigade/research/` with a cancellable, resumable task registry so a long research run survives interruption and reports partial progress, mirroring the work/runs receipt model. Status: implemented with per-round checkpoints, `brigade research resume`, and `brigade research cancel`.
- Emit two artifacts per run: a self-contained visual HTML report and a structured memory handoff, so research output flows straight into the existing ingest -> cards/`.learnings` pipeline and becomes durable, cited memory. Status: implemented with a dependency-free HTML report and a memory handoff that separate trusted-local from untrusted-web provenance.
- Treat every fetched source as untrusted context (see prompt-injection hardening below); web content and tool output are data, not instructions. Status: implemented with an opt-in (`--web`) web tier that is quarantined and labeled untrusted in the report and handoff.
- Bound cost explicitly: per-run search count and per-page content caps, surfaced in the run receipt, with no silent truncation. Status: implemented with configurable round, time, URL, and content caps recorded in the run stats.

## Later Phase: Operator Capabilities Beyond The CLI

> **In plain terms:** the vision past the terminal. The principle: the CLI is the engine, and any future UI is just a window onto the same commands, never a separate codebase. Near-term, still-CLI ideas include prompt-injection protection, blocking risky tools on publicly exposed instances, a dependency-free HTML report renderer, optional on-device memory search, and off-terminal notifications. Longer-term: an actual workspace UI, adopting an existing open-source chat front end, local model-serving guidance, and calendar/email triage. The later items are "planned direction," meaning not committed yet.

Goal: the CLI is just the bones. It is the load-bearing skeleton, the testable, scriptable engine that every capability hangs off, but the destination is a full operator workspace, not a terminal tool. Architecture principle: every higher-level surface (including a future UI) sits on top of a CLI command plus a structured (JSON) contract, so the bones stay authoritative, automatable, and the single source of behavior. The UI becomes a view over the same commands, never a parallel implementation. Near-term items below are CLI-native; later items put a workspace on top of the same bones.

Near-term, CLI-native:

- Prompt-injection hardening. Add an untrusted-context policy helper that tags external content (web results, tool output, retrieved documents, saved memories, skill text) as data-not-instructions before it reaches a model. High value for the research lane and for any ingest path that reads handoffs which could carry injected instructions. Status: implemented with the shared `brigade.untrusted` policy helper (`wrap_untrusted` content-fenced framing + `scan_untrusted` injection signal), adopted in the research extractor and used to gate injection-flagged handoffs to the ingest inbox.
- Owner-scoped tool gating. Complement the existing tool contracts/approvals/runtime policy with owner-tier tool blocking (admin / single-user vs publicly exposed), so a publicly reachable instance refuses high-risk tools by default. Status: proposed.
- Self-contained visual report renderer. A dependency-free styled HTML report (system fonts only, dark/light via `prefers-color-scheme`, auto table of contents, collapsible sources). Reused by the research lane and available to operator-center and release-evidence reports. No remote fonts or CDNs, consistent with the offline and content-guard ethos. Status: proposed.
- Optional semantic memory retrieval. An opt-in local vector + keyword hybrid retrieval layer over `memory/cards/` using on-device embeddings (no external API), with import/export. Stays optional: core memory remains file-first and zero-dependency. Status: proposed.
- Multi-channel operator notifications. A small notification-channel abstraction so backlog and release-gate warnings can reach the operator off-terminal. Status: started with optional `agent-notify` managed-tool health in `brigade doctor`, `brigade notifications status`, `brigade notifications setup plan`, `brigade center status`, `brigade work brief`, and `brigade daily status/plan`, without sending notifications, storing secrets, or writing harness hook config from status/planning flows.

The workspace on top of the bones:

- A workspace UI that is a view over the CLI: side-by-side blind model comparison with synthesis, an assisted document editor, and a viewer for the deep-research reports. Each panel maps to an existing command and its JSON output. Status: planned direction.
- Agent-interaction chat GUI: prefer adopting a mature, MIT-licensed open-source agent front end (the OpenCode terminal agent, already supported as a writer harness) rather than building a chat surface from scratch. Brigade supplies the operator-system bones beneath it (memory, ingest, scanners, tool contracts, publish guards); the front end is the conversation surface. Target its in-progress v2 line, and keep the integration a thin adapter so the bones stay the source of behavior. Status: planned direction; evaluate against v2.
- Local-first, hardware-aware model selection and serving guidance, surfaced in the workspace and scriptable from the CLI. Status: planned direction.
- Personal-data surfaces such as calendar and email triage as Brigade becomes a daily workspace, behind the same privacy and approval gates that already govern the CLI. Status: planned direction.

## Active Phase Queue: Roadmap Completion Hardening

> **In plain terms:** not new features, this is the polish and cleanup backlog: tighten audits, fix documentation drift, add privacy regression tests, finish closeouts. The actual checklist lives in the linked plan file.

Status: active.

The detailed working queue for phases 61-100 lives in [`docs/phase-61-100-plan.md`](docs/phase-61-100-plan.md). The queue focuses on roadmap audit precision, deferred-item ownership, command documentation contracts, cross-producer provenance, privacy regression coverage, chat export hardening, backup and tool closeouts, context and learning receipts, security report compatibility, issue/TDD repair imports, memory and handoff hardening, release evidence schemas, operator-center schemas, fleet release reports, CI platform warnings, install smoke receipts, public template privacy, and a final local operator readiness closeout.
