# Personalized Context Ranking Benchmark

The checked-in corpus at `benchmarks/context-ranking/corpus.json` measures the
opt-in personalized context ranker against GraphTrail's deterministic baseline
ordering. All paths, symbols, tasks, and graph edges are synthetic. The runner
does not read a local repository, user history, environment variables, or the
network.

Run it with:

```bash
cargo test --test context_ranking_benchmark -- --nocapture
```

The runner reports mean reciprocal rank (MRR) for one labeled relevant file per
case. A missing relevant file scores zero. It also reports p95 wall-clock time
for baseline context construction and context construction plus personalized
ranking after ten warmup iterations. Latency measurements are process-local and
are intended as a regression alarm, not a machine-independent performance SLO.

The corpus owns its thresholds so proposed corpus and policy changes are
reviewed together:

- At least 3 cases.
- Personalized MRR of at least 0.80.
- MRR gain over baseline of at least 0.65.
- Personalized p95 no greater than 10,000 microseconds.
- p95 overhead no greater than 10,000 microseconds.
- 100 measured iterations per case after 10 warmups.

These cases cover three ranking contracts: an explicitly named path beats an
unrelated high-degree hub, a graph-connected caller beats alphabetical order,
and an explicitly mentioned disconnected file remains in the pack.

Passing this corpus does not justify enabling personalization by default. It is
a small synthetic contract suite derived from known ranking behaviors. A
default change needs a larger labeled corpus sampled from real tasks across the
four supported languages, measured top-k usefulness, and repeated latency runs
on the supported CI platforms. Keep `--personalized` and the MCP `personalized`
argument opt-in until that evidence exists.

## Relevance floor

`benchmarks/context-ranking/floor-corpus.json` holds labeled search tasks for
the relevance floor that `context` applies to keyword hits before they become
entry points (brigade#1648). Each case lists synthetic symbols and the entry
points a reviewer would call relevant. An empty list means the task needs no
code context, and the pack must come back with `confident: false`. The docs-only
and named-symbol cases mirror the briefs reported in the issue.

The same test file runs three floor checks:

- `relevance_floor_raises_precision_without_losing_recall` compares the old
  top-`limit` search against the floored pack. It requires equal or better
  recall, strictly better precision, an empty pack for every no-context case,
  and the labeled `first` symbol as entry point 1.
- `name_coverage_floor_matches_its_calibration_sweep` sweeps the minimum name
  coverage from 0.00 to 1.00 in steps of 0.05. It keeps the values that reach
  the best recall and then the best F0.5, and it fails unless
  `NAME_COVERAGE_FLOOR` equals the lowest of them. To move the floor, change the
  labeled corpus, not the constant.
- `ranking_corpus_relevant_files_survive_the_floor` runs the personalized
  ranking cases above through real search and checks that each relevant file
  still reaches the pack.

The floor keeps only the best tier that has any hit. Identified hits are named
by file path or code identifier. Described hits have most of their name spelled
out by distinctive task words. Located hits sit in a directory the task names,
or in a file whose stem the task names when a second task word also appears in
the hit's name. Generic task words such as "fix", "update", "add", and
"section" never count as evidence on their own.
