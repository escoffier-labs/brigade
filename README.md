# Solomon's Mise en Place

> Mise en place for agent memory.

`solo-mise` is the installable starter kit behind [Solomon's Guide to Cookin' with Gas](https://github.com/solomonneas/solos-cookbook). It lays down a public-safe agent workspace, a memory handoff flow, and a bootstrap layout that works with OpenClaw, Claude Code, Codex, Hermes, and other coding harnesses.

The cookbook explains the *why*. This package gives you the *kitchen*.

## What you get

- sanitized bootstrap files for agent behavior, safety, tools, identity, and memory
- a canonical memory layout where one configured owner holds durable knowledge
- a `.claude/memory-handoffs/` inbox shared by Claude Code, Codex, and other side harnesses
- starter memory cards and routing rules
- content-guard publish gates so private infrastructure does not leak into public docs
- adapter fragments for OpenClaw (tested), Hermes (stubbed), and generic harnesses
- doctor checks that prove the system is wired before you trust it

## What you do not get

- private hostnames, IPs, account IDs, or personal details
- live auth profiles or OAuth tokens
- cron jobs that post publicly by default
- destructive automation, or write-enabled integrations without explicit opt-in

## Install

```bash
pipx install git+https://github.com/solomonneas/solo-mise
```

## Quick path

```bash
solo-mise init --target .                     # repo-local handoff flow + publish guard
solo-mise init --target ~/my-workspace --profile workspace   # full agent kitchen
solo-mise doctor --target ~/my-workspace
solo-mise scrub --target .
```

See [QUICKSTART.md](QUICKSTART.md) for step-by-step setup and verification.

## Profiles

| Profile | What it installs | When to use |
|---------|------------------|-------------|
| `repo` *(default)* | `AGENTS.md`, `CLAUDE.md`, `.claude/memory-handoffs/`, pre-push hook | A project wants the handoff flow and a public-leak guard. |
| `workspace` | Full bootstrap file set, memory folders, starter cards, safety files | A user wants a home agent workspace. |
| `openclaw` | `workspace` plus OpenClaw config fragments and doctor checks | An OpenClaw user. |
| `hermes` | `workspace` plus Hermes adapter fragments and doctor checks | A Hermes user. (Experimental.) |
| `generic` | Contract docs and templates, no orchestrator config | A user who wants the layout without picking a harness yet. |
| `publisher` | content-guard policies, scrub commands, publish gates | A user who publishes blog posts, docs, or social drafts. |

## The design

**Harness-agnostic contract, OpenClaw-tested defaults.** One memory owner is the source of truth. Side harnesses keep local context but write durable findings as Memory Handoffs into a shared inbox. A conservative ingester routes those into cards, runbooks, user prefs, rules, or learnings.

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

## Related

- [Solomon's Cookbook](https://github.com/solomonneas/solos-cookbook) - the long-form guide and reference docs
- [content-guard](https://github.com/solomonneas/content-guard) - the publish-gate scanner used by the pre-push hook
- [OpenClaw](https://github.com/openclaw/openclaw) - the reference memory owner

## License

MIT.
