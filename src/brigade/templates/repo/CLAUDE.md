# CLAUDE.md - Claude Code Rules

@AGENTS.md

## Project rules

- Follow `AGENTS.md` in this repo. This file is the Claude Code-specific bridge; cross-harness behavior lives in `AGENTS.md`.

## Memory handoff

The canonical memory owner on this repo is **{{memory_owner_name}}**. Claude Code may keep local session context, but durable knowledge must be written as a Memory Handoff in `.claude/memory-handoffs/`. Full contract in `AGENTS.md`.

Write a handoff only when the session produced knowledge a later session cannot get from the repo, git history or an existing card: a root cause, a decision with its reason, a new operational fact, or a stated preference. Run `brigade memory search` first and update that card where one exists. Write it with `brigade handoff draft`. If nothing qualifies, end with one line, `Nothing durable: <reason>`, and write no file. A code or config edit alone is not a reason.

## Brigade work loop (Mandatory)

This repo is Brigade-wired. Route real work through Brigade so its outcome ledger fills. Invoke the `brigade-work` skill: `brigade work brief` at the start; run verifications via `brigade work verify run --target . --command "<test>"` (not raw) when the result should count; then `brigade outcome capture <skill-or-card-id> --run-id latest`; handoff only if durable (see above). If `brigade outcome rank` says "ranking: none", work is not flowing through Brigade. Full contract in `AGENTS.md`.

For large scoping work, invoke `ultra-work-scout` first. It is installed with the built-in Brigade skills and keeps Scout delegation tied to verified Brigade work.

## Closeout

- Report the exact verification command you ran.
- If verification could not run, state the blocker.
- If a Memory Handoff was warranted, confirm where it landed. Otherwise end with `Nothing durable: <reason>`.

## Tool use

- Say it = call it. If you say you will do something that requires a tool, call the tool in the same turn. Silent intent is a lie.
- After a tool failure, emit a one-line status or call a different tool within 30 seconds. Do not silently reason for minutes.

## Git

- Do not add `Co-Authored-By` or AI-attribution trailers to commits, PR bodies, or public docs.
- Use conventional commits.
- Never bypass pre-push hooks (`--no-verify`) unless the user has explicitly accepted the risk.
- Never push to `main` directly on shared repos. Feature branch + PR.

## When in doubt

- Default to reading more before writing more.
- Ask one specific question rather than guess.
- Surface tradeoffs rather than presenting decisions as facts.
