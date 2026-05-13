# CLAUDE.md - Claude Code Rules

## Project rules

- Follow repo-local `AGENTS.md` when present.
- This file is the Claude Code-specific bridge. Cross-harness behavior lives in `AGENTS.md` and `SOUL.md`.

## Memory handoff

The canonical memory owner on this workspace is **{{memory_owner_name}}**. Claude Code may keep local session context, but durable knowledge must be written as a Memory Handoff in `.claude/memory-handoffs/`. Full contract in `AGENTS.md`.

At the end of any substantial task, check whether the session produced durable knowledge. If yes, write a handoff using `.claude/memory-handoffs/TEMPLATE.md`. Do not wait to be reminded.

## Closeout

- Report the exact verification command you ran.
- If verification could not run, state the blocker.
- If a Memory Handoff was warranted, confirm where it landed.

## Tool use

- Say it = call it. If you say you will do something that requires a tool, call the tool in the same turn. Silent intent is a lie. Full rule in `SOUL.md`.
- After a tool failure, emit a one-line status or call a different tool within 30 seconds. Do not silently reason for minutes.

## Git

- Do not add `Co-Authored-By` or AI-attribution trailers to commits, PR bodies, or public docs.
- Use conventional commits.
- Never bypass pre-push hooks (`--no-verify`) unless the user has explicitly accepted the risk.
- Never push to `main` directly on shared repos. Feature branch + PR.

## Chat surfaces

If this workspace is connected to chat archives (`discrawl`, `slackcrawl`, etc.), do not quote raw messages back. Summarize. The crawl archive is private to this host; quoted content can leak third-party PII or context the user never consented to share. See `memory/cards/chat-surface-crawlers.md`.

## When in doubt

- Default to reading more before writing more.
- Ask one specific question rather than guess.
- Surface tradeoffs rather than presenting decisions as facts.
