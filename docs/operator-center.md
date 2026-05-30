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

The operator center never invokes scanners, tools, reviewers, handoff ingestion, release publishing, git commands that mutate state, or remote APIs. It only reads existing local files and prints text or JSON.
