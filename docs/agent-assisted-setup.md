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

Built-in engines use public names **code map** (`brigade code`) and **evidence log** (`brigade evidence`). The canonical binaries are `brigade-code-graph` and `brigade-evidence`. Existing legacy paths and owned MCP entry labels remain readable through v0.32.

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

Session start then injects the work brief. An unwired repo prints the exact `brigade init` command for the agent to run. The home directory is never a work target. Pin with `--target <wired-workspace>` when the session cwd is not the workspace you want. Uninstall with `brigade work hooks uninstall --scope user`. Only Brigade-owned hook entries are touched.

## Strict, observe and off modes and safety rails

Brigade's default remains `strict`. Hooks route checks through `brigade work verify run`, and the Stop gate asks for a receipt after code changes. `observe` and `off` allow direct checks and session completion without a receipt. Observe does not record raw checks. Recording requires `brigade work verify run`.

```bash
brigade init --target . --depth repo --harnesses claude --observe
brigade work mode set off --target .
brigade work mode set strict --target .
```

`--strict` accepts `--engineer` as an alias. `--off` accepts `--yolo`. Config values and `mode set` accept `engineer` and `enforced` as `strict`, and `yolo` as `off`. Writers store only `strict`, `observe` or `off` in `work.mode` in `.brigade/config.json`, including the default. Init without a flag preserves the recorded mode.

An absent, unreadable or invalid mode still resolves to `strict`. The work brief pins a readable keyless config only when git does not track it. Doctor reports an unpinned config. use `brigade work mode set strict` to pin a tracked one explicitly. Older binaries treat all three canonical names as unknown, fall back to engineer enforcement and warn in doctor. An `off` config shared with an older binary therefore enforces verification there.

All modes keep the brief, recall, memory search, Fleet Hub messages and handoffs on request. All install the same Claude hook events. Each target governs its checks: a strict child repository still requires receipts when reached from an observe or off workspace. Adoption reports `observe` and `off` by name and never counts their sessions as bypassed. `brigade work verify run` remains available in every mode.

Safety rails are a separate switch, on by default in every mode. They are the content guard, secret and credential scanning, and the pre-push publication checks:

```bash
brigade work rails off --target .   # prints what stops being checked
brigade work rails on --target .
```

Turning rails off makes Brigade's pre-push hook skip its scans. You lose the check that stops secrets, credentials, private hostnames, and private paths from leaving the machine, including the history scan of commits that a clean tip would hide. Guard policies and baselines stay untouched, so `rails on` restores the same checks.

The rails-off choice is per user and per repository. `brigade work rails off` records it in your own Brigade config directory, outside every repository (`$XDG_CONFIG_HOME/brigade/work-rails.json` on Linux, `~/Library/Application Support/brigade/` on macOS, `%LOCALAPPDATA%\brigade\` on Windows). The record names the repository by its real path, with symlinks resolved and, on Windows and macOS, case folded. It turns rails off for that exact path only, never for a parent, a sibling, or another clone. Run it from the repository root, because the pre-push hook checks the root. Nothing inside a repository can turn the rails off, so a pushed commit cannot disable leak checks for anyone who clones it. A `work.rails` key in `.brigade/config.json`, which earlier builds wrote, is ignored. `brigade doctor` and `brigade work rails show` warn when one is present. Enforced mode with rails off is allowed, for a private repo that wants receipts and never publishes.

## Success Check

A healthy first run should end with:

```text
status: ok
ready: yes
blocking_issues: 0
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

Codex observe capture installs project hooks in `.codex/hooks.json` when you
run `brigade work mode set observe` with Codex selected. Trust the project
`.codex/` layer, then review and trust the hook definition in Codex `/hooks`.
An installed file alone does not mean the hooks are trusted or enabled.
`brigade work mode show --json` reports installation under `observe_hooks.codex`.
Codex check status and duration remain unknown because the finished-tool
response has no documented exit-status or duration contract. Agent type remains
unknown. Unknown statuses must be excluded from pass-rate denominators.
Leaving observe mode or running `brigade work hooks uninstall` removes only
Brigade's Codex entries. The installed command pins capture to its project.
Strict and off modes do not record these observed checks.

Cursor capture is deferred to its own pull request because a CLI version cannot
establish compatibility for every editor and shared-project reader. Its redesign
must avoid version probes and unsupported events that invalidate hook documents.
