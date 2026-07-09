# Quickstart

Five minutes from install to a working agent kitchen.

## 1. Install

```bash
pipx install brigade-cli
```

To track the latest `main` branch instead of the latest package release:

```bash
pipx install git+https://github.com/escoffier-labs/brigade
```

If you do not have `pipx`:

```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
```

## First install

The canonical first-run command is `brigade operator quickstart`. It installs the template files, wires the operator config, scaffolds the MCP catalog and dogfood/work-loop config, and runs the health checks in one shot:

```bash
# Code repo with Codex as the writer
brigade operator quickstart --target ./my-repo --harnesses codex

# OpenClaw or Hermes workspace instead of a code repo
brigade operator quickstart --target ~/agent-workspace --depth workspace --harnesses openclaw,hermes --owner openclaw
```

Pass `--dry-run` first to preview the planned steps without writing anything.

Two commands share this surface: `brigade init` installs the template files only, and `brigade operator quickstart` wraps it with operator config, the MCP and dogfood on-ramps, writer verification, and health checks. Use `init` when you want the interactive harness picker or just the files:

```bash
$ brigade init --target ~/agent-kitchen

Which harnesses do you use? (type numbers separated by space/comma to toggle, enter to confirm)
  [x] 1. Claude Code
  [ ] 2. Codex
  [ ] 3. OpenCode
  [ ] 4. Antigravity
  [ ] 5. Pi
  [ ] 6. Cursor
  [ ] 7. Aider
  [ ] 8. Goose
  [ ] 9. Continue
  [ ] 10. GitHub Copilot CLI
  [ ] 11. Qwen Code
  [ ] 12. Kimi Code
  [ ] 13. AdaL
  [ ] 14. OpenHands
  [ ] 15. Grok CLI
  [ ] 16. Amp
  [ ] 17. Crush
  [ ] 18. OpenClaw
  [ ] 19. Hermes

Depth? (type a number, enter for default)
  * 1. repo       (handoff flow + publish guard)
    2. workspace  (full home: MEMORY.md, TOOLS.md, USER.md, ...)

Add-ons? (type numbers separated by space/comma to toggle, enter to confirm)
  [ ] 1. publisher  (content-guard policies for blog/social/docs)
```

Defaults are claude harness, repo depth, no includes. Enter ships the install.

## CI / scripted install

Pass flags directly to skip the prompt. The same flags work on `operator quickstart`:

```bash
# Claude Code + Codex + OpenClaw, full workspace
brigade operator quickstart --target ~/agent-kitchen \
  --depth workspace \
  --harnesses claude,codex,openclaw

# Codex-only project, minimal install
brigade operator quickstart --target ./my-project --depth repo --harnesses codex

# Template files only, no harness-specific files
brigade init --target ./my-project --harnesses none
```

## Verifying

After install, check the operator profile:

```bash
brigade operator doctor --target <path> --profile local-operator
```

For the file-by-file view, `brigade doctor --target <path>` reports the apparent harness shape and checks every configured inbox and adapter:

```
brigade doctor: target /home/you/agent-kitchen
  harnesses: claude, codex, openclaw (owner=openclaw, depth=workspace)
  [ok]   bootstrap: AGENTS.md   /home/you/agent-kitchen/AGENTS.md
  [ok]   handoff: claude inbox  /home/you/agent-kitchen/.claude/memory-handoffs
  [ok]   handoff: codex inbox   /home/you/agent-kitchen/.codex/memory-handoffs
  [ok]   openclaw: config        /home/you/.openclaw/openclaw.json
  ...
```

A `[fail]` line means the install is incomplete; `[warn]` is informational; `[todo]` means the check needs your attention.

## Reconfiguring

To change which harnesses are installed on an existing target:

```bash
# Add a harness
brigade reconfigure --target . --harnesses claude,codex

# Drop one (without removing its files)
brigade reconfigure --target . --harnesses claude

# Drop one and remove its files
brigade reconfigure --target . --harnesses claude --prune
```

## The handoff flow

The starter handoff template lives at `<inbox>/TEMPLATE.md`. Copy it to a new dated file (e.g. `2026-05-16-1430-fixed-X.md`), fill it in, and the ingester promotes safe card handoffs into `memory/cards/`, appends targeted updates to the right file, and kicks ambiguous material to the review inbox.

See the [Solo Cookbook](https://github.com/escoffier-labs/solos-cookbook) for the longer-form guidance on what makes a good handoff and when to use which routing.

## Your daily loop

Handoffs are one half. The other half is routing your actual work through Brigade so it produces a real signal, instead of leaving Brigade installed-but-dormant. `brigade init` wires a `brigade-work` skill into each harness so your agent does this automatically; by hand it is three steps:

```bash
brigade work brief --target .                                  # 1. what's pending (and whether the loop is being fed)
brigade work verify run --target . --command "pytest -q" --capture <skill-or-card>   # 2. verify + capture in one step
# 3. write a Memory Handoff for anything durable; the ratchet (outcome reconcile) runs hands-off on your cron
```

Capture against an id you actually have: a skill you followed, a memory card (`--kind card`), or `brigade-work` itself when nothing else applies. `brigade work brief` surfaces the loop's own health so an empty ledger never goes unnoticed.

## Optional: close the GraphTrail ↔ Brigade ↔ MiseLedger loop

0.21.0 already ships receipt deltas, MiseLedger export, evidence briefs, and context evals. Productizing them is a short dogfood path:

```bash
# optional stations (fail-open everywhere if absent)
brigade add graphtrail          # or: cargo install graphtrail
graphtrail sync                 # builds .graphtrail/graphtrail.db in the repo
brigade add evidence            # miseledger

# one operator glance: doctors + graph ok / ledger ok / last brief hit rate
brigade operator checkup --target .

# after real work flows through verify/run + capture:
brigade receipts export miseledger --target . --new-only --import
brigade outcome rank --target .    # surfaces brief_hit as a skill quality signal
# next brigade run attaches a capped MiseLedger evidence brief automatically
```

That is the differentiated loop: receipts that feed the next run's context, with a measured hit rate (`context_eval.brief_hit_rate`) on whether the pre-run brief named the files the run actually touched.

## Next steps

- Read [the cookbook](https://github.com/escoffier-labs/solos-cookbook) for the deep version of every concept here.
- Customize `USER.md` and `TOOLS.md` with your real preferences and runbooks (kept private; do not commit personal details).
- Wire the ingester on a cron or a manual end-of-day workflow.
- Add a memory-care staleness scan when your card set starts to matter. See [docs/memory-care.md](docs/memory-care.md).
- If you use TokenJuice, wire Claude Code and Codex hooks deliberately and tell agents what the wrapper means. See the tokens station in [docs/technical-guide.md](docs/technical-guide.md#managed-stations).
- Run `brigade work bootstrap` inside active repos that did not use quickstart when you want the dogfood-backed daily work loop, scanner inbox, and local evidence receipts.
