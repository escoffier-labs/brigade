# Project Consolidation And Learning

Brigade keeps project consolidation and self-learning local, explicit, and reviewable.

## Project Consolidation

Configure safe local project records in gitignored `.brigade/projects.toml`.

```toml
[[project]]
id = "project-alias"
label = "Project Alias"
category = "workflow helper"
decision = "bake-in"
reason = "Small workflow primitive belongs directly in Brigade."
docs_ready = true
license_ready = true
security_ready = true
release_ready = true
```

Supported decisions:

- `bake-in`
- `integrate`
- `catalog-only`
- `move-candidate`
- `leave-alone`

Commands:

```bash
brigade projects audit
brigade projects audit --json
brigade projects import-issues
```

Migration plans are manual-only. Brigade does not transfer repos, archive repos, change visibility, push, tag, publish, or mutate remotes.

## Learning Loop

`brigade learn` aggregates local learning candidates from pending scanner imports, failed review receipts, and failed portable tool run receipts.

Commands:

```bash
brigade learn plan
brigade learn doctor
brigade learn import-issues
```

Every candidate should end in one reviewed path:

- task
- Memory Handoff draft
- suppression or accepted risk
- archive or dismissal

Learning receipts store safe summaries only. Brigade does not edit canonical memory, source files, tool configs, or policies automatically.
