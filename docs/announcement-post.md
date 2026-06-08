# Brigade Announcement Post

## Long Version

Brigade is a local-first operator CLI for agent workspaces.

It is for people who already have a real, homegrown agent setup: memory handoffs, repo instructions, local tools, shell scripts, scheduled jobs, security scans, and a daily loop that grew out of actual work.

Brigade does not try to replace that with a hosted control plane. It helps you make the setup explicit, reviewable, portable, and safer to adapt.

What works now:

- first-run setup for repo and workspace targets
- cross-harness memory handoff folders for Codex, Claude Code, OpenCode, Hermes, and OpenClaw-style workspaces
- a daily driver that picks one safe local action at a time
- redacted adoption snapshots for existing setups
- redacted external surface tracking for shell crontab, OpenClaw cron, and PM2
- migration rollups that keep replacement work batched instead of scattered across tiny tasks
- local security scans for plaintext passwords, API keys, tokens, private keys, risky harness wiring, and prompt-injection patterns
- research handoff export for turning completed local research runs into reviewed Memory Handoff drafts
- release/readiness evidence that stays local until the operator chooses to publish

The dogfood path is real: Brigade was used to adapt an existing operator workspace into Brigade-managed evidence. It discovered external scheduler/process surfaces, captured 57 redacted records, reviewed every record with zero stale reviews, kept 36 records externally owned, marked 14 as Brigade runbook migration candidates, marked 7 as retirement-review candidates, routed the actionable follow-ups, and closed the batch with local redacted replacement evidence.

It does not start daemons, install schedulers, push to GitHub, publish releases, ingest memory, rotate credentials, or mutate remotes unless you explicitly run the command that does it. It also does not store raw scheduler lines, process names, job names, command paths, environment values, host details, or secrets in public docs.

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

If your setup is already messy because it is real, Brigade is meant for you.

Project: https://github.com/escoffier-labs/brigade

Website: https://brigade.tools

## Short Version

Brigade is a local-first operator CLI for agent workspaces.

It helps turn a real homegrown agent setup, memory handoffs, repo instructions, local tools, scheduled jobs, security scans, and daily work loops, into something explicit, reviewable, portable, and safer to adapt.

The current dogfood run captured 57 redacted external scheduler/process records, reviewed every one, kept 36 externally owned, marked 14 as Brigade runbook migration candidates, marked 7 as retirement-review candidates, and closed the replacement batch without exposing raw scheduler lines, job names, process names, command paths, environment values, host details, or secrets.

Install:

```bash
pipx install brigade-cli
brigade operator quickstart --target ./my-repo --harnesses codex --dry-run
```

Project: https://github.com/escoffier-labs/brigade

Website: https://brigade.tools

## Launch Checklist

- Public repo release readiness: ready
- Release blockers: 0
- Release warnings: 0
- Pre-push Content Guard: passed
- Replacement migration doctor: ready
- Pending replacement imports: 0
- Pending replacement tasks: 0
