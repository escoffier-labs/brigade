# Inspiration Pattern Registry

Brigade keeps public inspiration records at the pattern-family level. Public docs describe the workflow shape Brigade is adopting, not the exact private repos, owners, organizations, raw examples, or local evidence that inspired the work.

Use:

```bash
brigade roadmap patterns
brigade roadmap patterns --json
```

## Pattern Families

- `command-harness-patterns`: command surfaces, subcommands, project settings, permission boundaries, and local command discovery.
- `delivery-loop-patterns`: think, plan, implement, review, verify, release, and learn loops with receipts.
- `durable-memory-eval-patterns`: markdown-backed handoffs, memory repair candidates, replayable eval records, and bounded learning cycles.
- `portable-skill-patterns`: small composable skills, portable source files, projection planning, and conflict-safe apply.
- `agent-security-guardrails`: local security scans, prompt and instruction risk checks, tool permission findings, and redacted evidence bundles.
- `context-engineering-packs`: planned context pack creation, freshness checks, and harness-specific export receipts.
- `cross-harness-skill-plugin-sync`: explicit sync plans, managed metadata, add-only defaults, dry-run output, and no silent deletes.
- `local-side-project-categories`: safe fleet metadata, disposition planning, and reviewed local work imports.
- `mcp-tooling`: local server metadata, tool listing, call planning, runtime checks, and receipts.
- `portable-tools`: catalog, projection, contract, approval, policy, runtime, execution, history, replay, checkpoint, and local MCP execution flows.
- `security-gates`: publish readiness checks, introduced-content checks, suppressions, accepted-risk records, and release blockers.
- `self-learning`: learning output becomes reviewed tasks, handoffs, suppressions, or receipts, not unbounded self-mutation.
- `release-gates`: verification, closeout, review, scanner, security, docs, changelog, roadmap, candidate bundle, and manual publish-plan checks.

## Decision Types

- `bake-in`: the pattern is small and belongs directly in Brigade.
- `integrate`: the pattern should stay in its own tool but report into Brigade through receipts or imports.
- `catalog-only`: Brigade should inspect and track it, not own execution or projection.
- `move-candidate`: the repo or tool may need a reviewed migration or consolidation plan.
- `leave-alone`: the reference is useful context but not Brigade scope.

## Public Boundary

Do not put exact private reference repo names, side-project names, owner names, organization names, private paths, raw logs, private config, tokens, or raw evidence in this file, public fixtures, handoffs, imports, release evidence, or committed diffs. Exact local source names belong only in gitignored host-local config when the operator needs them.
