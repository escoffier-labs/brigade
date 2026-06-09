# Brigade Announcement Draft

Superseded 2026-06-09: the channel-ready posts (Show HN, Reddit, short social) live in [announcement-post.md](announcement-post.md), rewritten in the README's first-person origin-story voice. Use that file; this one keeps only the supporting command blocks.

Install:

```bash
pipx install brigade-cli
brigade --version
```

Start with a repo:

```bash
brigade operator quickstart --target ./my-repo --harnesses codex --dry-run
brigade operator quickstart --target ./my-repo --harnesses codex
brigade operator doctor --target ./my-repo --profile local-operator
```

Start with an existing operator workspace:

```bash
brigade operator adopt plan --target ~/agent-workspace --json
brigade operator adopt capture --target ~/agent-workspace --json
brigade operator migration status --target ~/agent-workspace --json
brigade operator surfaces capture --target ~/agent-workspace --json
brigade operator surfaces reviews --target ~/agent-workspace --json
```

Real dogfood evidence available for posts: Brigade adapted the maintainer's production operator workspace. It discovered external scheduler/process surfaces, captured 57 redacted records, reviewed every record, kept 36 externally owned, marked 14 as runbook migration candidates and 7 as retirement-review candidates, then reached `operator migration doctor: ready` with the first runbook (the memory-ingest wrapper) executed with receipts.
