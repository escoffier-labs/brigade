# Agent Guide: Installing and Adapting Brigade

You are a coding agent. **You** install, set up, verify, and leave handoffs. The human usually does not type these commands. They point you at this repo (or paste a short prompt) and expect files and a doctor result on disk.

Developing Brigade itself? Start from root `AGENTS.md`, then `CONTRIBUTING.md`. That file is for contributors, not for wiring a user workspace.

## What Brigade is for

Brigade is a **local control plane for coding agents**, not a human day-to-day terminal app.

- **Agents run:** install, `setup`, `operator quickstart`, `work verify`, handoffs, `code`, `evidence`.
- **Humans own:** policy and review when a gate is ambiguous or risky (or when they explicitly ask for a destructive or remote action).
- **Artifacts:** plain files on the machine (receipts, memory cards, configs). No daemon. No lock-in. See [`docs/execution-model.md`](execution-model.md).

Public names for the built-in engines:

| Public surface | Commands | Historical name |
|---|---|---|
| Code map | `brigade code …` | GraphTrail |
| Evidence log | `brigade evidence …` | MiseLedger |

Standalone GraphTrail or MiseLedger product installs are replaced by `brigade setup`. Some binary and path names still use the historical labels.

## Start here

Read in this order:

1. `README.md` (short landing page)
2. This file (`docs/agents-guide.md`)
3. `docs/agent-assisted-setup.md` (boundaries and adaptation detail)
4. `docs/new-user-quickstart.md` if the target is a first-time human skim

### Paste-ready install prompt

When a human pastes a short job into Claude Code, Codex, Cursor, OpenClaw, or any agent with shell access, use this exact brief:

```text
Read https://github.com/escoffier-labs/brigade and follow docs/agents-guide.md.
Install brigade-cli with pipx if missing (or uv tool install brigade-cli).
Run operator quickstart with --dry-run first and show the plan. Then apply,
run brigade operator doctor, and report ready: yes. Keep existing memory
layout. Do not touch remotes, do not commit, stop before anything destructive.
```

Brigade is local-first workspace wiring. Local-first means data on the operator-controlled machine first (laptop, workstation, or VPS) before any external service. Adapt the user's existing memory, handoff, and agent workflow. Do not replace a working layout with someone else's exact tree.

## Installing for a user (you run this)

