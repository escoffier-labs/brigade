# Security Policy

## Supported versions

solo-mise is in alpha. Only the latest minor release on the `main` branch receives security fixes. Pin to a released tag if you need a known-good version.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security problems. Email **srneas@gmail.com** with: <!-- content-guard: allow pii/email -->


- A short description of the issue.
- Steps to reproduce (or a minimal proof of concept).
- The version or commit you tested against.
- Whether you would like to be credited in the release notes.

You should get an acknowledgment within 72 hours. If you do not, please follow up - the mail may have been filtered.

## In scope

- Code execution, path traversal, or symlink-attack flaws in `solo-mise init`, `doctor`, `scrub`, or the ingester.
- Template content that leaks credentials, tokens, or personal data into a target workspace.
- Public-leak guard bypasses (cases where the content-guard pre-push hook fails to flag content it is configured to catch).
- Profile manifests that write outside `--target` (the manifest validator should reject these).

## Out of scope

- Bugs in `content-guard` itself - please report those upstream at
  <https://github.com/escoffier-labs/content-guard>.
- Bugs in OpenClaw, Hermes, Claude Code, or Codex - report those to their respective projects.
- Issues that require an attacker to already have write access to the user's machine, harness config, or PyPI account.
- Memory cards or handoffs that a user wrote and committed themselves. solo-mise provides scaffolding and guardrails, not perfect content review.

## Disclosure

We aim to ship a fix within 14 days of confirming a valid report. A coordinated disclosure timeline can be negotiated for issues that need longer.
