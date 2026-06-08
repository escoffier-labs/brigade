# Brigade Announcement Draft

Brigade is a local-first operator CLI for agent workspaces.

It is for people who already have a real, homegrown agent setup: memory handoffs, repo instructions, local tools, shell scripts, scheduled jobs, security scans, and a daily loop that grew out of actual work. Brigade does not try to replace that with a hosted control plane. It helps you make the setup explicit, reviewable, portable, and safer to adapt.

Install:

```bash
pipx install brigade-cli
brigade --version
```

Start with a repo:

```bash
brigade operator quickstart --target ./my-repo --harnesses codex --dry-run
brigade operator quickstart --target ./my-repo --harnesses codex
brigade operator doctor --target ./my-repo --profile local-operator
```

Start with an existing operator workspace:

```bash
brigade operator adopt plan --target ~/agent-workspace --json
brigade operator adopt capture --target ~/agent-workspace --json
brigade operator migration status --target ~/agent-workspace --json
brigade operator surfaces capture --target ~/agent-workspace --json
brigade operator surfaces reviews --target ~/agent-workspace --json
```

What works today:

- First-run setup for repo and workspace targets.
- Cross-harness memory handoff folders for Codex, Claude Code, OpenCode, Hermes, and OpenClaw-style workspaces.
- A daily driver that picks one safe local action at a time.
- Redacted adoption snapshots for existing setups.
- Redacted external surface tracking for shell crontab, OpenClaw cron, and PM2.
- Migration rollups that keep replacement work batched instead of scattered across tiny tasks.
- Local security scans for plaintext passwords, API keys, tokens, private keys, risky harness wiring, and prompt-injection patterns.
- Research handoff export for turning completed local research runs into reviewed Memory Handoff drafts.
- Release/readiness evidence that stays local until the operator chooses to publish.

What it does not do automatically:

- It does not start daemons.
- It does not install schedulers or hooks without an explicit command.
- It does not publish, push, tag, or mutate remotes.
- It does not ingest memory into a canonical store by itself.
- It does not store raw scheduler lines, process names, job names, command paths, environment values, host details, or secrets in public docs.
- It does not rotate credentials or rewrite your secret storage. It reports redacted findings and gives response options.

Why I built it:

Agent setups become real infrastructure quickly. Once multiple tools, memories, schedules, scanners, and handoff folders exist, the problem is no longer "how do I run one command?" The problem is "how do I keep this whole operator setup understandable and adaptable without leaking private details or hiding risky changes in chat?"

Brigade is the local layer for that.

The current dogfood path is simple: Brigade is being used to adapt an existing operator workspace into Brigade-managed evidence. It discovered external scheduler/process surfaces, captured them as redacted records, reviewed ownership at the surface level, routed migration follow-ups into the daily loop, and rolled the remaining replacement work into one migration status view.

If your setup is already messy because it is real, Brigade is meant for you.

Project:

```text
https://github.com/escoffier-labs/brigade
```
