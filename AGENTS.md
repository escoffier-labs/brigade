# Brigade Agent Instructions

Use these instructions when a user points you at the Brigade repository and asks you to install, evaluate, or adapt Brigade for their workspace.

## Start Here

Read these files first:

1. `README.md`
2. `docs/new-user-quickstart.md`
3. `docs/agent-assisted-setup.md`

Brigade is local-first workspace wiring. Local-first means local data on the operator-controlled machine first, before any external service; that machine can be a laptop, workstation, or VPS. Brigade should help the user adapt their existing memory, handoff, and agent workflow instead of replacing it with someone else's exact layout.

## Installing Brigade For A User

When the user wants Brigade installed in a target repo or operator workspace, work in that target directory and run:

```bash
pipx install brigade-cli
brigade --version
brigade operator quickstart --target . --harnesses codex --dry-run
brigade operator quickstart --target . --harnesses codex
brigade operator doctor --target . --profile local-operator
```

For an OpenClaw or Hermes workspace rather than a code repo, prefer workspace depth:

```bash
brigade operator quickstart --target . --depth workspace --harnesses openclaw,hermes --owner openclaw --dry-run
brigade operator quickstart --target . --depth workspace --harnesses openclaw,hermes --owner openclaw
brigade operator doctor --target . --profile local-operator
```

If the user uses more than one harness, use a comma-separated list:

```bash
brigade operator quickstart --target . --harnesses codex,claude,opencode
```

If you are unsure which harnesses the user uses, start with the current harness and explain how to add more later.

## Adapting Existing Setups

Before changing files, inspect the target directory for existing setup:

- `AGENTS.md`
- `CLAUDE.md`
- `MEMORY.md`
- `TOOLS.md`
- `.codex/`
- `.claude/`
- `.opencode/`
- `.hermes/`
- `.openclaw/`
- `.mcp/`

Preserve the user's existing memory owner, conventions, repo layout, and tool-specific docs when possible. Prefer adding compatibility wiring such as handoff inboxes, shared instruction files, portable tool sources, or scanner config.

Do not force the user into Brigade's example layout when they already have a working homegrown setup. Do not assume the target must be a git repo; an OpenClaw/Hermes memory workspace or VPS operator directory is also a valid target.

## Local And Shareable Files

Usually safe to commit after review:

- `AGENTS.md`
- `MEMORY.md` and reviewed memory cards if this repo owns memory
- `rules/`
- `tools/`
- public docs

Usually local-only:

- `.brigade/`
- `.codex/`
- `.claude/`
- `.opencode/`
- `.hermes/`
- `.openclaw/`
- `.mcp/`
- generated `scripts/` projections

Do not commit generated local state unless the user explicitly asks and the Brigade docs say it is repo-shareable.

## Safety Boundaries

Do not start daemons, install schedulers, publish, push, tag, deploy, mutate remotes, install hooks, or run destructive commands as part of Brigade setup unless the user explicitly asks for that action.

Do not paste raw scanner output, session text, tokens, API keys, private hostnames, private repo names, or unredacted absolute paths into public issues or docs.

If setup fails, collect machine-readable output and summarize it after redaction:

```bash
brigade operator quickstart --target . --harnesses codex --json
brigade operator doctor --target . --profile local-operator --json
brigade tools doctor --target . --json
brigade skills doctor --target . --json
```

Use the "Quickstart setup problem" issue form:

```text
https://github.com/escoffier-labs/brigade/issues/new/choose
```

## Success Criteria

Report the exact commands you ran. A healthy first run should end with:

```text
quickstart: ok
operator doctor: ready yes
blocking issues: 0
```

If anything remains manual, list the remaining steps clearly and do not hide warnings.
