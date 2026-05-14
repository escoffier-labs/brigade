# Quickstart

Five minutes from clone to a working agent kitchen.

## 1. Install

```bash
pipx install git+https://github.com/solomonneas/solo-mise
```

If you do not have `pipx`:

```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
```

## 2. Pick a profile

The default `repo` profile adds the handoff flow and a publish guard to an existing project.

The `workspace` profile creates a home-style agent workspace from scratch.

## 3. Initialize

### Repo profile (lightest)

In a project you already work in:

```bash
cd ~/repos/your-project
solo-mise init --target .
```

This creates:

```text
your-project/
  AGENTS.md
  CLAUDE.md
  .claude/memory-handoffs/
    TEMPLATE.md
  hooks/
    pre-push
```

Enable the pre-push hook once:

```bash
git config core.hooksPath hooks
```

### Workspace profile

```bash
solo-mise init --target ~/agent-kitchen --profile workspace
```

This creates the full bootstrap file set: `AGENTS.md`, `CLAUDE.md`, `SOUL.md`, `USER.md`, `TOOLS.md`, `MEMORY.md`, `IDENTITY.md`, `HEARTBEAT.md`, `SAFETY_RULES.md`, `INSTALL_FOR_AGENTS.md`, plus `memory/cards/` with starter cards, a `.claude/memory-handoffs/` inbox, and the publish hook.

## 4. Verify

```bash
solo-mise doctor --target ~/agent-kitchen
```

The doctor checks that the bootstrap files exist, the handoff inbox is in place, and (if you chose `--profile openclaw`) the OpenClaw config can see your workspace. It prints `OK` or `MANUAL ACTION NEEDED` per check; it never edits your config.

## 5. Write your first handoff

```bash
solo-mise handoff-template --target ~/agent-kitchen > \
  ~/agent-kitchen/.claude/memory-handoffs/$(date -u +%Y-%m-%dT%H%MZ)-first-handoff.md
```

Edit the file with whatever durable knowledge your agent just produced.

## 6. Ingest

```bash
solo-mise ingest --target ~/agent-kitchen --dry-run
solo-mise ingest --target ~/agent-kitchen
```

The ingester is conservative. Handoffs with `Recommended memory action: create-card` and a safe filename + frontmatter become memory cards. Handoffs that route to `TOOLS.md`, `USER.md`, `rules/*.md`, or `.learnings/*.md` get appended. Anything ambiguous lands in `memory/handoff-inbox/` for manual review.

If you administer multiple agent setups, keep one canonical owner and pull remote handoffs into staging directories before ingesting them. See `memory/cards/multi-workspace-handoff-admin.md`.

## 7. Scrub before publishing

```bash
solo-mise scrub --target . --dry-run
```

If you have [content-guard](https://github.com/solomonneas/content-guard) installed, the pre-push hook will block pushes that contain private IPs, secrets, or AI attribution trailers. `solo-mise scrub` is the deterministic counterpart that runs the same scanner with the public-repo policy.

## 8. OpenClaw users

```bash
solo-mise doctor --target ~/.openclaw/workspace --harness openclaw
solo-mise openclaw-fragments --out ./openclaw-fragments
```

The fragments are JSON files you can inspect and merge into your `openclaw.json` by hand. `solo-mise` never mutates your live OpenClaw config.

## Next steps

- Read [the cookbook](https://github.com/solomonneas/solos-cookbook) for the deep version of every concept here.
- Customize `USER.md` and `TOOLS.md` with your real preferences and runbooks (kept private; do not commit personal details).
- Wire the ingester on a cron or a manual end-of-day workflow.
- Add a memory-care staleness scan when your card set starts to matter. See `memory/cards/memory-care-staleness.md`.
