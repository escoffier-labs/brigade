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

If `brigade --version` is not available after install, also include your OS and shell. Compare the installed version with the latest stable release on PyPI. The output has this form:

```text
brigade X.Y.Z
```

## Quickstart Warned Or Failed

Please prepare a local bug report in the affected workspace:

```bash
brigade bug report --target . --summary "What happened and what you expected" --dry-run
```

Review the complete preview for private information. Reuse its printed report
identifier with `brigade bug report --report-id REPORT_ID` to search for
duplicates and choose a destination. Use
`--include-output FILE` only for a log you want included. The command redacts
its tail before saving it. Answer the security question before sharing.
For agent submission, approve the preview and confirm this is an ordinary
defect before using `--yes --not-security`. Include the fingerprint when
discussing the report.

## Doctor Not Ready

Use the same bug report command above. The bundle includes bounded local health
probes for bootstrap files, AGENTS.md quality, and security-config file type under
`bug_diagnostics`. It does not include full Doctor output. For a failing hook,
profile, or component, use `--include-output FILE` to add the relevant output
from a file you choose. Inspect the full preview before sharing it. For a
vulnerability or exposed credential, follow SECURITY.md instead of filing a
public issue.

## What Files Do I Commit?

Brigade splits shareable source from local generated state.

Usually safe to commit after review:

- `AGENTS.md`
- `MEMORY.md` and reviewed memory cards if this repo owns memory
- `rules/`
- `tools/`
- public docs
- the managed handoff template for each selected writer harness — for Claude Code, `.claude/memory-handoffs/TEMPLATE.md` (required for Claude onboarding; `verify-harness` fails readiness when a global exclude hides it); for other writers, the matching `<inbox>/TEMPLATE.md`

Handoff inboxes stay local except each inbox's managed `TEMPLATE.md`. Session handoff files in those folders stay ignored.

Usually local-only:

- `.brigade/`
- `.codex/` (except `.codex/memory-handoffs/TEMPLATE.md` when Codex is selected)
- `.claude/` (except `.claude/memory-handoffs/TEMPLATE.md` when Claude Code is selected)
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
