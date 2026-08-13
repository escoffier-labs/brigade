# Agent-Assisted Setup

Point a coding agent at the Brigade repository (or paste the prompt below). **The agent installs and wires Brigade.** Humans often never type `pipx` or `brigade setup`.

Root `AGENTS.md` is for **developing Brigade**. For install and adapt, agents follow **`docs/agents-guide.md`** (this doc is supporting detail).

## Paste-ready install prompt

```text
Read https://github.com/escoffier-labs/brigade and follow docs/agents-guide.md.
Install brigade-cli with pipx if missing (or uv tool install brigade-cli).
Run operator quickstart with --dry-run first and show the plan. Then apply,
run brigade operator doctor, and report ready: yes. Keep existing memory
layout. Do not touch remotes, do not commit, stop before anything destructive.
```

Brigade is designed so agents run the control plane (install, setup, verify, handoffs) and humans review when a gate is ambiguous or risky. Adapt an existing homegrown setup. Do not replace it wholesale. Keep the user's memory owner, workspace or repo layout, harness choices, and local habits unless Brigade needs a small compatibility file or handoff inbox.

Treat setup as local workspace wiring, not as a release, deploy, or remote mutation. Local-first means data on the operator-controlled machine first (laptop, workstation, or VPS) before any external service.

Built-in engines use public names **code map** (`brigade code`) and **evidence log** (`brigade evidence`). Historical product names GraphTrail and MiseLedger may still appear in engine paths and MCP entry labels.

## Agent entry point

If an agent has access to this repository, it should start by reading:

1. `README.md`
2. `docs/agents-guide.md`
3. `docs/agent-assisted-setup.md` (this file)
4. `docs/new-user-quickstart.md` if useful

Then work inside the **target** repo or operator workspace and run:

```bash
pipx install brigade-cli
# alternative: uv tool install brigade-cli
brigade setup
brigade --version
brigade operator quickstart --target . --harnesses codex --dry-run
brigade operator quickstart --target . --harnesses codex
brigade operator doctor --target . --profile local-operator
```

For an OpenClaw or Hermes workspace instead of a code repo, use workspace depth:

```bash
brigade operator quickstart --target . --depth workspace --harnesses openclaw,hermes --owner openclaw --dry-run
brigade operator quickstart --target . --depth workspace --harnesses openclaw,hermes --owner openclaw
brigade operator doctor --target . --profile local-operator
```

## What The Agent Should Do

The agent should:

- install the `brigade-cli` package if missing (humans often will not)
- run `brigade setup` so the code map and evidence engines are present
- run quickstart in dry-run mode first
- apply quickstart only after the dry-run looks reasonable
- run `operator doctor` and report the exact result
- prefer `brigade work verify run` for checks after wiring, not raw test claims
- explain which files are shareable or durable and which are local-only
- preserve the user's existing memory layout and agent conventions where possible (including OpenClaw `SOUL.md`, `TOOLS.md`, `AGENTS.md`, `IDENTITY.md`, `MEMORY.md`, and related bootstrap files)
- suggest Brigade compatibility wiring instead of moving or renaming personal systems
- stop and ask before remote changes, destructive commands, new services, schedulers, or commits

The agent should not:

- treat `.brigade/` as public repo or portable workspace content
- paste raw scanner output that may contain secrets
- rewrite permanent memory files unless the user asked for that exact edit
- force the user into Brigade's example layout when they already have a working setup
- push, tag, publish, deploy, or install hooks without explicit approval

## Adapting A Homegrown Setup

Many users already have a personal version of this workflow: memory files, agent instructions, project notes, handoff folders, scripts, scheduled checks, or tool-specific command docs. Brigade should make that setup easier to reuse across agents.

When adapting an existing setup, the agent should:

- run `brigade operator adopt plan --target . --json` before changing files when the target may already have scripts, handoff folders, crons, or process managers
- run `brigade operator adopt capture --target . --json` after reviewing the plan if the user wants Brigade to keep a local redacted adoption receipt
- run `brigade operator adopt import-issues --target . --json` to route migration gaps into the work inbox instead of tracking them only in chat
- run `brigade operator migration status --target . --json`, `brigade operator migration doctor --target . --json`, and `brigade operator migration consolidate --target . --surface shell_crontab --review-status needs-owner` to see whether Brigade can drive the remaining replacement work from redacted local evidence and avoid tiny record-level task slices once a rollup exists
- run `brigade operator surfaces capture --target . --json`, `brigade operator surfaces doctor --target . --json`, `brigade operator surfaces review --target . --surface shell_crontab --status external-ok --all --reason reviewed-external-ownership`, and `brigade operator surfaces import-issues --target . --json` when external scheduler or process coverage should be tracked as redacted local evidence
- inventory current files such as `AGENTS.md`, `CLAUDE.md`, `MEMORY.md`, `TOOLS.md`, `.codex/`, `.claude/`, `.opencode/`, `.antigravity/`, `.pi/`, `.cursor/`, `.aider/`, `.goose/`, `.continue/`, `.copilot/`, `.qwen/`, `.kimi/`, `.adal/`, `.openhands/`, `.grok/`, `.amp/`, `.crush/`, `.hermes/`, and `.openclaw/`
- identify the memory owner before changing memory rules
- keep repo-shareable or durable workspace files separate from generated local projections
- use `brigade operator quickstart --dry-run` to preview compatibility files
- run `brigade operator doctor` after setup and report what remains manual
- leave existing working conventions intact unless the user approves a migration

Good adaptation usually means adding a handoff inbox, a shared instruction file, portable tool sources, scanner config, or a redacted surface registry. It should not mean flattening the user's system into someone else's exact directory tree. Do not paste raw scheduler lines, job names, process names, private paths, hostnames, or environment values into public docs or issues.

## Harness Selection

Use the harness list that matches the tools the user actually runs:

```bash
brigade operator quickstart --target . --harnesses codex
brigade operator quickstart --target . --harnesses claude
brigade operator quickstart --target . --harnesses opencode
brigade operator quickstart --target . --harnesses antigravity
brigade operator quickstart --target . --harnesses pi
brigade operator quickstart --target . --harnesses cursor
brigade operator quickstart --target . --harnesses codex,claude,opencode,antigravity,pi,cursor,aider,goose,continue,copilot,qwen,kimi,adal,openhands,grok,amp,crush
brigade operator quickstart --target . --depth workspace --harnesses openclaw,hermes --owner openclaw
```

OpenClaw and Hermes can act as memory owners or writer surfaces depending on the user's setup. If the agent is unsure, it should start with the harness the user is currently using and report the next command needed to add another surface later.

## Claude Code user-scope work hooks

Install the verified-work loop once at user scope instead of wiring every repo by hand:

```bash
brigade work hooks install --scope user
```

Session start then injects the work brief. An unwired repo prints the exact `brigade init` command for the agent to run. Uninstall with `brigade work hooks uninstall --scope user`. Only Brigade-owned hook entries are touched.

## Success Check

A healthy first run should end with:

```text
quickstart: ok
operator doctor: ready yes
blocking issues: 0
```

The minimum verification to report back is:

```bash
brigade --version
brigade operator doctor --target . --profile local-operator
```

For troubleshooting, collect safe machine-readable output:

```bash
brigade operator quickstart --target . --harnesses codex --json
brigade operator doctor --target . --profile local-operator --json
brigade tools doctor --target . --json
brigade skills doctor --target . --json
```

Open a setup issue at:

```text
https://github.com/escoffier-labs/brigade/issues/new/choose
```

Use the "Quickstart setup problem" form and include the redacted `issue_report`.
