# Agent-Assisted Setup

You can point an agent at the Brigade repository and ask it to install Brigade for you. The root `AGENTS.md` contains the direct instructions an agent should follow, so users should not need to paste a long prompt.

Brigade is designed to help users adapt an existing homegrown agent setup, not replace it wholesale. Keep the user's current memory owner, repo layout, harness choices, and local habits unless Brigade needs a small compatibility file or handoff inbox to make the workflow portable.

The agent should treat Brigade setup as local workspace wiring, not as a release, deploy, or remote mutation.

## Agent Entry Point

If an agent has access to this repository, it should start by reading:

- `AGENTS.md`
- `README.md`
- `docs/new-user-quickstart.md`
- `docs/agent-assisted-setup.md`

Then it should work inside the target repo and run:

```bash
pipx install brigade-cli
brigade --version
brigade operator quickstart --target . --harnesses codex --dry-run
brigade operator quickstart --target . --harnesses codex
brigade operator doctor --target . --profile local-operator
```

## What The Agent Should Do

The agent should:

- install the `brigade-cli` package if missing
- run quickstart in dry-run mode first
- apply quickstart only after the dry-run looks reasonable
- run `operator doctor` and report the exact result
- explain which files are shareable and which are local-only
- preserve the user's existing memory layout and agent conventions where possible
- suggest Brigade compatibility wiring instead of moving or renaming personal systems
- stop and ask before remote changes, destructive commands, new services, schedulers, or commits

The agent should not:

- treat `.brigade/` as public repo content
- paste raw scanner output that may contain secrets
- rewrite permanent memory files unless the user asked for that exact edit
- force the user into Brigade's example layout when they already have a working setup
- push, tag, publish, deploy, or install hooks without explicit approval

## Adapting A Homegrown Setup

Many users already have a personal version of this workflow: memory files, agent instructions, project notes, handoff folders, scripts, scheduled checks, or tool-specific command docs. Brigade should make that setup easier to reuse across agents.

When adapting an existing setup, the agent should:

- inventory current files such as `AGENTS.md`, `CLAUDE.md`, `MEMORY.md`, `TOOLS.md`, `.codex/`, `.claude/`, `.opencode/`, `.hermes/`, and `.openclaw/`
- identify the memory owner before changing memory rules
- keep repo-shareable source files separate from generated local projections
- use `brigade operator quickstart --dry-run` to preview compatibility files
- run `brigade operator doctor` after setup and report what remains manual
- leave existing working conventions intact unless the user approves a migration

Good adaptation usually means adding a handoff inbox, a shared instruction file, portable tool sources, or scanner config. It should not mean flattening the user's system into someone else's exact directory tree.

## Harness Selection

Use the harness list that matches the tools the user actually runs:

```bash
brigade operator quickstart --target . --harnesses codex
brigade operator quickstart --target . --harnesses claude
brigade operator quickstart --target . --harnesses opencode
brigade operator quickstart --target . --harnesses codex,claude,opencode
```

OpenClaw and Hermes can act as memory owners or writer surfaces depending on the user's setup. If the agent is unsure, it should start with the harness the user is currently using and report the next command needed to add another surface later.

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
