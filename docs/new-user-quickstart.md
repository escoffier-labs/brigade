# New User Quickstart

Brigade is local-first. The first run should create local config, handoff inboxes, and portable tool or skill projections without starting services or touching remotes.

## Install

```bash
pipx install brigade-cli
brigade --version
```

## Preview A Setup

Run the quickstart in dry-run mode first:

```bash
brigade operator quickstart --target ./my-repo --harnesses codex --dry-run
```

Use a comma-separated harness list if you use more than one agent surface:

```bash
brigade operator quickstart --target ./my-repo --harnesses codex,claude,opencode --dry-run
```

## Apply The Setup

```bash
brigade operator quickstart --target ./my-repo --harnesses codex
brigade operator doctor --target ./my-repo --profile local-operator
```

Expected shape:

```text
quickstart: ok
operator doctor: ready yes
blocking issues: 0
```

For a machine-readable first-run report:

```bash
brigade operator quickstart --target ./my-repo --harnesses codex --json
```

In a healthy run, the JSON has `status: "ok"` and `issue_report.status: "ok"`.

Quickstart runs these local-only steps:

- installs Brigade repo templates
- writes host-local `.brigade/` operator config
- imports built-in portable tools and skills
- projects harness-specific files such as Codex skills or Claude command docs
- verifies selected handoff writer inboxes
- prints next commands

It does not start daemons, install hooks, publish, push, tag, or mutate remotes.

## What To Commit

Commit repo-shareable source files only. Keep generated and local state ignored.

Usually safe to commit:

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

## If Quickstart Fails

First collect the compact report:

```bash
brigade operator quickstart --target ./my-repo --harnesses codex --json
```

Copy the `issue_report` object into a GitHub issue after reviewing it. Do not paste tokens, private hostnames, private repo names, or unredacted absolute paths.

Useful follow-up commands:

```bash
brigade operator doctor --target ./my-repo --profile local-operator --json
brigade operator verify-harness --target ./my-repo --harness codex --json
brigade tools doctor --target ./my-repo --json
brigade skills doctor --target ./my-repo --json
brigade security scan --target ./my-repo --fail-on none --json
```

Open a quickstart issue here:

```text
https://github.com/escoffier-labs/brigade/issues/new/choose
```

Use the "Quickstart setup problem" form. The more exact the command and redacted output are, the faster the fix can land.
