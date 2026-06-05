# Roadmap Archive

This file holds roadmap items that left the active queue. Keep `ROADMAP.md` focused on current direction and keep old completion detail here when it is still useful evidence.

## Archive Policy

- Move implemented or intentionally closed roadmap items here after the release or hardening pass that closed them.
- Keep active, planned, and still-owned deferrals in `brigade roadmap audit`.
- Use `carried-forward` when an item is valid future direction but no longer belongs in the current hardening queue.
- Keep archive entries public-safe: no private repo names, private paths, hostnames, raw logs, raw chat, tokens, or exact private owner names.
- Update this file and the parser-backed archive records together.

Check the archive with:

```bash
brigade roadmap archive
brigade roadmap audit
```

## Archived Items

### Deeper Roadmap Ownership Modeling

- id: `deeper-roadmap-ownership-modeling`
- status: carried-forward
- closed phase: 62
- owner: roadmap
- reason: Closed for the completion-hardening queue with explicit deferred ownership records; richer roadmap workflow state belongs in the next roadmap.

### Private Pattern Source Aliases

- id: `private-pattern-source-aliases`
- status: implemented
- closed phase: 62
- owner: roadmap
- reason: Closed for public hardening by keeping exact private reference names out of public docs and using neutral pattern families.

### Outbound Backup Operator Status Messages

- id: `outbound-backup-status-messages`
- status: out-of-scope
- closed phase: n/a
- owner: backup
- reason: Closed as out of scope for the local read-only operator loop; outbound notification behavior belongs behind an explicit future surface.

### Context Pack Writes Into Harness Destinations

- id: `context-harness-destination-writes`
- status: carried-forward
- closed phase: 70
- owner: context
- reason: Closed for the foundation by shipping read-only sync plans and receipts; destination writes require a future explicit apply command.

### Repo-Shareable Workflow Rule Templates

- id: `repo-shareable-workflow-rule-templates`
- status: implemented
- closed phase: 79
- owner: templates
- reason: Closed with public-safe repo templates and work doctor visibility.

### Stale Active Issue Repair Imports

- id: `stale-issue-repair-imports`
- status: implemented
- closed phase: 80
- owner: work
- reason: Closed with local repair imports for stale or unreadable issue-backed task context.

### Cross-Producer Provenance Audits Across Historical Sources

- id: `cross-producer-provenance-audit`
- status: implemented
- closed phase: 64
- owner: work
- reason: Closed with work import provenance checks and inbox doctor provenance contract warnings.

### Expanded Chat Export Provider Aliases And Parser Fixtures

- id: `expanded-chat-export-parsers`
- status: implemented
- closed phase: 66
- owner: chat
- reason: Closed with provider alias normalization, starter surfaces, JSONL fixtures, sweep review, task promotion, and handoff promotion.

### Separate Tool Projection Parity Closeout Receipt

- id: `tool-projection-parity-closeout`
- status: implemented
- closed phase: 68
- owner: tools
- reason: Closed with tools parity status and closeout receipts, doctor and brief integration, and changed-fingerprint resurfacing.

### Rich Accepted-Risk Quieting Across Learning Sources

- id: `learning-accepted-risk-quieting`
- status: implemented
- closed phase: 74
- owner: learn
- reason: Closed with learning closeout records for accepted-risk, dismissed, archived, and deferred outcomes.

### Dependency-Free Security SARIF Output

- id: `security-sarif-output`
- status: implemented
- closed phase: 76
- owner: security
- reason: Closed with dependency-free SARIF 2.1.0 output in security scan bundles and `brigade security sarif` regeneration.

### Safe Memory-Care Autofix Planning

- id: `safe-memory-autofix-planning`
- status: implemented
- closed phase: 83
- owner: memory
- reason: Closed with mutation-free `brigade memory care plan-fixes` planning and blocked-plan reporting.

### Safe Repo Root Discovery From Configured Roots

- id: `recursive-repo-root-discovery`
- status: implemented
- closed phase: 93
- owner: repos
- reason: Closed with dry-run `brigade repos discover plan`, configured-root parsing, safe candidate labels, include/exclude/max-depth handling, and path redaction.
