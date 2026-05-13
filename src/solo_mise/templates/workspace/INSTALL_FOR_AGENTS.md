# Install For Agents

You have just been dropped into a `solo-mise` workspace. Here is how to operate.

## Start Here

1. Read `AGENTS.md` for the operating rules and the memory handoff contract.
2. Read `CLAUDE.md` if you are Claude Code; otherwise check whether this harness has its own bridge file.
3. Read `TOOLS.md` for local commands.
4. Skim `MEMORY.md` for durable-knowledge pointers. Follow links into `memory/cards/` only when relevant to the task.
5. Read `SAFETY_RULES.md` once. It has hard boundaries.

## Memory Handoff

The canonical memory owner is **{{memory_owner_name}}**. If you produce durable knowledge during this session - architecture decisions, workflow changes, root causes, gotchas, security findings, reusable commands - write a Memory Handoff in `.claude/memory-handoffs/` using `TEMPLATE.md` before you finish.

Do not edit `memory/cards/*.md`, `TOOLS.md`, `USER.md`, `rules/*.md`, or `.learnings/*.md` directly unless the user explicitly asks. The ingester routes handoffs into those files.

## Verification

```bash
git status --short
find . -maxdepth 2 -name AGENTS.md -o -name CLAUDE.md
ls .claude/memory-handoffs/ 2>/dev/null
```

## Closeout

Report:

- what changed
- what verification ran
- whether a Memory Handoff was warranted and where it landed
