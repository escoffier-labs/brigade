# ActiveGraph inspiration: before/after graph diff

Part of a fleet-wide effort inspired by Yohei Nakajima's
[ActiveGraph](https://github.com/yoheinakajima/activegraph) and his two papers,
"The Log is the Agent" ([arXiv:2605.21997](https://arxiv.org/abs/2605.21997)) and
"Regimes" ([arXiv:2606.10241](https://arxiv.org/abs/2606.10241)). Master notes:
`~/notes/activegraph-credits.md`. ActiveGraph is a reference architecture only; no
code from it is vendored or depended on here.

## Why GraphTrail is different from the other four

Brigade, Hotwash, Cutsheet, and Miseledger took the *event-sourcing* half of
ActiveGraph: an append-only log, a projection, replay, fork. GraphTrail already
was the *other* half. Its whole job is a shared graph state (symbols, imports,
call edges in SQLite) that a human or agent queries before acting. It had no log
to replay.

What it was missing was ActiveGraph's **diff**: the ability to compare two
projections and say what changed. GraphTrail could answer "what depends on this
symbol right now?" but not "what did this change do to the call graph?"

## What we borrowed

- **Diff of two projections.** ActiveGraph's `compute_diff`
  (`activegraph/runtime/diff.py`) is a structural comparison of two resulting
  states with provenance stripped so equality is structural. GraphTrail's
  projection is the code graph, so the analogue is a structural diff of two
  indexed DBs.

## How it landed in GraphTrail

- **`graphtrail diff --before <db> --after <db> [--json]`** compares two graph DBs
  and reports added / removed / changed nodes and added / removed call edges. It
  opens both DBs read-only and writes nothing.
- **Nodes** are keyed by `(file_path, qualified_name, kind)` so a symbol that only
  moves lines is not a spurious remove+add. A node is `changed` when that key
  survives but its signature, line-span, or v3 per-symbol body hash differs.
- **Edges** are the `calls` set, canonicalized to
  `(source_file, source, line, target_file, target)` (the same canonical row the
  golden-corpus resolver test asserts on), diffed both ways. The JSON summary
  reports raw line-sensitive edge counts and line-insensitive counts that cancel
  call pairs whose only difference is line number.
- The JSON output is compact by design so Brigade can attach a code-graph delta to
  an outcome receipt: a promotion then records the actual structural change, not
  just a commit range.

Producing the two DBs needs no new code: `graphtrail --db before.db sync <root-at-ref-A>`
and `graphtrail --db after.db sync <root-at-ref-B>` already exist, so a caller
supplies a `git worktree` at each ref. Auto-worktree ref-awareness is a thin
convenience wrapper left for later.

Implementation: `src/query/diff.rs` (`diff_graphs`), `src/model.rs`
(`GraphDiff` / `DiffNode` / `DiffEdge`), the `Diff` arm in `src/cli.rs`, and
`tests/diff.rs`.

## What we did differently, and why

- **Two DB paths, not two git refs.** The minimal core is a pure read/compare.
  Ref-awareness (auto `git worktree` + double-sync) is kept out. The v1 diff
  touched nothing in sync or schema; schema v3 later added `symbols.body_hash`
  (with a one-pass reindex on upgrade) precisely so the diff could detect
  body-only edits, but git handling stays out of scope.
- **Full-graph query, not the traversal API.** The traversal helpers in
  `src/query/graph.rs` cap fan-out (`EDGE_CAP_PER_DIRECTION`) for interactive use.
  A diff must not truncate, so it reads the `edges` table directly.
- **Node identity excludes line, "changed" uses signature + span + body hash.** GraphTrail's
  `symbols.id` bakes in `start_line`, so keying on it would read every line shift
  as remove+add. Keying on `(file_path, qualified_name, kind)` and classifying
  "changed" by signature-or-span keeps a moved-but-identical symbol quiet. v3 adds
  `symbols.body_hash`, a per-symbol hash of the indexed line span, so a body edit
  that leaves the first line and span unchanged is now caught. Mixed v2/v3 diffs
  keep the older signature+span heuristic when either side lacks `body_hash`.

## Feedback worth sending Yohei

GraphTrail is the case where the graph existed but the diff did not. Event
sourcing gives you diff for free (fork two runs, compare projections). A tool that
maintains a graph *without* a log still wants the compare primitive, and it turns
out the hard part is choosing a node identity that survives re-indexing. Baking
line numbers into node ids (as the current `symbols.id` does) makes every edit
look like a churn of adds and removes; a line-independent identity plus an
explicit body fingerprint is what makes the diff read as "what changed" rather
than "what moved."
