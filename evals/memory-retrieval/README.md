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

1. **current** — existing keyword scorer in `brigade.memory_cmd` (title/tag/summary hits score 3, body hits score 1).
2. **grep** — naive tokenized substring match count. Deliberately dumb floor.
3. **semantic** (optional) — on-device embeddings + cosine via `sentence_transformers`, only when the package is installed **and** a local model loads with `local_files_only=True`. Set `BRIGADE_MEMORY_EVAL_EMBED_MODEL` to a local path or cached model id. Otherwise the adapter is reported as skipped; the harness still exits 0.

Adapters 1 and 2 never touch the network and never download a model.

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
- Exercise projection-state scenarios (#845 V1): identity drift, stale/partial scans,
  superseded leakage, duplicate live rows, scope annotations, provenance fields,
  instruction-like trusted-path checks, and index/query cost metrics via the
  versioned `projection` section in the JSON report.

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
