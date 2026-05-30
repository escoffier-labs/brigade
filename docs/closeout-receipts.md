# Closeout Receipts

Closeout commands mark reviewed local state without mutating the underlying external system.

Commands added to the daily loop:

```bash
brigade work backup closeout
brigade security closeout
brigade handoff closeout
brigade memory care closeout
brigade work acceptance
brigade release candidate compare <candidate-id>
brigade release candidate closeout <candidate-id>
```

Backup closeouts record current backup-health issue fingerprints and safe summaries under `.brigade/backups/closeouts/`.

Security closeouts record reviewed or accepted-risk state for the latest local security report under `.brigade/security/closeouts/`.

Handoff closeouts record draft id, lint state, ingestion state, target card or document, source import reference, and safe fingerprints under `.brigade/handoffs/closeouts/`.

Memory-care closeouts record refresh queue fingerprints under `.brigade/memory-care/closeouts/`.

Release candidate closeouts write `CLOSEOUT.json` inside the candidate bundle and use one of these states:

- `draft`
- `reviewed`
- `superseded`
- `archived`

Closeout commands do not run tools, promote imports, ingest handoffs, edit memory, publish releases, create tags, push commits, or mutate remotes.
