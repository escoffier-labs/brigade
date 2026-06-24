# Security Policy

## Supported versions

Brigade is in alpha. Only the latest minor release on the `main` branch receives security fixes. Pin to a released tag if you need a known-good version.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security problems. Email **me@solomonneas.dev** with: <!-- content-guard: allow pii/email -->


- A short description of the issue.
- Steps to reproduce (or a minimal proof of concept).
- The version or commit you tested against.
- Whether you would like to be credited in the release notes.

You should get an acknowledgment within 72 hours. If you do not, please follow up - the mail may have been filtered.

## In scope

- Code execution, path traversal, or symlink-attack flaws in `brigade init`, `doctor`, `scrub`, or the ingester.
- Template content that leaks credentials, tokens, or personal data into a target workspace.
- Public-leak guard bypasses (cases where the content-guard pre-push hook fails to flag content it is configured to catch).
- Profile manifests that write outside `--target` (the manifest validator should reject these).

## Runbooks execute arbitrary shell

`brigade runbook run` executes the `run` string of every step as a shell command on the operator's machine. Treat a runbook file as **arbitrary shell that is only as trustworthy as whoever wrote it** - including any agent that can write a `.json` file into the workspace.

- Execution requires the **operator** to pass `--approved` on the command line. An `"approved": true` baked into the runbook file is **ignored** and never authorizes execution. This keeps a human in the loop: run `brigade runbook plan <file>` (or `runbook run <file> --dry-run`) to read every command before approving.
- The `allowed_commands` allowlist validates the whole command, not just the first token, and refuses an inline-script shell wrapper (for example `bash -c "..."`) because such a wrapper can run anything regardless of the allowlist.
- The built-in destructive-pattern deny-list is **advisory only**. It catches a few obvious shapes but is trivially bypassable (`find / -delete`, `dd`, `curl ... | sh`, and so on). Do not rely on it as a security boundary; the boundary is the operator reviewing the steps before approving.

## Out of scope

- Bugs in `content-guard` itself - please report those upstream at
  <https://github.com/escoffier-labs/content-guard>.
- Bugs in OpenClaw, Hermes, Claude Code, or Codex - report those to their respective projects.
- Issues that require an attacker to already have write access to the user's machine, harness config, or PyPI account.
- Memory cards or handoffs that a user wrote and committed themselves. Brigade provides scaffolding and guardrails, not perfect content review.

## Disclosure

We aim to ship a fix within 14 days of confirming a valid report. A coordinated disclosure timeline can be negotiated for issues that need longer.
