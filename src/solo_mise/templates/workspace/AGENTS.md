# AGENTS.md - Workspace Rules

## Memory Owner

The configured memory owner is **{{memory_owner_name}}**. Side harnesses may keep local session context, but durable knowledge must be written as a Memory Handoff in `.claude/memory-handoffs/`. The memory owner ingests those handoffs into canonical durable memory.

Do not create a second canonical memory system. If a session produced durable knowledge, write the handoff and let the owner route it.

## Every Session

- Read the repo-local instructions before editing.
- Prefer root-cause fixes over surface patches.
- Run the smallest meaningful verification before claiming success.
- Ask before destructive, production-impacting, or dependency-adding work.

## Memory Handoff

If a session discovers durable knowledge - architecture decisions, workflow changes, non-obvious fixes, setup gotchas, security findings, reusable commands, durable research, or user preferences - create a handoff at the end of the task.

Write the handoff to `.claude/memory-handoffs/<YYYY-MM-DD-HHMM>-<slug>.md` using the format in `.claude/memory-handoffs/TEMPLATE.md`.

Do not wait to be reminded. Do not edit canonical memory directly unless this is the memory owner.

## Safety

- Never expose secrets, private hostnames, account IDs, or internal endpoints in public output.
- Use deterministic scrubbers before publishing generated content.
- Do not bypass security checks unless the user explicitly accepts the risk.
- Read `SAFETY_RULES.md` for hard boundaries.

## Multi-Agent Workflow

- Delegate bounded tasks with clear ownership.
- Keep write scopes separate when multiple agents work in parallel.
- Integrate results before reporting completion.
