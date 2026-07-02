# AGENTS.md - Working In This Repo

This repo is Brigade-wired. These are the operating rules for any agent working here.

## Every Session

For substantial work, first gather context:

1. Read this file - operating rules and the memory handoff contract.
2. Read the repo's `README` and `CONTRIBUTING` (if present) for build, test, and style expectations.
3. Skim your harness's handoff inbox ({{handoff_inboxes}}) for recent notes from other sessions.
4. Read `SAFETY_RULES.md` once. Hard boundaries.

For tiny read-only commands, do the command directly and avoid loading unrelated context. Do not ask permission for normal context gathering.

## Definition of Done

Before you report work as complete:

1. Run the project's checks (tests, linters, type checks, build). If you do not know them, look in `README`, `CONTRIBUTING`, `pyproject.toml` / `package.json`, or the CI config before guessing.
2. Report the actual result. Never claim success you did not observe; paste the command you ran and its output.
3. If a check could not run, say so and name the blocker instead of guessing.
4. If the session produced durable knowledge, confirm a Memory Handoff was written (see below).

## Memory Owner

The configured memory owner is **{{memory_owner_name}}**. Side harnesses may keep local session context, but durable knowledge must be written as a Memory Handoff in your harness's own inbox ({{handoff_inboxes}}). The memory owner ingests those handoffs into canonical durable memory; reviewed repo-local notes land under `.learnings/`.

If you are not the memory owner, do not edit `SAFETY_RULES.md`, `rules/`, or `.learnings/` as canonical memory unless the user explicitly asks for that file edit. Do not create a second canonical memory system.

## Memory Handoff (Mandatory)

If a session discovers durable knowledge - architecture decisions, workflow changes, non-obvious fixes, setup gotchas, security findings, reusable commands, durable research, or user preferences - create a handoff at the end of the task.

Write the handoff to your harness's inbox ({{handoff_inboxes}}) as `<YYYY-MM-DD-HHMM>-<slug>.md` using the format in that inbox's `TEMPLATE.md`. If the template is missing, run `brigade handoff-template` to print it.

Do not wait to be reminded. Do not edit canonical memory directly unless this is the memory owner.

## Daily Work Loop (Mandatory)

This repo is Brigade-wired, so real work flows THROUGH Brigade and its outcome ledger and learning ratchet fill instead of sitting empty. Invoke the `brigade-work` skill and follow it:

- **Start:** `brigade work brief --target .` (or `brigade daily status --target .`) to see pending work before deciding what to do.
- **Verify through Brigade, not raw:** when a test or check result should count, run `brigade work verify run --target . --command "<your test>"` instead of running it bare. This writes a receipt with the real exit code.
- **Capture the outcome:** immediately after, `brigade outcome capture <skill-or-card-id> --run-id latest` against whatever skill or card did the work. A failure is signal too.
- **Finish:** write the Memory Handoff (above).

Running tests raw produces no signal. If `brigade outcome rank --target .` ever says "ranking: none", the loop is not being fed - this is the difference between Brigade installed and Brigade used.

For large or ambiguous tasks, invoke the `ultra-work-scout` skill before editing. It scopes parallel Scout questions, keeps ownership with the main agent, and still routes final verification through Brigade.

## Self-Improvement

When the user corrects you, write a Memory Handoff capturing the correction and *why*, so the next session does not re-make the same mistake. Check your inbox's recent notes before similar tasks.

## Safety

- Keep private data local unless the user explicitly asks otherwise.
- Do not run destructive commands without asking.
- Prefer recoverable deletes (`trash`) over permanent recursive deletion.
- When in doubt, ask.

Full hard rules: `SAFETY_RULES.md`.

## Git And Repo Work

- Do not revert or overwrite user changes.
- Do not use destructive git commands without explicit approval.
- Use conventional commits.
- Never add `Co-Authored-By` lines.
- Never mention AI tools, model vendors, or bot identities in commit messages.
- Run the smallest meaningful verification before claiming success, and report the exact command.

## Brigade operator setup (advanced)

Beyond the daily work loop above, an operator sets up readiness and tool sync with `brigade operator guide`, `brigade operator doctor --target .`, and `brigade operator sync-tools --target .`. Keep `.brigade/` gitignored; tracked cross-harness tool sources live under `tools/`. Brigade does not run automatically, start daemons, install hooks, send notifications, publish, push, tag, or mutate remotes.
