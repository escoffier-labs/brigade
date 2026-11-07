# Memory retrieval eval harness (#722)

Local, offline eval that compares memory-search adapters over a checked-in
fixture corpus. This is the gate named by ROADMAP "Retrieval honesty": optional
on-device semantic retrieval lands only if it beats the grep baseline on the
hard query categories, not merely by tying on easy exact-match queries.

## Run

From a Brigade checkout with the package installed editable:

```bash
python -m brigade.memory_retrieval_eval
python -m brigade.memory_retrieval_eval --json
python -m brigade.memory_retrieval_eval --adapters current,grep
```

No public `brigade` subcommand is registered; this stays dev tooling so the
zero-dependency CLI surface does not grow a retrieval dependency.

## Corpus

| Piece | Count | Notes |
| --- | --- | --- |
| Cards | 40 | Real card format under `memory/cards/` (frontmatter + body) |
| Queries | 32 | Gold card ids in `queries.json` |
| K | 5 | Precision@K / Recall@K |

Query categories (weighted toward hard cases):

- `exact` — lexical overlap with title/body (baseline should do well)
- `paraphrase` — little shared vocabulary with the gold card
- `abbreviation` — short forms / initialisms
- `cross_tag` — query uses sibling vocabulary rather than the card's tags

## Adapters

1. **current**: production search, `brigade.memory_cmd.search_cards_payload`. Since #2196 this is plain BM25 (k1=1.2, b=0.75) over one field per card: title, tags, summary, and the card text.
2. **bm25**: the same BM25 ranker from `brigade.memory_bm25`, called directly with the index built once per run. It should match `current`; a gap means the production path drifted.
3. **substring**: the scorer production used before #2196, kept only as a baseline: a whitespace query term scores 3 when it is a substring of the title, tags, or summary, else 1 when it is a substring of the card text.
4. **grep**: naive tokenized substring match count. Deliberately dumb floor.
5. **semantic** (optional): on-device embeddings + cosine via `sentence_transformers`, only when the package is installed **and** a local model loads with `local_files_only=True`. Set `BRIGADE_MEMORY_EVAL_EMBED_MODEL` to a local path or cached model id. Otherwise the adapter is reported as skipped; the harness still exits 0.

Adapters 1 through 4 never touch the network and never download a model.

## Metrics

Per adapter and per category:

- Precision@K, Recall@K (default K=5)
- Hit rate (fraction of queries with at least one gold in the top K)
- Mean / median rank of the first gold hit (misses excluded from the mean)
- Oracle **ceiling** — best achievable score if every gold id is ranked first

Output is a small table plus optional JSON (`--json`) so runs are diffable.

## What this bench can and cannot differentiate

**Can**

