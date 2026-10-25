# Agent Guide: Installing and Adapting Brigade

You are a coding agent. **You** install, set up, verify, and leave handoffs. The human usually does not type these commands. They point you at this repo (or paste a short prompt) and expect files and a doctor result on disk.

Developing Brigade itself? Start from root `AGENTS.md`, then `CONTRIBUTING.md`. That file is for contributors, not for wiring a user workspace. To prove a change by driving the CLI, follow [`registry/skills/verify-brigade/SKILL.md`](../registry/skills/verify-brigade/SKILL.md) (Launch / Doctor / Drive / Evidence / Cleanup / Feature map).

## What Brigade is for

Brigade coordinates coding agents across machines and cloud seats. Agents leave receipts and pass evidence-based handoffs so another agent can check their work.

- **Agents run:** install, `setup`, `operator quickstart`, `work verify`, handoffs, `code`, `evidence`.
- **Humans own:** policy, budgets, scopes, and gates. The operator can inspect or stop work and choose where approval is needed.
- **Artifacts:** local receipts, run journals, memory cards, and configs remain inspectable across harnesses. Optional services coordinate fleet work and serve views or MCP resources. See [`docs/execution-model.md`](execution-model.md).

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
If brigade is missing, install it with npm install --global
@escoffier-labs/brigade@0.30.0 only when Node.js 22+ and npm are present and
the target is glibc Linux x64/ARM64, macOS x64/ARM64, or Windows x64.
Otherwise use pipx install brigade-cli (or uv tool install brigade-cli),
including on musl Linux (such as Alpine) and Windows ARM64.
Run operator quickstart with --dry-run first and show the plan. Then apply,
run brigade operator doctor, and report ready: yes. Keep existing memory
layout. Do not touch remotes, do not commit, stop before anything destructive.
```

Brigade coordinates autonomous agent work across machines and cloud seats through local receipts, run journals, and evidence-based handoffs. It also serves memory and skills over MCP and syncs tool and skill projections across harnesses. The operator chooses the scope and gates. Adapt the user's existing memory, handoff, and agent workflow, keeping their local records inspectable.

## Installing for a user (you run this)

Platform: Linux, macOS, or native Windows PowerShell. Use the npm package only when Node.js 22 or newer and npm are already present and the target is glibc Linux x64/ARM64, macOS x64/ARM64, or Windows x64. It needs no Python first and fetches a managed Python on first launch. On other targets, including musl Linux (such as Alpine) and Windows ARM64, or without Node.js 22+ and npm, use Python 3.10 or newer with pipx or uv. If `pipx` is missing, use the platform package instructions in [`QUICKSTART.md`](../QUICKSTART.md#1-install). Do not assume WSL is required. On an npm install, upgrade with npm, not `brigade update`.

Work in the **target** directory (the repo or operator workspace the user named), not the Brigade source tree unless they asked to develop Brigade.

Always dry-run before write. Then apply. Then doctor.

```bash
npm install --global @escoffier-labs/brigade@0.30.0
# alternatives: pipx install brigade-cli, or uv tool install brigade-cli
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

From then on, every repo the agent opens gets the work brief injected at session start, and an unwired repo gets the exact `brigade init` command printed for the agent to run. The home directory is never treated as a work target. Pass `--target <wired-workspace>` to pin `hook-run` to one workspace; omit it to stay multi-repo. Agents can wire new repos themselves and close the verified-work loop without another human command. Remove it with `brigade work hooks uninstall --scope user`. Only Brigade-owned hook entries are touched.

## After install: the agent work loop

Once doctor is healthy, **you** (the coding agent) should prefer:

```bash
brigade work verify run --manifest <path> --capture <id>
brigade code impact <symbol>    # when a change has blast radius
brigade evidence search "<query>"  # when you need prior runs or claims
```

`--command` and `--argv-json` receipts are audit-only. When dirty paths match a tracked manifest under `verify/manifests/`, `suggested_command` should name that manifest. See [outcome scoring](outcome-scoring.md).

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

Do not start daemons, install schedulers, publish, push, tag, deploy, mutate remotes, install hooks, or run destructive commands as part of Brigade setup unless the user explicitly asks. Setup writes local wiring and reports its checks. Long-running services and scheduled care can be enabled separately within the user's authorized scope. Boundary: [`docs/execution-model.md`](execution-model.md).

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
status: ok
ready: yes
blocking_issues: 0
```

If anything remains manual, list the remaining steps clearly and do not hide warnings.
