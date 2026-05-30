# Local Operator Center

`brigade center` is a read-only CLI view over local Brigade state. It is meant for wrappers and future UI experiments that need one stable JSON surface without starting a server, database, daemon, scheduler, or sync engine.

Commands:

```bash
brigade center status
brigade center status --json
brigade center activity
brigade center activity --json
brigade center reviews
brigade center reviews --json
brigade center templates
brigade center templates --json
brigade center report plan
brigade center report build
brigade center report list
brigade center report show <report-id>
brigade center report archive <report-id>
```

`status` summarizes active work, pending tasks, pending imports, scanner sweep health, review health, handoff drafts, tool catalog health, learning candidates, context packs, release readiness, release candidates, repo fleet, roadmap health, project consolidation, and security health.

`activity` reads local receipts and pack metadata across work sessions, scanner runs, scanner sweeps, review runs, context packs, release readiness receipts, and release candidates.

`reviews` returns one pending local review queue across work imports, learning candidates, project consolidation issues, and context pack health. Each row includes:

- owning subsystem
- local id
- status
- priority or severity when known
- safe summary
- suggested next command

`templates` lists local workflow templates for context packs, tool packs, project audits, release candidates, and review closeouts.

Every center row uses the same wrapper-facing fields: `subsystem`, `local_id`, `status`, `priority`, `severity`, `safe_summary`, `created_at`, `updated_at`, `receipt_path`, `path`, and `suggested_next_command`.

## Operator Reports

`brigade center report build` writes a local bundle under:

```text
.brigade/center/reports/
```

Each bundle contains:

- `OPERATOR_REPORT.md`, a daily review summary with review queue, activity, and suggested next commands.
- `OPERATOR_REPORT.html`, a dependency-free escaped static rendering of the Markdown report.
- `CENTER_EVIDENCE.json`, stable JSON evidence for wrappers.

`brigade center report plan` previews the same evidence without writing. `list`, `show`, and `archive` inspect or move local bundles. Report health warns when the latest bundle is stale, references missing receipts, was built from an older git HEAD, or newer center activity exists. `brigade work brief`, `brigade work doctor`, `brigade release doctor`, and release candidate evidence surface those report health checks.

The operator center never invokes scanners, tools, reviewers, handoff ingestion, release publishing, git commands that mutate state, or remote APIs. Only `center report build` and `center report archive` write local gitignored report bundle files.
