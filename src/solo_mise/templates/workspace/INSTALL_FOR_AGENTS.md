# Install for agents

You have just entered a `solo-mise` workspace. Here is how to operate.

## Start here

1. Read `AGENTS.md` - operating rules and the memory handoff contract.
2. Read `CLAUDE.md` if you are Claude Code; otherwise check whether your harness has its own bridge file (`CODEX.md`, `GEMINI.md`, etc.).
3. Read `SOUL.md` - voice, pacing, and the "say it = call it" rule.
4. Read `USER.md` - who you are helping.
5. Skim `TOOLS.md` for local commands.
6. Skim `MEMORY.md` for durable-knowledge pointers. Follow links into `memory/cards/` only when relevant to the task.
7. Read `SAFETY_RULES.md` once. Hard boundaries.

## Memory contract

The canonical memory owner here is **{{memory_owner_name}}**. If you produce durable knowledge during this session - architecture decisions, workflow changes, root causes, gotchas, security findings, reusable commands - write a Memory Handoff in `.claude/memory-handoffs/` using `TEMPLATE.md` before you finish.

Do not edit `memory/cards/*.md`, `TOOLS.md`, `USER.md`, `rules/*.md`, or `.learnings/*.md` directly unless the user explicitly asks. The ingester routes handoffs into those files.

Full contract: `memory/cards/memory-architecture.md` and `memory/cards/handoff-flow.md`.

## Daily rhythm

This workspace runs three short cron-driven sessions per day:

- **~21:00** Nightshift pipeline standup - recap of the day across all harnesses.
- **~22:00** Memory sweep - session-review pass that promotes durable findings.
- **~08:00** Morning report - briefing for the day ahead.

You may be invoked as the agent behind any of these. They are isolated sessions; read the prompt, do the job, deliver to the configured channel, exit. Do not pollute the main agent's context.

See `memory/cards/pipeline-standups.md` and `memory/cards/memory-scanner.md` for the full job shape.

If this workspace is one of several agent homes, read `memory/cards/multi-workspace-handoff-admin.md`. Secondary setups should inform the canonical owner through handoffs rather than keeping separate durable truth.

If you are maintaining an established card set, read `memory/cards/memory-care-staleness.md` before editing stale cards. Refresh only from current source-of-truth files or route to manual review.

## If your harness loads a compact context

Some harnesses load a generated `llms.txt` or `llms-full.txt` instead of every bootstrap file individually. If those exist in this workspace, follow them and rebuild via the workspace's build script when source docs change. If they do not exist, default to reading the files listed in "Start here" directly.

## Verification

```bash
git status --short
find . -maxdepth 2 -name AGENTS.md -o -name CLAUDE.md -o -name SOUL.md
ls .claude/memory-handoffs/ 2>/dev/null
solo-mise doctor --target . --harness <openclaw|hermes|generic>
```

## Closeout

Report:

- What changed.
- What verification ran (with the exact command).
- Whether a Memory Handoff was warranted and where it landed.
- Any failed checks that need user attention.
