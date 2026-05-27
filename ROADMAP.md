# Brigade Roadmap

Brigade is being built as a practical daily workflow first, then a portable setup other people can adapt. The core direction is an organized version of real agent work: one command to start, predictable local artifacts, reviewable memory handoffs, and enough inspection to trust the loop during normal work.

## Current Phase: Daily Driver

Status: in progress.

- Local dogfood defaults live in gitignored `.brigade/dogfood.toml`.
- `brigade work bootstrap` prepares a repo for the daily loop.
- `brigade work brief` is the start-of-day entrypoint.
- `brigade work run` wraps a dogfood run in local work-session artifacts and handoffs.
- `brigade work tasks` plus `brigade work task add/show/done` provide a gitignored local task ledger.
- `brigade work run --queue-next` queues extracted follow-up work without duplicating equivalent pending tasks.
- `brigade work import add/list/show/promote` gives scanners and wrappers a stable local inbox for candidate work.

## Next Phase: Scanner-Ready Inbox

Status: active.

Goal: make Brigade a safe target for local automations that discover useful work.

- Keep raw scanner output private and gitignored under `.brigade/work/imports/`.
- Normalize imports into small records with `kind`, `source`, text, timestamps, and metadata.
- Document the scanner JSONL contract so external producers can target Brigade without importing Brigade internals.
- Validate and ingest scanner-authored JSONL files.
- Let wrappers import candidate tasks, findings, decisions, preferences, incidents, links, and commands without knowing Brigade internals.
- Convert memory-care refresh queues into local task imports.
- Promote selected imports into the work task ledger, with source metadata preserved.
- Dismiss noisy imports so scanners can be useful without leaving permanent queue clutter.
- Batch-promote reviewed imports by source and kind.
- Surface pending imports and grouped counts in `brigade work brief` so discovered work appears in the daily flow.

## Later Phase: Chat Surface Scanners

Goal: support the common places agent work happens without making any one chat product mandatory.

- Build adapters for Discord, Slack, ClickClack, Telegram, and export-based chat archives as separate scanner layers.
- Convert surface-specific events into the local import inbox instead of writing memory directly.
- Summarize private chat evidence, do not quote raw third-party messages into public docs or handoffs.
- Use promotion gates so only reviewed, durable, or actionable items become tasks or memory handoffs.
- Keep source metadata such as workspace, channel, thread, message range, and confidence local unless explicitly exported.
- Maintain a local provider registry for OpenClaw, Peter S, Vincent, and other chat plugins instead of hardcoding one product list. Seeded channel families include Discord, Slack, ClickClack, Telegram, WhatsApp, Signal, iMessage, BlueBubbles, Google Chat, Microsoft Teams, Matrix, Mattermost, Nextcloud Talk, Feishu, Line, QQ bot, Zalo, Nostr, IRC, Twitch, Tlon, Google Meet, voice-call transcripts, webhooks, and QA channels.
- Import nightly memory sweep `issues` into Brigade with `brigade work import chat-sweep`. Status: started with `.brigade/chat-memory-sweep.example.json`, OpenClaw cron fragment, and a command that converts sweep issues into local work imports.
- Add scheduler rules that spread memory ingest, crawler repair, chat sweeps, and OpenClaw updater jobs around update windows so upgrades do not race plugin or extension loads.

## Later Phase: Backup And Recovery Visibility

Goal: make backup health part of the same daily operator loop as chat, memory, and work imports.

- Track restic backups to both NAS and cloud destinations.
- Surface latest NAS snapshot age, latest cloud snapshot age, prune result, `restic check` result, and restore rehearsal date.
- Send compact private backup summaries to the operator chat/status surface, including Discord or ClickClack when configured.
- Route stale snapshot, failed check, failed prune, missing mount, and restore-rehearsal overdue signals into `brigade work import` as incidents.
- Keep real hostnames, remote names, mount paths, webhook URLs, channel ids, and backup passwords out of public templates.

## Later Phase: Cybersecurity Plugin

Goal: ship a Brigade cybersecurity plugin with broad coverage for agent workspaces, then go deeper on Brigade's multi-harness, memory, scanner, and dogfood workflows.

Baseline coverage targets:

- Scan agent workspace configs for hardcoded secrets, exposed tokens, private keys, database URLs, and unsafe environment-variable handling.
- Audit tool permissions for broad mutable access, wildcard shell access, missing deny lists, dangerous flags, destructive git commands, and unrestricted network commands.
- Analyze hooks and startup automation for command injection, remote execution, data exfiltration, silent failures, package installs, container escape, reverse shells, clipboard access, log tampering, and persistence behaviors.
- Audit MCP server configs for high-risk server types, remote transports, shell metacharacters, unpinned `npx` usage, hardcoded env secrets, sensitive file args, excessive server counts, missing timeouts, and auto-approve behavior. Status: started with structural JSON MCP checks for transports, auto-approval, `npx`, shell metacharacters, env secrets, sensitive or broad args, high-risk commands, server count, and timeouts.
- Review agent prompts, skills, subagents, slash commands, and workspace instructions for prompt-injection patterns, hidden instructions, URL execution, data harvesting, output suppression, time bombs, and unsafe auto-run language.
- Emit graded reports with severity, category scores, evidence snippets, suggested fixes, JSON output, markdown output, HTML or bundle output, and CI-friendly exit codes. Status: started with redacted JSON and Markdown evidence bundles.
- Support CLI use, GitHub Action use, and local evidence packs.
- Add optional threat-intel enrichment, including MISP as an opt-in provider, without changing the default no-network local scan behavior. Status: started with explicit `brigade security enrich`, offline local enrichment, MISP provider config, and separate enrichment artifacts.