- Show whether keyword scoring beats plain grep on paraphrase / abbreviation / cross-tag queries.
- Reject a semantic upgrade that only ties the baseline on aggregate scores driven by easy exact matches.
- Publish an honest ceiling so future claims cannot exceed what the fixture set allows.
- Exercise projection-state scenarios (#845 V1) via checked-in fixtures and the
  `memory-projection-eval.v1` report builder in `projection.py` (schema, scope
  annotations, nullable #495/#498 fields, cost metrics, diffable failure artifacts).
  Runtime harness attachment lands in V2 after #843/#844.
- Measure read-only stale-recall, projection-drift, and scope-leakage rates over
  `evals/memory-retrieval/quality/` cards (canonical `card-<uuid>` ids with
  alias dual-read) nested under `projection.quality` on the #722 envelope.

**Cannot**

- Stand in for production corpora. At ~40 cards, strong systems and grep often tie on easy queries; the query set is intentionally paraphrase-heavy for that reason.
- Measure production MiseLedger or Brigade facade projection fidelity (V2, after #843/#844).
- Enforce closed repository/task/operator/branch/worktree scope on lexical adapters;
  unavailable scope dimensions are report-level failures until a production filter ships.
- Provide per-item selection explanations (#495) or origin-scoped redaction (#498);
  those fields are explicitly unavailable/not_applicable in the projection report.
- Replace the live label-free repeat-search recall signal (#723) that
  `memory care status` surfaces from `.brigade/memory/search-log.jsonl`; that
  is a usage metric, not this offline fixture. High follow-up rates here are
  the demand signal to mine real failed queries into this corpus.

## Gate

Semantic retrieval ships only if it beats **grep** by a margin that survives the
`paraphrase` and `cross_tag` breakdowns. An aggregate-only win that collapses
once exact matches are removed is not enough. If it ties, keep the keyword
scorer and skip the dependency.

## Hub adapters and S17 runner

`hub-lexical` and `hub-hybrid` reuse this harness and its public fixture.
They start a temporary hub with an isolated child home, load the fixture with
`put`, and stop the process group on exit. The fixture uses file stems as hub
ids, matching the existing S13b R0 drive. Results map back to canonical gold ids.
No ambient fleet credentials or hub settings are read.

```sh
python -m brigade.memory_retrieval_eval --gate R0 --hub-binary /path/to/brigade-hub --json
python -m brigade.memory_retrieval_eval --adapters bm25,hub-lexical --hub-binary /path/to/brigade-hub --json
```

R0 requests `ranking: raw` and compares all returned candidate ids, up to the
hub's 20-result limit. Normal search defaults to lineage collapse and source
demotion. MRR uses reciprocal first-gold rank, counting misses as zero.
Comparisons including a hub adapter use the same depth of 20 for every adapter.

For a later candidate run, configure the disposable hub with an explicit
`[memory]` TOML file. This is an opt-in embedding call and may send the selected
fixture to the configured endpoint. It is separate from S17's offline checks.

```sh
python -m brigade.memory_retrieval_eval --gate R1 --hub-binary /path/to/brigade-hub \
  --hub-memory-config /path/to/candidate.toml --candidate-embedder '<full-embedder-id>' --json
```

`R1-public` checks the public half only. Private prompt judges, synthetic
keyword queries and the model default decision remain separate operations.
Neither a passing public half nor a skipped adapter qualifies a model for R1.
A gate exits nonzero when a required adapter is skipped. Missing binaries or
embedders are reported as unavailable, and fallback or stale search responses
cannot pass. A disposable candidate hub gets 30 seconds for initial coverage.
An operator can instead prepare an isolated fixture hub before running:

```sh
python -m brigade.memory_retrieval_eval --gate R1 --hub-url https://hub.example.test \
  --hub-token-file /path/to/private-token --hub-scope collection:eval \
  --candidate-embedder '<full-embedder-id>' --fixture-root /path/to/fixture --aggregate-only --json
```

External-hub mode reads only. It verifies fixture content hashes and never
imports cards, reconfigures retrieval or calls an embedder directly. Use a
scope containing exactly the selected fixture. Search metadata binds
`indexed_seq`, `index_format`, `scorer_version`, mode, ranking policy, RRF
weights, embedder identity and the current policy revision and digest. Any change during the run fails the evaluation.
The report also carries a fixture digest and a SHA-256 digest over its canonical
JSON without the `digest` field. `--aggregate-only` removes per-query data before
that report digest is calculated. It changes no S19e evidence record format.

RRF weights are positive finite numbers up to 100, under `[memory.rrf]` in the
hub's TOML file. `lexical = 1.0` and `dense = 1.0` remain the defaults, with
rank constant 60. A restart applies tuning. Lineage links use scoped
`superseded_by`, `supersedes`, `merged_from` and merge `aliases` metadata.
Each collapsed hit includes readable chain members with revisions for `card`
lookup. Flagged cards and unreviewed automation cannot assert links that
absorb interactive results. Reviewed proposal relations remain effective.
Terminal heads win, with newest revision breaking branches and cycles.
The registered principal credential class is capped at automation, regardless
of an `interactive` hint. Node and admin writes use `scheduled` and `worker`
hints. Only `agent-unreviewed` automation is demoted, and
`sources: ["automation"]` disables that demotion without filtering results.

The bounded before/after script is `engines/hub/bench/retrieval.py`. Run it
inside a Brigade verification script with `--before`, `--after` and `--output`.
It runs twice per binary over the public fixture, then twice over the same
fixture with deterministic predecessor and scheduled-source copies. It records
hit@5, MRR at depth 20, elapsed query time and load averages. It accepts no
external hub or embedder settings.
