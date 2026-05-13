# CLAUDE.md - Claude Code Rules

## Project Rules

- Follow repo-local `AGENTS.md` when present.
- Use this file only for Claude Code-specific behavior.

## Memory Handoff

The canonical memory owner on this workspace is **OpenClaw**. Claude Code may keep local session context, but durable knowledge must be written as a Memory Handoff in `.claude/memory-handoffs/`. See `AGENTS.md` for the full rule.

At the end of any substantial task, check whether the session produced durable knowledge. If yes, create a Memory Handoff using the standard format in `.claude/memory-handoffs/TEMPLATE.md`. Do this without waiting to be reminded.

## Closeout

- Report the verification command that was run.
- If verification could not run, state the blocker.
- If a Memory Handoff was warranted, confirm it was written.

## Git

- Do not add `Co-Authored-By` or AI-attribution trailers to commits.
- Use conventional commit messages.
- Never bypass pre-push hooks unless the user has explicitly accepted the risk.
