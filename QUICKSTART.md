# Quickstart

Five minutes from clone to a working agent kitchen.

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

The fastest path is to run `brigade init` with no flags and answer the prompts:

```bash
$ brigade init --target ~/agent-kitchen

Which harnesses do you use? (type numbers separated by space/comma to toggle, enter to confirm)
  [x] 1. Claude Code
  [ ] 2. Codex
  [ ] 3. OpenClaw
  [ ] 4. Hermes (experimental)

Depth? (type a number, enter for default)
  * 1. repo       (handoff flow + publish guard)
    2. workspace  (full home: MEMORY.md, TOOLS.md, USER.md, ...)

Add-ons? (type numbers separated by space/comma to toggle, enter to confirm)
  [ ] 1. publisher  (content-guard policies for blog/social/docs)
```

Defaults are claude harness, repo depth, no includes. Enter ships the install.

## CI / scripted install

Pass flags directly to skip the prompt:

```bash
# Claude Code + Codex + OpenClaw, full workspace
brigade init --target ~/agent-kitchen \
  --depth workspace \
  --harnesses claude,codex,openclaw

# Codex-only project, minimal install
brigade init --target ./my-project --depth repo --harnesses codex

# Generic layout, no harness-specific files
brigade init --target ./my-project --harnesses none
```

## Verifying

After install, `brigade doctor --target <path>` reports the apparent harness shape and checks every configured inbox and adapter:

```
brigade doctor: target /home/you/agent-kitchen
  harnesses: claude, codex, openclaw (owner=openclaw, depth=workspace)
  [ok]   bootstrap: AGENTS.md   /home/you/agent-kitchen/AGENTS.md
  [ok]   handoff: claude inbox  /home/you/agent-kitchen/.claude/memory-handoffs
  [ok]   handoff: codex inbox   /home/you/agent-kitchen/.codex/memory-handoffs
  [ok]   openclaw: config        /home/you/.openclaw/openclaw.json
  ...
```

A `[fail]` line means the install is incomplete; `[warn]` is informational; `[todo]` means the check needs your attention (e.g. Hermes is experimental).

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

## Next steps

- Read [the cookbook](https://github.com/escoffier-labs/solos-cookbook) for the deep version of every concept here.
- Customize `USER.md` and `TOOLS.md` with your real preferences and runbooks (kept private; do not commit personal details).
- Wire the ingester on a cron or a manual end-of-day workflow.
- Add a memory-care staleness scan when your card set starts to matter. See `memory/cards/memory-care-staleness.md`.
- If you use TokenJuice, wire Claude Code and Codex hooks deliberately and tell agents what the wrapper means. See `memory/cards/tokenjuice-output-compaction.md`.
- Run `brigade work bootstrap` inside active repos when you want the dogfood-backed daily work loop, scanner inbox, and local evidence receipts.
