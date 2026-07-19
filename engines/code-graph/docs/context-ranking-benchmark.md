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
