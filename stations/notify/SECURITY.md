# Security Policy

## Supported versions

agent-notify is pre-1.0. Only the latest tagged release receives security fixes. Pin to a released tag if you need a known-good version.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security problems. Email **me@solomonneas.dev** with: <!-- content-guard: allow pii/email -->

- A short description of the issue.
- Steps to reproduce (or a minimal proof of concept).
- The version or commit you tested against.
- Whether you would like to be credited in the release notes.

You should get an acknowledgment within 72 hours. If you do not, please follow up - the mail may have been filtered.

## In scope

- Code execution, path traversal, or symlink-attack flaws in `agent-notify init`, `doctor`, `status`, `hooks print`, or the send path.
- A message body, title, or tag value that escapes its channel encoding (Discord embed, Telegram Markdown V2, Signal plain text) in a way that lets attacker-controlled input forge formatting or inject a payload.
- Secret leakage: any path by which a token, webhook URL, or chat ID is written to disk, logged to stderr, or echoed to stdout. The tool is designed to keep secrets in env vars and never persist them.
- Outbound HTTP to a host other than the channel API you configured (the privacy posture promises egress only to configured channel URLs).

## Trust model

- `agent-notify` reads a notification message from stdin or argv and sends it to channels you configured. The message body is **attacker-influenced** whenever the upstream agent's output is (for example, a tool result echoed into a Stop-hook event). Channel adapters are responsible for encoding that body safely; an encoding escape is in scope above.
- Channel credentials are read from environment variables named in your config. The config file stores env-var **names**, not literal secrets. A config that embeds a literal token is user error, not a tool vulnerability, but a tool path that *causes* a literal secret to be persisted is in scope.
- The hook adapters parse third-party event JSON (Claude Code, Codex CLI, and custom sources). Malformed or hostile JSON should fail closed with a non-zero exit, never crash with a secret in the panic trace.

## Out of scope

- Bugs in Discord, Telegram, or the Signal CLI themselves. Report those to their respective projects.
- Bugs in the agent harnesses (Claude Code, Codex, OpenClaw, Hermes). Report those to their respective projects.
- Issues that require an attacker to already have read access to your environment variables or write access to your config file. If they have your env, they have your tokens regardless of this tool.
- A channel dropping or rate-limiting a notification. That is a documented v1 limitation (exit code `3`), not a security flaw.

## Disclosure

We aim to ship a fix within 14 days of confirming a valid report. A coordinated disclosure timeline can be negotiated for issues that need longer.