Platform: Linux, macOS, or native Windows PowerShell with Python 3.10 or newer. If `pipx` is missing, use the platform package instructions in [`QUICKSTART.md`](../QUICKSTART.md#1-install). Do not assume WSL is required.

Work in the **target** directory (the repo or operator workspace the user named), not the Brigade source tree unless they asked to develop Brigade.

Always dry-run before write. Then apply. Then doctor.

```bash
pipx install brigade-cli
# alternative: uv tool install brigade-cli
brigade setup
brigade --version
brigade operator quickstart --target . --harnesses <harness> --dry-run
# show the plan to the user if they are watching; then apply
brigade operator quickstart --target . --harnesses <harness>
brigade operator doctor --target . --profile local-operator
```

Replace `<harness>` with what they use (for example `codex`, `claude`, `cursor`). If unsure, use the current harness and say how to add more later.

### OpenClaw or Hermes workspace (not a code repo)

Prefer workspace depth and an explicit memory owner:

```bash
brigade operator quickstart --target . --depth workspace --harnesses openclaw,hermes --owner openclaw --dry-run
brigade operator quickstart --target . --depth workspace --harnesses openclaw,hermes --owner openclaw
brigade operator doctor --target . --profile local-operator
```

OpenClaw bootstrap files often include `SOUL.md`, `TOOLS.md`, `AGENTS.md`, `IDENTITY.md`, `MEMORY.md`, and related session-start files. Preserve them. Oversize bootstrap sets are a Bootstrap Doctor concern (`brigade add bootstrap-doctor` / bootstrap-doctor CLI), not something to silently truncate.

### Multiple harnesses

```bash
brigade operator quickstart --target . --harnesses codex,claude,opencode,antigravity,pi,cursor,aider,goose,continue,copilot,qwen,kimi,adal,openhands,grok,amp,crush
```

### Cursor GUI work loop at user scope

Cursor GUI agents need user-level wiring in addition to a repository handoff inbox. Preview, then apply:

```bash
brigade harness install cursor --scope user --dry-run
brigade harness install cursor --scope user --write
brigade harness doctor cursor --scope user
```

This profile manages a local plugin rule, the global `brigade-work` skill, one `sessionStart` hook, and MCP entries for Brigade plus the code-map and evidence engines (often still labeled `graphtrail` / `miseledger` in native Cursor config). It preserves unrelated plugins, hooks, MCP servers, and sibling JSON fields. Existing values with a managed name are reported as conflicts instead of being replaced. Reload Cursor windows after a successful write.

Uninstall is ownership-aware:

```bash
brigade harness uninstall cursor --scope user --dry-run
brigade harness uninstall cursor --scope user --write
```

### Claude Code user-scope work hooks

Instead of wiring repos one at a time, install the work-loop hooks once at user scope:

```bash
brigade work hooks install --scope user
```

From then on, every repo the agent opens gets the work brief injected at session start, and an unwired repo gets the exact `brigade init` command printed for the agent to run. Agents can wire new repos themselves and close the verified-work loop without another human command. Remove it with `brigade work hooks uninstall --scope user`. Only Brigade-owned hook entries are touched.

## After install: the agent work loop

Once doctor is healthy, **you** (the coding agent) should prefer:

```bash
brigade work verify run --target . --command "<real check>" --capture brigade-work
brigade code impact <symbol>    # when a change has blast radius
brigade evidence search "<query>"  # when you need prior runs or claims
```

Do not claim tests passed without a real exit code. Prefer Brigade-wrapped verify over raw test commands when the project wires the work loop.

## Adapting existing setups

Before changing files, inventory what is already there:

- `AGENTS.md`, `CLAUDE.md`, `MEMORY.md`, `TOOLS.md`, `SOUL.md`, `IDENTITY.md` (OpenClaw and friends)
- harness dirs: `.codex/`, `.claude/`, `.cursor/`, `.openclaw/`, `.hermes/`, and the rest listed in the previous inventory

Preserve the user's memory owner, conventions, repo layout, and tool-specific docs when possible. Prefer adding compatibility wiring (handoff inboxes, shared instructions, portable tool sources, scanner config).

Do not force Brigade's example layout when they already have a working homegrown setup. Do not assume the target must be a git repo; an OpenClaw/Hermes memory workspace or VPS operator directory is valid.

When the target may already have scripts, handoffs, crons, or process managers, use adopt before rewrite:

```bash
brigade operator adopt plan --target . --json
# only after review:
# brigade operator adopt capture --target . --json
```

## Local vs shareable files

Usually safe to commit after review:

- `AGENTS.md`, `MEMORY.md` and reviewed memory cards if this repo owns memory
- `rules/`, `tools/`, public docs
- the managed handoff template for each selected writer harness. Claude onboarding requires `.claude/memory-handoffs/TEMPLATE.md`, and `verify-harness` fails readiness when a global exclude hides it. Other writers use the matching `<inbox>/TEMPLATE.md`

Handoff inboxes stay local except each inbox's managed `TEMPLATE.md`. Session handoff files in those folders stay ignored.

Usually local-only:

- `.brigade/`
- harness local dirs (`.codex/`, `.claude/`, `.cursor/`, `.openclaw/`, …) except each selected writer's `<inbox>/TEMPLATE.md`
- generated projections and scanner state

Do not commit generated local state unless the user explicitly asks and the docs say it is repo-shareable.

## Safety boundaries

Do not start daemons, install schedulers, publish, push, tag, deploy, mutate remotes, install hooks, or run destructive commands as part of Brigade setup unless the user explicitly asks. Brigade is not a scheduler. An external trigger may call its commands later. Boundary: [`docs/execution-model.md`](execution-model.md).

Do not paste raw scanner output, session text, tokens, API keys, private hostnames, private repo names, or unredacted absolute paths into public issues or docs.

If setup fails, collect machine-readable output and summarize after redaction:

```bash
brigade operator quickstart --target . --harnesses codex --json
brigade operator doctor --target . --profile local-operator --json
brigade tools doctor --target . --json
brigade skills doctor --target . --json
```

Issue form: https://github.com/escoffier-labs/brigade/issues/new/choose

## Success criteria

Report the exact commands you ran. A healthy first run should end with:

```text
quickstart: ok
operator doctor: ready yes
blocking issues: 0
```

If anything remains manual, list the remaining steps clearly and do not hide warnings.
