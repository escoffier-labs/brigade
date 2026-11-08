# Install for agents

You have just entered a Brigade-wired repo. Here is how to operate.

## Start here

1. Read `AGENTS.md` - operating rules and the memory handoff contract.
2. Read `CLAUDE.md` if you are Claude Code; otherwise check whether your harness has its own bridge file.
3. Read the repo's `README` and `CONTRIBUTING` (if present) for build, test, and style expectations.
4. Read `SAFETY_RULES.md` once. Hard boundaries.

## Memory contract

The canonical memory owner here is **{{memory_owner_name}}**. Write a Memory Handoff in your harness's inbox ({{handoff_inboxes}}) only when the session produced knowledge a later session cannot get from the repo, git history or an existing card: a root cause, a decision with its reason, a new operational fact, or a stated preference. Check `brigade memory search` first, and write it with `brigade handoff draft`. If nothing qualifies, end with one line, `Nothing durable: <reason>`, and write no file. A code or config edit alone is not a reason.

Do not edit `rules/*.md` or `.learnings/*.md` directly unless the user explicitly asks. The ingester routes handoffs into those files.

## Work loop (use Brigade, do not just sit next to it)

Brigade is dormant until real work flows through it: installed-and-unused, its outcome ledger never fills and `brigade outcome rank` says "ranking: none". Do not leave it that way. Invoke the `brigade-work` skill and follow it every session:

- `brigade work brief --target .` at the start, to see pending work before deciding what to do.
- When a test or check result should count, run it through Brigade: `brigade work verify run --target . --command "<your test>"` (not raw).
- Right after, `brigade outcome capture <skill-or-card-id> --run-id latest` against whatever skill or card did the work. Failures are signal too.
- Memory Handoff only if durable (above), else `Nothing durable: <reason>`.

Make sure the built-in skills are actually loaded in your harness. `brigade init` wires `brigade-work` and `ultra-work-scout` into selected harnesses, including Codex at `.codex/skills/`. This is the difference between Brigade installed and Brigade used.

## Verification

```bash
git status --short
ls {{handoff_inbox}} 2>/dev/null
brigade operator doctor --target .
```

## Closeout

Report:

- What changed.
- What verification ran (with the exact command).
- Whether a Memory Handoff was warranted and where it landed.
- Any failed checks that need user attention.
