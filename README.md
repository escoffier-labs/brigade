# Brigade

**Shared memory for people using AI agent tools.**

Brigade is for the normal version of agent work: you have OpenClaw, Codex, Claude Code, OpenCode, Hermes, or some mix of them, and you want them to remember useful things without stuffing every random session note into permanent memory.

Brigade gives those tools a simple local routine:

1. an agent writes a handoff note
2. you review it
3. the useful part becomes durable memory
4. the next session starts smarter

It runs on your machine and keeps you in control. No background service, no surprise publishing, no automatic memory rewrites.

![One shared memory, many agent tools](docs/assets/brigade-memory-flow.svg)

<p align="center">
  <img src="docs/assets/brigade-social-preview.jpg" alt="Brigade" width="520">
</p>

<p align="center">
  <img src="https://img.shields.io/github/actions/workflow/status/escoffier-labs/brigade/ci.yml?branch=main&style=for-the-badge&label=ci" alt="CI status">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/license-MIT-green?style=for-the-badge" alt="MIT license">
</p>

## Who This Is For

Use Brigade if:

- you use OpenClaw as your main memory system
- you use more than one agent tool in the same repo
- you want Codex, Claude Code, OpenCode, or Hermes to leave memory notes
- you want to review those notes before saving them
- you want local proof of what agents did without sending it anywhere

You do not need to understand the whole command surface to start. The detailed operator system is there when you need it, but the first use case is just shared memory handoffs.

## Start Here

Install:

```bash
pipx install brigade-cli
```

Set up a repo:

```bash
brigade init --target ./my-repo --depth repo --harnesses openclaw,codex
brigade doctor --target ./my-repo
```

Write a handoff note:

```bash
brigade handoff draft \
  --target ./my-repo \
  --inbox codex \
  --title "What changed" \
  --summary "Short note future agents should know." \
  --content "### What changed

Put the durable note here."
```

Then review it before adding it to long-term memory.

## The Loop

![The Brigade loop stays local and reviewable](docs/assets/brigade-local-loop.svg)

Brigade’s job is to keep the loop boring and safe:

- agents can write notes
- humans choose what gets saved
- local files show what happened
- risky actions stay manual

## What It Handles

Brigade can set up and check:

- shared memory files and rules
- handoff inboxes for Codex, Claude Code, OpenCode, and Hermes
- OpenClaw or Hermes as the main memory owner
- local review queues and work receipts
- optional scanner and release-readiness workflows

Generated local state stays in ignored folders such as `.brigade/`, `.codex/`, `.claude/`, `.opencode/`, and `.hermes/`.

## What It Does Not Do

Brigade does not:

- run in the background
- install schedulers
- push to GitHub
- publish releases
- send notifications by default
- save every note automatically

That pause is intentional. Agent memory should be useful, not noisy.

## Docs

- [Technical guide](docs/technical-guide.md): the detailed command walkthrough.
- [Hermes handoffs](docs/hermes-handoffs.md): Hermes writer inbox setup.
- [Handoff promotion](docs/handoff-promotion.md): how reviewed notes move toward memory.
- [Internal dogfood loop](docs/internal-dogfood.md): how this repo uses Brigade on itself.
- [Command inventory](docs/command-inventory.md): every public CLI command.
- [Roadmap](ROADMAP.md): current direction.

## Tiny Glossary

- **Agent tool**: OpenClaw, Hermes, Codex, Claude Code, OpenCode, or a similar program.
- **Handoff**: a note an agent writes for later review.
- **Inbox**: the local folder where those notes wait.
- **Memory owner**: the place that keeps the durable shared memory.
- **Operator**: the human deciding what gets saved or run.
