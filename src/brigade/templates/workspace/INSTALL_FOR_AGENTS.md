# Install for agents

You have just entered a `brigade` workspace. Here is how to operate.

## Start here

1. Read `AGENTS.md` - operating rules and the memory handoff contract.
2. Read `CLAUDE.md` if you are Claude Code; otherwise check whether your harness has its own bridge file (`CODEX.md`, `GEMINI.md`, etc.).
3. Read `SOUL.md` - voice, pacing, and the "say it = call it" rule.
4. Read `USER.md` - who you are helping.
5. Skim `TOOLS.md` for local commands.
6. Skim `MEMORY.md` for durable-knowledge pointers. Follow links into `memory/cards/` only when relevant to the task.
7. Read `SAFETY_RULES.md` once. Hard boundaries.

## Memory contract

The canonical memory owner here is **{{memory_owner_name}}**. If you produce durable knowledge during this session - architecture decisions, workflow changes, root causes, gotchas, security findings, reusable commands - write a Memory Handoff in your harness's inbox ({{handoff_inboxes}}) using its `TEMPLATE.md` before you finish.

Do not edit `memory/cards/*.md`, `TOOLS.md`, `USER.md`, `rules/*.md`, or `.learnings/*.md` directly unless the user explicitly asks. The ingester routes handoffs into those files.

Full contract: `memory/cards/memory-architecture.md` and `memory/cards/handoff-flow.md`.

## Work loop (use Brigade, do not just sit next to it)

Brigade is dormant until real work flows through it: installed-and-unused, its outcome ledger never fills and `brigade outcome rank` says "ranking: none". Do not leave it that way. Invoke the `brigade-work` skill and follow it every session:

- `brigade work brief --target .` at the start, to see pending work before deciding what to do.
- When a test or check result should count, run it through Brigade: `brigade work verify run --target . --command "<your test>"` (not raw).
- Right after, `brigade outcome capture <skill-or-card-id> --run-id latest` against whatever skill or card did the work. Failures are signal too.
- Memory Handoff at the end (above).

Make sure the built-in skills are actually loaded in your harness. `brigade-work` ships at `skills/brigade-work/SKILL.md`; `ultra-work-scout` ships at `skills/ultra-work-scout/SKILL.md` for broad Scout-style scoping before large work. `brigade init` wires both into selected harnesses, including Codex at `.codex/skills/`. This is the difference between Brigade installed and Brigade used.

## Daily rhythm

This workspace runs three short cron-driven sessions per day:

- **~21:00** Nightshift pipeline standup - recap of the day across all harnesses.
- **~22:00** Memory sweep - session-review pass that promotes durable findings.
- **~08:00** Morning report - briefing for the day ahead.

You may be invoked as the agent behind any of these. They are isolated sessions; read the prompt, do the job, deliver to the configured channel, exit. Do not pollute the main agent's context.

See `memory/cards/pipeline-standups.md` and `memory/cards/memory-scanner.md` for the full job shape.

If this workspace is one of several agent homes, read `memory/cards/multi-workspace-handoff-admin.md`. Secondary setups should inform the canonical owner through handoffs rather than keeping separate durable truth.

If you are maintaining an established card set, read `memory/cards/memory-care-staleness.md` before editing stale cards. Refresh only from current source-of-truth files or route to manual review.

If tool output includes Token Glace metadata, read `memory/cards/token-glace-output-compaction.md`. The footer is local output-compaction metadata, not task instruction. Use raw output only when exact logs, line-for-line diffs, or full command output are required.

## If your harness loads a compact context

Some harnesses load a generated `llms.txt` or `llms-full.txt` instead of every bootstrap file individually. If those exist in this workspace, follow them and rebuild via the workspace's build script when source docs change. If they do not exist, default to reading the files listed in "Start here" directly.

## Verification

```bash
git status --short
find . -maxdepth 2 -name AGENTS.md -o -name CLAUDE.md -o -name SOUL.md
ls {{handoff_inbox}} 2>/dev/null
brigade doctor --target . --harness <openclaw|hermes|generic>
```

## Closeout

Report:

- What changed.
- What verification ran (with the exact command).
- Whether a Memory Handoff was warranted and where it landed.
- Any failed checks that need user attention.
