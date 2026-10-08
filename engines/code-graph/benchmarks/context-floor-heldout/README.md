# Context relevance floor: real-title labels

Two hand-labeled sets of Brigade issue titles score the relevance floor that
`graphtrail context` applies to keyword hits (brigade#1648).

- `labels.json` is the held-out set: the 80 most recent issues at labeling time
  (#1464 to #1658). Never tune the floor on it.
- `calibration-labels.json` is the calibration set: the next 80 older issues
  (#1228 to #1463). `../context-ranking/floor-calibration-real.json` freezes its
  keyword top 8 per title, and `tests/context_ranking_benchmark.rs` sweeps the
  floor thresholds over that fixture.

## How the labels were made

The index was a `graphtrail sync` of the Brigade repository at the PR branch
(34,848 symbols). For each title, the candidate pool was the union of the top 8
entry points from the pre-floor engine and from the first floor revision. Every
pooled candidate was judged by reading its name and path against the title. Each
case lists `relevant` and `judged` (pooled but not relevant) symbols.

The labeling was conservative:

- A symbol is relevant only when the title names it, or it is clearly the code
  or the test for the specific behavior the title describes.
- Nested helpers inside test functions, bundled JavaScript, and symbols that
  share only one generic word with the title are not relevant.
- Titles about documentation, research, process, or another PR get no relevant
  symbols unless a code symbol is plainly named.
- Recall is relative to the pool: relevant code that neither engine returned is
  not counted.

`run.py` counts any returned symbol outside `relevant` and `judged` as not
relevant and reports how many there were (`returned_unjudged`), so a new engine
that surfaces unseen symbols gets a lower-bound precision.

## Running

```bash
graphtrail --db /tmp/brigade.db sync /path/to/brigade
python3 run.py --graphtrail target/release/graphtrail --db /tmp/brigade.db
python3 run.py --labels calibration-labels.json --graphtrail target/release/graphtrail --db /tmp/brigade.db
```

Scores depend on the indexed tree, so rerun against the commit you label.
