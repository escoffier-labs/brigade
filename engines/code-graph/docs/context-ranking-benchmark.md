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

## Relevance floor (opt-in)

`context --relevance-floor` filters keyword hits through a relevance floor before
they become entry points (brigade#1648). Without the flag, `context` returns the
top `limit` keyword hits exactly as before and reports `confident: true`. The
floor is opt-in because it trades recall for precision and answers some tasks
with no context: on 30 titles labeled independently before either engine was
run, it raised precision from 0.285 to 0.483 and F0.5 from 0.299 to 0.420, but
recall fell from 0.371 to 0.276 and 4 of 30 titles got a false empty answer.

The rule, `task-coverage-v2`, reads the top `CANDIDATE_POOL` (50) keyword rows,
keeps these, and truncates to `limit`:

- identified hits: the task names the hit's file, or spells its name as a code
  identifier or a compound name in any case (`codegraphbrief`). The name must be
  unique in the index, ignoring case and separators. A shared name such as
  `run_dir` (next to several `_run_dir` helpers) only counts as ordinary
  evidence below, where spelling the whole name still covers it.
- described hits: the hit's name and path explain at least `MIN_TASK_COVERAGE`
  (0.30) of the task's distinctive words, at least one of them in the name, and
  the task explains at least `MIN_NAME_COVERAGE` (0.10) of the hit's name.
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
  top 50 for 80 Brigade issue titles labeled by the implementer.
  `floor_thresholds_match_their_real_calibration_sweep` sweeps the task coverage
  from 0.10 to 0.40 and the name coverage from 0.0 to 0.3, picks the best F0.5
  (ties go to the most permissive pair), and fails unless the constants match.
  To move the floor, change the labels, not the constants.
- `benchmarks/context-floor-heldout/` holds 80 newer titles, also labeled by
  the implementer. Its results were seen while the rule was being revised, so
  it is leaked and cannot show readiness. `run.py` scores an engine binary
  against a synced index.

Numbers from the implementer's own labels. They are in-sample or leaked and are
not evidence that the floor is ready to be the default:

| Set | Engine | Precision | Recall | F0.5 |
|---|---|---|---|---|
| Calibration fixture (in-sample) | keyword top 8 | 0.342 | 0.654 | 0.378 |
| Calibration fixture (in-sample) | `--relevance-floor` | 0.504 | 0.594 | 0.520 |
| Held-out (leaked) | keyword top 8 | 0.216 | 0.831 | 0.253 |
| Held-out (leaked) | `--relevance-floor` | 0.308 or more | 0.675 | 0.345 |

The held-out precision is a lower bound: 150 of the 364 entry points it returned
were never labeled and count as not relevant.

`ranking_corpus_relevant_files_survive_the_floor` also runs the personalized
ranking cases above through the floored pack and checks that each relevant file
still reaches it.
