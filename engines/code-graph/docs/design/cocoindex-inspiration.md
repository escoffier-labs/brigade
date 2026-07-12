# CocoIndex inspiration: incremental honesty for a code graph

Part of the same credit-the-source series as
[activegraph-inspiration.md](activegraph-inspiration.md). Reference project:
[CocoIndex](https://github.com/cocoindex-io/cocoindex) (Apache 2.0), a Rust-core
data transformation framework built around one stance: the target store is a
materialized view of the source, `TargetState = Transform(SourceState)`, and the
engine's job is to keep that view converged by reprocessing only what changed.
No CocoIndex code is vendored or depended on here. GraphTrail is a single-purpose
Rust sidecar with a SQLite graph; what transferred is design, not code.

Key CocoIndex sources for the ideas below:
[incremental processing](https://cocoindex.io/blogs/incremental-processing/),
[functions/memoization](https://cocoindex.io/docs/programming_guide/function/),
[target state](https://cocoindex.io/docs/programming_guide/target_state/),
[CocoInsight](https://cocoindex.io/blogs/cocoinsight/),
[CLI / evaluate](https://cocoindex.io/docs/core/cli),
[cocoindex-code](https://github.com/cocoindex-io/cocoindex-code).

## What we borrowed, round by round

### Round 1 (shipped 0.3.0, 2026-07-08)

- **Dual fingerprint: hash(input) AND hash(logic) in the skip key.** CocoIndex
  memo keys combine a content fingerprint of the input with a fingerprint of the
  transformation code, plus an explicit `version=` bump for invalidation the
  code hash cannot see. GraphTrail's incremental sync skipped files by content
  hash alone, which lies the moment the extractor changes; the v3 body-hash
  migration needed a hand-rolled forced-full-reindex for exactly that reason.
  Schema v4 (PR #17) records a per-language `EXTRACTOR_FINGERPRINT` per file and
  compares both. Bump `rust-v1` to `rust-v2` and only `.rs` files re-extract,
  lazily, on the next pass. Our fingerprint is a hand-bumped constant, not a
  computed code hash: closer to their `version=` escape hatch than their
  `@coco.fn` hashing, chosen because a Rust build has no cheap runtime view of
  its own function bodies and a reviewed constant bump is auditable in a diff.
- **Refresh-before-query.** cocoindex-code's MCP search takes `refresh_index`
  (default true). GraphTrail's query tools gained opt-in `refresh: true`
  (PR #17): incremental sync before answering, fail-open, query itself still on
  a read-only connection. Default false, the opposite of theirs, because the
  default build promises no writes an agent did not ask for.
- **Freshness as a contract, not a vibe.** CocoIndex treats staleness as a
  first-class engine concern. `graphtrail doctor` (PR #18) is that stance as a
  command: schema status, last-sync age, four pending counts, branch drift, and
  a FRESH/STALE/NEEDS-MIGRATION verdict wired to exit 0/1/2 so hooks can gate.
- **Index warm at session start.** From cocoindex-code's editor integration: a
  SessionStart hook running `graphtrail sync` where `.graphtrail/` exists, so
  the graph is fresh before the first query. Chosen over a watch daemon because
  a no-op sync is sub-second.
- **First index gitignores its own data dir** (PR #17), straight from
  `ccc init` adding its data dir to `.gitignore`.

### Round 2 (shipped on master, schema v5/v6, 2026-07-09)

- **Lineage over replay.** CocoIndex deletes stale derived rows by tracked
  source-to-derived lineage instead of re-running old transforms, "robust to
  transformation logic non-determinism." GraphTrail's schema v5 is the same
  move for call edges: each file's unresolved calls persist in `pending_calls`,
  and the edges table is derived from that stored state. A one-file change
  re-parses one file, and resolutions in unchanged files pick up definitions
  added or removed elsewhere instead of going stale. Measured on a 16.7k-file
  TypeScript repo: one-file sync 2m52s before, 2.5s after; full-index RSS
  397 MB before, 136 MB after (streaming extraction, same row counts).
- **Rebuild derived data without touching sources.** Their schema-evolution
  story alters targets in place and backfills automatically. GraphTrail's v5
  to v6 upgrade re-resolves every edge (adding confidence scores) from stored
  pending calls without re-parsing a single file. The lineage table paid for
  itself one schema version later.

## Adopted stances that needed no code

- **Cheap check before expensive check.** Their memo layer probes mtime and
  conditional headers before hashing. GraphTrail's sync was already
  stat-first-then-content-hash; the same layering was retrofitted into
  code-search-api (PR #5) along with the content-addressed artifact cache,
  which is CocoIndex's `memo=True` applied to 493,641 existing embeddings and
  summaries.
- **Convergent roll-forward.** No rollback anywhere: failed refresh appends a
  `refresh_error` note and the next sync reconciles. Same shape as their
  failure isolation for background mounts.
- **Per-document extraction, global assembly.** Their knowledge-graph pattern
  (memoized per-document triple extraction, then dedup into a global graph) is
  structurally what GraphTrail already was: per-file extraction, cross-file
  resolution. Convergence, not a borrow, but worth recording as evidence the
  shape is right.

## Round 3 (shipped 2026-07-11)

All five candidates landed, ranked here by the fit assessment that ordered the
work. Shipped as: `evaluate` (dry-run extraction, zero DB writes), `explain`
(CLI + MCP edge lineage over the v6 resolution paths), `export`
(dot/GraphML/JSONL, file or symbol scope), `watch` (foreground debounced sync,
cargo feature `watch` on by default), and schema v7 line-independent symbol
identity, migrated in place from stored rows with zero re-parsing:

1. **`evaluate` dry run.** `cocoindex evaluate` materializes what a flow WOULD
   produce into a diffable directory without touching targets. GraphTrail
   analogue: extract a file or tree to stdout/JSON or a dump dir with zero DB
   writes. Pairs with fingerprints: bump an extractor, evaluate before/after,
   diff. Small build; the extractors are already pure functions of file
   content.
2. **Edge lineage explain.** CocoInsight's click-a-field-see-its-derivation,
   scoped to one edge: which resolution path produced it (schema v6 already
   stores confidence scored by path: import-strict 0.9 down to ambiguous 0.5),
   which import or container satisfied it, which pending call it came from.
   The data exists; this is a query and a renderer.
3. **Property-graph export.** Their uniform GraphDB target interface (Neo4j to
   Kuzu as a config change). GraphTrail analogue is an `export` command writing
   GraphML/Cypher/dot from the SQLite graph. Fits the sidecar rule since it
   writes a local file; enables visualization nobody can do against raw SQLite.
4. **`watch` live mode.** Their three-tier change capture (push, delta poll,
   full-scan fallback). Deferred in round 1; shipped in round 3 as an explicit
   foreground command (start it, Ctrl-C it), never a daemon, with the advisory
   sync lock preventing duplicate work against the timer fleet.
5. **Line-independent symbol identity.** Their stable component paths are how
   run N pairs with run N-1. GraphTrail's `symbols.id` baked in `start_line`,
   the known wart the diff worked around by re-keying on
   `(file_path, qualified_name, kind)`. Schema v7 fixes identity at the source
   with occurrence ordinals for same-named duplicates. The migration is the
   lineage payoff again: new ids derive from columns the symbols table already
   stores, so v5/v6 databases rewrite ids in place (symbols, pending calls,
   FTS) and re-resolve edges without re-parsing a file.

## Deliberately not taken

- **Semantic chunk embeddings** (`SplitRecursively`, cached embeddings): Code
  Search owns that lane on this machine. GraphTrail stays the exact-graph lane.
- **Target-state reconciliation with deletes** for evidence stores: MiseLedger
  archives are append-only on purpose.
- **The single-search-tool MCP surface** cocoindex-code bets on (one tool,
  claimed 70% token saving). GraphTrail bets the other way: thirteen narrow
  tools. Both cite token economy; watching which shape agents use better.
- **The framework itself.** Adopting the engine would mean adopting its
  internal state store and its chunker; GraphTrail gets the same properties
  from four schema versions of plain SQLite.

## Feedback worth sending upstream

The lineage table (their idea) turned out to be the enabler for cheap schema
migration (not something their docs advertise): v5 to v6 rebuilt every derived
edge with new scoring without touching a source file, because the intermediate
derivation state was already persisted. "Lineage makes deletes safe" is their
pitch; "lineage makes re-derivation free" was the surprise.