Brigade-specific additions:

- Scan Claude Code, Codex, OpenCode, Gemini, Hermes, OpenClaw, VS Code, Zed, dmux, and generic repo-local agent harness surfaces with explicit runtime-confidence labels.
- Understand Brigade installs: `.brigade/`, `.codex/`, `.claude/`, memory handoff inboxes, roster files, dogfood configs, run artifacts, work imports, memory-care decay files, and public template folders.
- Treat public-template findings differently from active runtime findings so docs and starter templates do not score like live credentials or enabled tools.
- Integrate with `brigade doctor` as a security station and with `brigade work import` so findings can become reviewable local tasks instead of only console output. Status: started with doctor checks, work doctor checks, and `--import-findings`.
- Provide safe auto-fix only for narrow cases such as replacing obvious hardcoded sample secrets, tightening generated allow-list examples, or adding missing ignore rules. Status: started with `brigade security fix` for local artifact directory and managed `.gitignore` hygiene.
- Produce Memory Handoffs for durable security findings while keeping raw secret evidence redacted.
- Add policy packs for personal dogfooding, public-repo release checks, CI gates, and strict enterprise workspaces. Status: started with `personal`, `public-repo`, and `strict`.
- Include dependency and package-manager hardening checks for agent plugin ecosystems, MCP packages, skills, and local tool wrappers. Status: started with package scripts, GitHub Actions refs and permissions, Python URL dependencies, and legacy install hooks.
- Enrich reviewed indicators and suspicious package or domain findings through optional providers such as MISP, then route enriched findings into local evidence bundles and work imports. Status: started with `security-enrichment.json`, `security-enrichment.md`, and review/doctor visibility.
- Track false-positive taxonomy, runtime-confidence rules, suppressions, and regression fixtures as first-class project artifacts. Status: started with `brigade security review`, reasoned suppressions, unsuppress, and stale-suppression doctor warnings.

## Later Phase: Issue And TDD Work Loop

Goal: make Brigade support a narrow issue lifecycle for daily work: pick one task, define acceptance, test first when practical, implement, review, refactor, and close.

- Add task templates for vertical-slice work, bugfix work, and RED/GREEN/REFACTOR loops.
- Let `brigade work run` consume structured acceptance criteria from the local task ledger or a GitHub issue mirror.
- Keep repo-shareable workflow rules separate from gitignored personal/global preferences.
- Add doctor checks for missing acceptance criteria or stale active issue context.

First build slice:

- Create a plugin scaffold and security scan contract. Status: started with built-in `security` station, `brigade security init`, and `brigade security scan`.
- Start with config discovery and read-only reporting for Brigade, Claude Code, Codex, and MCP config files. Status: started, including structural `mcpServers` checks for JSON configs.
- Add core rule categories for secrets, permissions, hooks, MCP servers, supply-chain patterns, and agent instructions. Status: started.
- Output JSON plus readable text, redacted evidence bundles, then route selected findings into `brigade work import`. Status: started with `--output-dir`, doctor evidence status, and `--import-findings`.
- Keep all raw findings local and gitignored unless the operator explicitly exports an evidence pack. Status: current default.
- Add local policy defaults, stable finding fingerprints, and suppressions. Status: started with `.brigade/security.toml`.

## Later Phase: Memory Card Decay And Refresh

Goal: prevent durable memory from silently rotting.

- Track freshness metadata, confidence, evidence, and review dates for memory cards.
- Run memory-care scanners that detect expired, stale, contradictory, or undersourced cards.
- Import refresh candidates into Brigade as local work imports.
- Promote refresh candidates into tasks or memory handoffs after review.
- Auto-fix only within safe gates where source evidence is current, low-risk, and locally reviewable.
- Treat bootstrap truncation as a hard failure. Bootstrap files stay slim, cards hold durable detail, and doctor checks enforce the boundary.
- Add a handoff doctor that compares repo-local writer inboxes such as `.claude/memory-handoffs/` and `.codex/memory-handoffs/` against the canonical ingestor source list, warning when handoffs exist in directories the owner is not scanning. Status: started with `brigade handoff doctor`, `.brigade/handoff-sources.example.json`, and `brigade doctor` / `brigade work doctor` integration.
- Add handoff-ingest observability checks for hidden warning states, including unreachable remote sources, malformed handoffs that are skipped, and runs that emit `NO_REPLY` despite warnings. Status: started with optional `ingestor.last_run_log` checks in `brigade handoff doctor`.

## Later Phase: Portable Operator Setup

Goal: keep the system usable by the original operator while making it adaptable by others.

- Keep Codex-first defaults, with Claude Code, OpenCode, Hermes, OpenClaw, and generic harness paths supported through writer-specific inboxes.
- Make local paths configurable and gitignored.
- Provide templates for fresh-start users without publishing private workspace state.
- Keep public repo docs focused on patterns, commands, and safety contracts.
- Leave release, tag, push-to-main, and production-impacting actions behind explicit approval gates.
