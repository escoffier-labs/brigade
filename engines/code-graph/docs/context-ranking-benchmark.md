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

`context` filters its keyword hits through a relevance floor before they become
entry points (brigade#1648). The rule, `task-coverage-v2`, keeps:

- identified hits: the task names the hit's file, or spells its name as a code
  identifier or a compound name in any case (`codegraphbrief`). A one-word name
  counts only when no other symbol shares it.
- described hits: the hit's name and path explain at least `MIN_TASK_COVERAGE`
  (0.30) of the task's distinctive words, at least one of them in the name, and
  the task explains at least `MIN_NAME_COVERAGE` (0.20) of the hit's name.
- located hits, only when nothing above matched or only tests did: the task
  names a directory the hit lives in, or its file stem plus a second word of its
  name.

Documentation tasks (README, CHANGELOG, a `docs:` change, a Markdown path) keep
identified hits only. Nested helpers inside test functions and vendored or
minified files never count as described or located. Generic task words such as
"fix", "update", "add", and "section" never count as evidence.

Three labeled sets back the rule:

- `benchmarks/context-ranking/floor-corpus.json` is synthetic. Its cases mirror
  the briefs reported in the issue and pin contracts: no-context tasks must come
  back with `confident: false` and no entry points, and a labeled `first`
  symbol must be entry point 1. `relevance_floor_raises_precision_without_losing_recall`
  checks those and compares precision and recall with the old top-`limit`
  search.
- `benchmarks/context-ranking/floor-calibration-real.json` freezes the keyword
  top 8 for 80 hand-labeled Brigade issue titles.
  `floor_thresholds_match_their_real_calibration_sweep` sweeps the task coverage
  from 0.10 to 0.40 and the name coverage from 0.0 to 0.3, picks the best F0.5
  (ties go to the most permissive pair), and fails unless the constants match.
  To move the floor, change the labels, not the constants.
- `benchmarks/context-floor-heldout/` holds 80 newer labeled titles that are
  never used for tuning. `run.py` scores an engine binary against a synced
  index. See its README for how the labels were made.

On the real sets the floor trades recall for precision, by design of F0.5:

| Set | Engine | Precision | Recall | F0.5 |
|---|---|---|---|---|
| Calibration (fixture) | keyword top 8 | 0.342 | 0.842 | 0.388 |
| Calibration (fixture) | task-coverage-v2 | 0.656 | 0.608 | 0.645 |
| Held-out (live index) | keyword top 8 | 0.216 | 0.831 | 0.253 |
| Held-out (live index) | task-coverage-v2 | 0.488 | 0.627 | 0.511 |

`ranking_corpus_relevant_files_survive_the_floor` also runs the personalized
ranking cases above through real search and checks that each relevant file
still reaches the pack.
