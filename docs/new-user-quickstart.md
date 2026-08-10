# New User Quickstart

Brigade is local-first. Local-first means local data on the operator-controlled machine first, before any external service; that machine can be a laptop, workstation, or VPS. The first run should create local config and handoff inboxes without starting services or touching remotes; workspace, `--full`, and pack-based installs can also project portable tools or skills.

The target can be a code repo, an OpenClaw or Hermes memory workspace, a VPS operator directory, or another local workspace you control. Repo installs are common, but they are not the only supported shape.

For the shortest path, see [`first-10-minutes.md`](first-10-minutes.md). This page keeps the fuller setup and troubleshooting detail.

## Install

Brigade supports Linux, macOS, and Windows with Python 3.10 or newer. Follow the [per-OS install steps](../QUICKSTART.md#1-install) if `pipx` is not already available.

```bash
pipx install brigade-cli
brigade setup
brigade --version
```

Open a new terminal after `pipx ensurepath`. On Windows, run the same commands in PowerShell and use `.\my-repo` when you want Windows-style relative paths. WSL is optional, not required.

## Preview A Setup

Run the quickstart in dry-run mode first:

```bash
brigade operator quickstart --target ./my-repo --harnesses codex --dry-run
```

For an OpenClaw or Hermes workspace, use workspace depth:

```bash
brigade operator quickstart --target ~/agent-workspace --depth workspace --harnesses openclaw,hermes --owner openclaw --dry-run
```

Use a comma-separated harness list if you use more than one agent surface:

```bash
brigade operator quickstart --target ./my-repo --harnesses codex,claude,opencode,antigravity,pi,cursor,aider,goose,continue,copilot,qwen,kimi,adal,openhands,grok,amp,crush --dry-run
```

If the target already has a homegrown operator setup with scripts, handoff folders, crons, or process managers, inspect it first:

```bash
brigade operator adopt plan --target ~/agent-workspace --json
brigade operator adopt capture --target ~/agent-workspace --json
brigade operator adopt import-issues --target ~/agent-workspace --json
brigade operator migration status --target ~/agent-workspace --json
brigade operator migration doctor --target ~/agent-workspace --json
brigade operator migration consolidate --target ~/agent-workspace --surface shell_crontab --review-status needs-owner
brigade operator surfaces capture --target ~/agent-workspace --json
brigade operator surfaces doctor --target ~/agent-workspace --json
brigade operator surfaces review --target ~/agent-workspace --surface shell_crontab --status external-ok --all --reason reviewed-external-ownership
brigade operator surfaces reviews --target ~/agent-workspace --json
brigade operator surfaces import-issues --target ~/agent-workspace --json
```

The adoption plan is read-only. Capture writes a redacted local snapshot under `.brigade/operator/adoption/`, and import-issues routes the migration gaps into the work inbox. The migration commands roll adoption state, surface reviews, and pending migration work into one replacement-progress view; consolidate lets that rollup supersede tiny record-level imports. The surfaces commands keep redacted scheduler and process coverage under `.brigade/operator/surfaces/`, with count totals, status totals, ordinal labels, review decisions, and fingerprints. External schedulers and process managers are never stored with raw crontab lines, job names, process names, command paths, host details, or environment values.

## Apply The Setup

```bash
brigade operator quickstart --target ./my-repo --harnesses codex
brigade operator doctor --target ./my-repo --profile local-operator
```

Or apply an agent workspace setup:

```bash
brigade operator quickstart --target ~/agent-workspace --depth workspace --harnesses openclaw,hermes --owner openclaw
brigade operator doctor --target ~/agent-workspace --profile local-operator
```

Expected shape:

```text
quickstart: ok
operator doctor: ready yes
blocking issues: 0
```

For a machine-readable first-run report:

```bash
brigade operator quickstart --target ./my-repo --harnesses codex --json
```

In a healthy run, the JSON has `status: "ok"` and `issue_report.status: "ok"`.

Quickstart runs these local-only steps:

- installs Brigade repo or workspace templates
- writes host-local `.brigade/` operator config
- scopes handoff source coverage to the selected writer harnesses and writes a local bootstrap handoff-ingest latest-run log
- scaffolds the local MCP catalog and dogfood/work-loop config
- imports built-in portable tools and skills for workspace installs, `--full`, or explicit pack installs
- projects harness-specific files such as Codex skills or Claude command docs
- verifies selected handoff writer inboxes
- prints next commands

It does not start daemons, install hooks, publish, push, tag, or mutate remotes.

## What To Commit

If the target is a git repo, commit repo-shareable source files only. Keep generated and local state ignored.

If the target is an operator workspace outside a git repo, treat the same split as a portability rule: durable memory and reviewed rules may be worth backing up or syncing, while `.brigade/` and harness projections are host-local state.

Usually safe to commit after review:

- `AGENTS.md`
- `MEMORY.md` and reviewed memory cards if this repo owns memory
- `rules/`
- `tools/`
- public docs
- the managed handoff template for each selected writer harness — for Claude Code, `.claude/memory-handoffs/TEMPLATE.md` (required for Claude onboarding; `verify-harness` fails readiness when a global exclude hides it); for other writers, the matching `<inbox>/TEMPLATE.md`

Handoff inboxes stay local except each inbox's managed `TEMPLATE.md`, which Brigade deliberately un-ignores so the handoff format travels with the repository. Session handoff files in those folders stay ignored. A `?? .claude/` or `?? .codex/` in `git status` is usually just that template file.

Usually local-only:

- `.brigade/`
- `.codex/` (except `.codex/memory-handoffs/TEMPLATE.md` when Codex is selected)
- `.claude/` (except `.claude/memory-handoffs/TEMPLATE.md` when Claude Code is selected)
- `.opencode/`
- `.antigravity/`
- `.pi/`
- `.cursor/`
- `.hermes/`
- `.openclaw/`
- `.mcp/`
- generated `scripts/` projections

## If Quickstart Fails

First collect the compact report:

```bash
brigade operator quickstart --target ./my-repo --harnesses codex --json
```

Copy the `issue_report` object into a GitHub issue after reviewing it. Do not paste tokens, private hostnames, private repo names, or unredacted absolute paths.

Useful follow-up commands:

```bash
brigade operator doctor --target ./my-repo --profile local-operator --json
brigade operator verify-harness --target ./my-repo --harness codex --json
brigade tools doctor --target ./my-repo --json
brigade skills doctor --target ./my-repo --json
brigade security scan --target ./my-repo --fail-on none --json
```

### Claude handoff template hidden by a global Git exclude

If Claude verification reports `ready: no` with `handoff_template_shadowed`, a
global `core.excludesFile` rule such as `.claude/` is hiding the managed
handoff template. Brigade does not edit global Git configuration. Diagnose with
the exact recovery command from verify-harness or quickstart `next_commands`:

```bash
git check-ignore -v .claude/memory-handoffs/TEMPLATE.md
```

Then add narrow repo-local un-ignore rules to the target `.gitignore` (keep them
outside the managed Brigade block so re-init does not drop them):

```gitignore
!.claude/
.claude/*
!.claude/memory-handoffs/
.claude/memory-handoffs/*
!.claude/memory-handoffs/TEMPLATE.md
```

Re-run `git check-ignore -v .claude/memory-handoffs/TEMPLATE.md` until it no
longer reports the template as ignored, then:

```bash
brigade operator verify-harness --target ./my-repo --harness claude
```

Codex-only onboarding keeps its warning-only contract: a shadowed Codex
template stays an advisory warning and does not block readiness.

Open a quickstart issue here:

```text
https://github.com/escoffier-labs/brigade/issues/new/choose
```

Use the "Quickstart setup problem" form. The more exact the command and redacted output are, the faster the fix can land.
