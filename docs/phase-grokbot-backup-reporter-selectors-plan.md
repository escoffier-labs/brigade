# Grok Bot Backup Reporter Selector Plan

## Problem

Backup observations choose a public target alias or operation ID, but the adapter invokes each reporter with only its fixed runtime arguments. The shipped reporters require a selector on every call, so observations fail even when direct reporter execution succeeds.

## Decision

Append one validated selector pair at the fixed-command execution boundary:

- target status and restore readiness: `--target-alias <canonical registry alias>`
- operation status: `--operation-id <validated operation ID>`

The executable, working directory, environment, timeout, output bound, and configured arguments stay fixed. Execution continues to use an argument vector without a shell.

## Rejected alternatives

- Add selectors to runtime configuration. Selectors vary per request and would make a fixed runtime command select one resource permanently.
- Let reporter scripts infer a target or operation. That makes observations ambiguous and breaks the public request-to-reporter binding.
- Accept arbitrary selector text. Public identifiers already have a closed 64-character grammar and must be validated before they enter the process argument vector.

## Acceptance

- Target observation invokes the configured reporter with its fixed arguments followed by `--target-alias` and the canonical selected alias.
- Restore-readiness observation uses the same target selector contract.
- Operation observation validates its identifier and appends `--operation-id` with the exact validated value.
- Invalid or oversized identifiers fail before the reporter runner is called.
- Existing timeout, output, executable, and no-shell boundaries remain unchanged.
