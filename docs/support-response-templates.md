# Support Response Templates

Use these as short first replies on GitHub issues or announcement replies. Always ask for redacted output. Do not ask people to paste tokens, private hostnames, private repo names, or unredacted absolute paths.

## Install Failed

Thanks for trying Brigade. Please share the redacted output from:

```bash
python3 --version
pipx --version
pipx install brigade-cli --verbose
brigade --version
```

If `brigade --version` is not available after install, also include your OS and shell. Expected current version is:

```text
brigade 0.8.1
```

## Quickstart Warned Or Failed

Please rerun quickstart with JSON and paste only the redacted `issue_report` object:

```bash
brigade operator quickstart --target . --harnesses codex --json
brigade operator doctor --target . --profile local-operator --json
brigade handoff doctor --target . --json
```

If you selected a different harness list, use that same `--harnesses` value in the rerun. Quickstart should only watch the writer harnesses you selected, so missing unwired side-harness folders should not warn on `0.8.1`.

## Doctor Not Ready

Please paste the redacted JSON from:

```bash
brigade operator doctor --target . --profile local-operator --json
brigade tools doctor --target . --json
brigade skills doctor --target . --json
brigade security doctor --target . --json
```

The fields that matter most are `ready`, `blocking_issue_count`, `blockers`, `tool_health`, and `security_issue_count`.

## What Files Do I Commit?

Brigade splits shareable source from local generated state.

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
- `.antigravity/`
- `.pi/`
- `.cursor/`
- `.hermes/`
- `.openclaw/`
- `.mcp/`
- generated `scripts/` projections

If you already have a homegrown setup, keep your existing owner and conventions. Brigade should add compatibility wiring, not force a new layout.

## Existing Homegrown Setup

Before changing files, inspect and capture a redacted adoption plan:

```bash
brigade operator adopt plan --target . --json
brigade operator surfaces capture --target . --json
brigade operator migration status --target . --json
brigade operator migration doctor --target . --json
```

These commands summarize local guidance files, handoff inboxes, and scheduler/process surfaces without storing raw cron lines, job names, process names, command paths, host details, or environment values.

## Security Or Secret Concern

Do not paste raw scanner output into a public issue. Share only redacted summaries from:

```bash
brigade security scan --target . --fail-on none --json
brigade security doctor --target . --json
```

For likely real credentials, move the value to `.env` or environment storage, rotate the credential, scrub any public history or transcript where it appeared, and record a local closeout after review.
