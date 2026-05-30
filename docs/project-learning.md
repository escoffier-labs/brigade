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
ownership_ready = true
migration_blockers = []
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
brigade projects readiness plan
brigade projects readiness record
brigade projects readiness list
brigade projects readiness show latest
brigade projects closeout --status deferred --reason "reviewed externally"
brigade projects closeouts
brigade projects closeout-show latest
brigade projects import-issues
```

`brigade projects readiness plan` calculates decision-specific readiness for docs, license, security, release, ownership, and migration blockers. `record` writes a local receipt under `.brigade/projects/readiness/`, while `list` and `show` inspect those receipts. Release readiness and the operator center can reference the latest receipt, but Brigade does not run any migration command.

`brigade projects closeout` writes reviewed project migration closeouts under `.brigade/projects/closeouts/`. `reviewed`, `deferred`, and `archived` closeouts quiet unchanged readiness issues, while changed source fingerprints resurface in doctor, brief, and import routing. `superseded` records that a prior closeout is no longer valid and does not quiet the active issue.

Migration plans, readiness receipts, and closeouts are manual-only. Brigade does not transfer repos, archive repos, change visibility, push, tag, publish, or mutate remotes.

## Learning Loop

`brigade learn` aggregates local learning candidates from pending scanner imports, failed review receipts, and failed portable tool run receipts.

Commands:

```bash
brigade learn plan
brigade learn doctor
brigade learn import-issues
brigade learn closeout <candidate-id> --status accepted-risk --reason "reviewed"
brigade learn closeouts
brigade learn closeout-show latest
brigade learn replay export scenario-id --before-summary "before" --after-summary "after"
brigade learn replay list
brigade learn replay show latest
brigade learn replay compare latest
```

Every candidate should end in one reviewed path:

- task
- Memory Handoff draft
- suppression or accepted risk
- archive or dismissal

Learning closeouts support `accepted-risk`, `dismissed`, `archived`, and `deferred`. Unchanged closed-out candidates stop making the learning queue noisy, while changed source fingerprints reappear in `learn plan`, `learn doctor`, and `learn import-issues`.

Learning replay receipts store safe before/after summaries under `.brigade/learn/replays/`. `brigade learn replay compare` writes a compare receipt under `.brigade/learn/replay-compares/`, reports improved, unchanged, regressed, or unknown outcomes, and surfaces regressions in release evidence and operator-center reviews.

Learning receipts store safe summaries only. Brigade does not edit canonical memory, source files, tool configs, or policies automatically.
