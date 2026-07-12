# acpx Cursor Adapter

## Goal

Add an opt-in, one-shot Cursor ACP transport without adding a runtime
dependency or adopting acpx session ownership.

## Boundary

- Require user-installed acpx 0.12.0 and Cursor CLI with `cursor-agent acp`.
- Invoke acpx with its raw agent command escape hatch.
- Use strict JSON, no terminal capability, explicit timeout, cwd, model, and
  permission mode.
- Allow writable ACP only in a Brigade-created worktree.
- Parse ACP protocol 1 NDJSON into the common agent result.
- Keep direct Cursor as the default and never fall back silently.
- Do not add persistent sessions, cancellation, queue recovery, Node, an ACP
  SDK, or a separate repository.

## Verification

- [x] Test exact version and command construction.
- [x] Test strict NDJSON, protocol metadata, final text, and model acknowledgment.
- [x] Test invalid output, empty final output, timeout, and permission failures.
- [x] Test roster validation and dispatch selection.
- [x] Run focused tests and `./scripts/verify` through Brigade.
- [x] Attempt a live local smoke test and record the environmental blocker or result.

## Live result

The installed Cursor CLI `2026.07.09-a3815c0` completed an ACP initialize
handshake at protocol version 1 and advertised load-session, MCP, prompt, and
session capabilities. No model prompt was sent. A full acpx smoke run could not
start because `acpx` is not installed on this machine. Brigade does not install
it automatically.
