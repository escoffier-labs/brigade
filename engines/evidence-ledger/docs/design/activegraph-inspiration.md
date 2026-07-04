# ActiveGraph inspiration: fork and diff

Part of a fleet-wide effort inspired by Yohei Nakajima's
[ActiveGraph](https://github.com/yoheinakajima/activegraph). Master notes:
`~/notes/activegraph-credits.md`. ActiveGraph is a reference architecture only;
no code from it is vendored or depended on here.

## What we borrowed

Two ideas, both from ActiveGraph's runtime:

- **Fork** — branch a state into a new namespace so it can diverge independently.
  In ActiveGraph, `SQLiteEventStore.fork_run` (`activegraph/store/sqlite.py`)
  copies the event rows up to a cut point into a new run id, then replays them
  without re-firing behaviors, so the shared prefix is never recomputed.
- **Diff** — a structural comparison of two resulting states. ActiveGraph's
  `compute_diff` (`activegraph/runtime/diff.py`) is a set-diff over final objects
  with provenance stripped so equality is structural, not incidental.

## How it landed in miseledger

- `miseledger fork <dest>` snapshots the live ledger into a standalone branch
  database with `VACUUM INTO`. The branch is an ordinary ledger you can import
  experimental sources into without touching the canonical one.
- `miseledger diff [<base>] <fork>` reports added / changed / removed items
  (grouped by logical identity, keyed on content hash) plus added / removed
  relations. It is strictly read-only.

The payoff: the ledger already accumulates immutable, content-addressed evidence,
but you could not ask "what did *this* source actually add or change?" Fork the
ledger, import the source into the fork, diff. That question now has a one-command
answer, and the answer is exact because it is a content-hash set-diff.

## What we did differently, and why

- **Fork copies the whole database, not an event log.** miseledger's state is
  already content-addressed (`items.content_hash` + the
  `unique(source_id, collection_id, external_id, content_hash)` key in
  `internal/archive/db.go`), so state is reproducible *by construction* — there is
  no need to replay an event stream to rebuild it. ActiveGraph replays events
  because its state is a projection of the log; miseledger's state is already the
  durable, immutable truth, so a consistent file-level snapshot (`VACUUM INTO`) is
  the honest analogue of fork here. It also gives us no-clobber semantics for free
  (VACUUM INTO refuses to overwrite).
- **Diff is over content-hash sets, not an event longest-common-prefix.** Because
  items are immutable and accumulate, an edit appears as a new hash under the same
  identity. So "changed" means: the identity exists on both sides but its set of
  content hashes differs. "Added"/"removed" are identity-level set differences.
  This matches miseledger's data model rather than ActiveGraph's event model.
- **No provenance stripping needed.** ActiveGraph strips provenance before
  comparing objects; miseledger's identity/content-hash keys already exclude
  incidental metadata, so structural equality falls out naturally.

## Feedback worth sending Yohei

miseledger is a useful counter-example to event-sourcing: content-addressed,
immutable rows give you replayable/forkable state *without* an event log at all.
Event sourcing is one way to make state reproducible; content-addressing is
another, and where it fits it removes the need to fold a log on every read. The
`events` table miseledger does have is effectively vestigial (a 1:1 shadow of
items) precisely because the content-hash model already carries the weight an
event log would.
