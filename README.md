<p align="center">
  <img src="docs/assets/solo-mise-banner.png" alt="Solomon's Mise en Place banner">
</p>

<h1 align="center">Solomon's Mise en Place</h1>

<p align="center">
  <strong>Mise en place for agent memory.</strong>
</p>

<p align="center">
  <em>Public-safe workspace bootstrap, memory handoffs, and publish guards for real agent setups.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/github/actions/workflow/status/solomonneas/solo-mise/ci.yml?branch=main&style=for-the-badge&label=ci" alt="CI status">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/license-MIT-green?style=for-the-badge" alt="MIT license">
  <img src="https://img.shields.io/badge/profiles-6-orange?style=for-the-badge" alt="6 profiles">
</p>

<p align="center">
  <code>solo-mise</code> is the installable starter kit behind <a href="https://github.com/solomonneas/solos-cookbook">Solomon's Guide to Cookin' with Gas</a>.
  It gives you the workspace skeleton, handoff inbox, conservative ingester, and publish guard that make a multi-agent setup usable without leaking private junk into public repos.
</p>

## What this is

This package lays down a clean starting point for an agent workspace or a repo that needs durable memory handoffs. It is meant for people running real tools, real docs, and real automation across OpenClaw, Claude Code, Codex, Hermes, or a similar harness.

The cookbook explains the why. This package gives you the kitchen.

## What you get

- sanitized bootstrap files for agent behavior, safety, tools, identity, and memory
- a canonical memory layout where one configured owner holds durable knowledge
- a shared `.claude/memory-handoffs/` inbox for Claude Code, Codex, and other side harnesses
- starter memory cards and routing rules
- multi-workspace handoff patterns for people administering more than one agent setup
- memory-care staleness checks so durable cards do not quietly rot
- content-guard publish gates so private infrastructure does not leak into public docs
- adapter fragments for OpenClaw (tested), Hermes (stubbed), and generic harnesses
- doctor checks that prove the system is wired before you trust it

## What you do not get

- private hostnames, IPs, account IDs, or personal details
- live auth profiles or OAuth tokens
- cron jobs that post publicly by default
- destructive automation or write-enabled integrations without explicit opt-in

## Install

```bash
pipx install git+https://github.com/solomonneas/solo-mise
```

## Quick path

```bash
solo-mise init --target .                     # repo-local handoff flow + publish guard
solo-mise init --target ~/agent-kitchen --profile workspace
solo-mise doctor --target ~/agent-kitchen
solo-mise scrub --target .
```

See [QUICKSTART.md](QUICKSTART.md) for setup, verification, and the ingest flow.

## Profiles

| Profile | What it installs | When to use |
|---------|------------------|-------------|
| `repo` *(default)* | `AGENTS.md`, `CLAUDE.md`, `.claude/memory-handoffs/`, pre-push hook | A project wants the handoff flow and a public-leak guard. |
| `workspace` | Full bootstrap file set, memory folders, starter cards, safety files | A user wants a home agent workspace. |
| `openclaw` | `workspace` plus OpenClaw config fragments and doctor checks | An OpenClaw user. |
| `hermes` | `workspace` plus Hermes adapter fragments and doctor checks | A Hermes user. Experimental. |
| `generic` | Contract docs and templates, no orchestrator config | A user wants the layout without picking a harness yet. |
| `publisher` | content-guard policies, scrub commands, publish gates | A user who publishes blog posts, docs, or social drafts. |

## The design

One memory owner stays canonical. Side harnesses keep local context, but durable findings move through a shared handoff inbox and into the right destination.

```text
Claude Code / Codex / other harness
        |
        v
<repo>/.claude/memory-handoffs/*.md
        |
        v
solo-mise ingest (or your harness's equivalent)
        |
        v
memory/cards/*.md, TOOLS.md, USER.md, rules/*.md, .learnings/*.md
```

The ingester is intentionally conservative. Safe card handoffs become cards. Targeted updates append to the right file. Ambiguous material gets kicked out for review instead of being trusted automatically.

For users running multiple agent homes, treat the owner workspace as the hub. Remote or secondary workspaces can write handoffs into their own `.claude/memory-handoffs/` directories, then a trusted sync pulls those files into a staging inbox on the owner. That keeps agents informed about what happened elsewhere without creating multiple canonical memories.

## Related

- [Solomon's Cookbook](https://github.com/solomonneas/solos-cookbook): the long-form guide and reference docs
- [content-guard](https://github.com/solomonneas/content-guard): the publish-gate scanner used by the pre-push hook
- [OpenClaw](https://github.com/openclaw/openclaw): the reference memory owner

## License

MIT
