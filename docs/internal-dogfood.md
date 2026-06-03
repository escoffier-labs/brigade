# Internal Dogfood Wiring

Brigade is repo-wired first and machine-aware second.

The repo wiring lives under `.brigade/` and is intentionally gitignored. It stores local config, receipts, scans, reports, queues, readiness waivers, and run artifacts for one repo. The machine wiring is limited to installed commands and user-level optional tools such as `brigade`, `codex`, and `agent-notify`.

## Onboard a Repo

Use the internal dogfood profile when a repo should participate in the local production loop:

```bash
brigade operator init --profile internal-dogfood --target .
brigade operator sync-tools --target .
brigade operator doctor --profile internal-dogfood --target .
brigade operator status --profile internal-dogfood --target .
```

For Brigade's own repo, public release readiness can be waived when the goal is internal production rather than publishing:

```bash
brigade operator init --profile internal-dogfood --target . --waive-public-release
```

That waiver is local evidence only. It does not publish, tag, push, mutate remotes, or change the public release gate.

## Daily Agent Loop

Agents and humans should use Brigade as an explicit local loop, not as a daemon:

```bash
brigade operator guide
brigade operator doctor --profile internal-dogfood --target .
brigade operator status --profile internal-dogfood --target .
brigade operator sync-tools --target .
brigade daily status --target .
brigade daily plan --target .
```

When the selected action is a safe bounded adapter, run:

```bash
brigade daily run --target .
```

When the selected action needs judgment or external setup, do it manually, then run `brigade daily status --target .` again and confirm the signal moved.

## Handoffs

If a session changes how Brigade should be used in this repo, write that durable knowledge into the normal handoff path for the active harness:

- Codex dogfood runs default to `.codex/memory-handoffs/`.
- Claude Code uses `.claude/memory-handoffs/`.
- OpenCode uses `.opencode/memory-handoffs/`.
- Hermes uses `.hermes/memory-handoffs/`.

Prefer the writer command so the draft matches Brigade's expected section style and lints before handoff:

```bash
brigade handoff draft --title "Brigade workflow update" --summary "What changed and why it matters." --content "### Brigade workflow update\n\nDurable guidance to append."
```

Handoffs should mention the concrete commands, what changed, what remains manual, and any local-only waiver or setup assumption. Do not copy raw `.brigade/` evidence into committed docs.

## Boundaries

The internal dogfood profile:

- writes repo-local gitignored config defaults
- refreshes read-only security evidence
- reports repo-local versus machine-level wiring
- projects tracked `tools/*.md` sources into local harness folders when `brigade operator sync-tools --target .` is run
- verifies a harness handoff inbox with `brigade operator verify-harness --harness hermes --target .`
- keeps public release readiness separate from internal production readiness

It does not:

- start background services
- install global hooks
- send notifications
- ingest handoffs into canonical memory
- publish, push, tag, or mutate remotes
- make other repos participate automatically

Other repos need their own `brigade operator init --profile internal-dogfood --target <repo>` or need to be tracked through the repo-fleet commands.
