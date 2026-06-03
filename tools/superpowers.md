# Superpowers

Use this cross-harness tool source to expose repo-reviewed workflows, skills, and repeatable operating patterns to any agent harness.

## Intent

Make higher-level capabilities discoverable across Claude, Codex, OpenCode, OpenClaw, Hermes, scripts, MCP adapters, and future harnesses without treating any one harness as canonical.

## Use When

- A repo has a reviewed workflow that should be reused by more than one harness.
- A command, skill, checklist, or operating pattern needs a single source document before projection.
- Agents need to know which local capability to use before starting work.

## Procedure

1. Start from the tracked source document in `tools/`.
2. Project only reviewed content into harness-specific locations.
3. Keep generated projections local unless the repo intentionally tracks them.
4. Re-run the relevant Brigade health check after changing source or projections.
5. Write a handoff when the available cross-harness capability changes in a durable way.

## Boundaries

- Do not auto-install harness plugins or skills.
- Do not overwrite harness-specific files without an explicit command.
- Do not store secrets, private host paths, tokens, or user-specific credentials in shared source docs.
- Do not use projections to bypass repo guidance, approvals, or safety rules.

## Useful Commands

```bash
brigade tools doctor --target .
brigade tools list --target .
brigade tools plan --target .
```

Use `brigade tools apply` only when the operator explicitly wants reviewed projections written.
