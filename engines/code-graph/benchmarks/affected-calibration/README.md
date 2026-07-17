# Affected-test calibration bench

Measures how well `affected` attributes test files from changed source files, using
this repository's own history as ground truth, and compares against an off-the-shelf
graph-navigation baseline at the same depths.

Method: for each of 17 historical commits that modified both `src/` and existing
`tests/` files, check out the parent commit, index it fresh with both tools, ask each
for the tests affected by the commit's changed src files, and score file-level
precision/recall against the test files the commit itself modified.

## Results

| tool, depth  | precision | recall | mean predicted files |
|--------------|-----------|--------|----------------------|
| ours, d1     | 0.414     | 0.912  | 4.9                  |
| ours, d3     | 0.414     | 0.912  | 4.9                  |
| ours, d5     | 0.412     | 0.912  | 5.1                  |
| baseline, d1 | 0.440     | 0.912  | 4.5                  |
| baseline, d5 | 0.260     | 1.0    | 6.5                  |

Two conclusions:

1. **Depth default: null result.** Recall is identical at depth 1, 3, and 5 on this
   corpus (7 integration test files that exercise most modules, so caller-BFS
   saturates at one hop). No change to the depth default is justified by this data.
2. **Fixture misclassification: real defect.** `affected` returned
   `tests/fixtures/golden/mixed/src/bin/graphtrail-mcp.rs` — a fixture source, not a
   runnable test — because `is_test_file` counted any `tests/`-nested path. Fixed by
   excluding `fixtures/`, `golden/`, `snapshots/`, and `testdata/` segments unless
   the file name itself matches a test-name pattern.

Caveats recorded in `results.json`: small test corpus, and commit-touched tests
under-count genuinely affected tests the author did not edit.
