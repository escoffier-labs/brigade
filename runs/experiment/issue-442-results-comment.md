Experiment complete on `main` commit
`0941c3e66274b76c36a8277c106cf85ffd8a90c9`.

Recommendation: **kill the opt-in flag under the predeclared gate**. Route C
did not beat route B on known-good matches plus planted-contradiction
detection at the matched 4,000-output-token target.

| Route | Known-good matches | Contradictions caught | Primary score |
| --- | ---: | ---: | ---: |
| A: normal run | 8/8 | 2/2 | 10/10 |
| B: self-consistency, N=3 | 5/8 | 1/2 | 6/10 |
| C: grounded deliberation | 0/8 | 0/2 | 0/10 |

Per case, the score cell is `known-good + contradiction` points earned over
the points available. Tokens are provider output tokens, including reasoning
output. Latency is route wall time in seconds.

| Case | A score | A tokens / s | B score | B tokens / s | C score | C tokens / s |
| ---: | :---: | ---: | :---: | ---: | :---: | ---: |
| 1 | 1/1 | 1,214 / 36.912 | 1/1 | 1,391 / 40.585 | 0/1 | 0 / 0.291 |
| 2 | 1/1 | 1,128 / 40.299 | 0/1 | 738 / 26.131 | 0/1 | 0 / 0.302 |
| 3 | 1/1 | 827 / 28.716 | 1/1 | 1,184 / 33.738 | 0/1 | 0 / 0.293 |
| 4 | 1/1 | 837 / 26.436 | 1/1 | 1,021 / 33.874 | 0/1 | 0 / 0.322 |
| 5 | 1/1 | 1,185 / 42.538 | 1/1 | 1,116 / 41.344 | 0/1 | 0 / 0.297 |
| 6 | 1/1 | 1,552 / 47.201 | 0/1 | 618 / 21.045 | 0/1 | 0 / 0.315 |
| 7 | 2/2 | 956 / 30.212 | 0/2 | 820 / 31.670 | 0/2 | 0 / 0.286 |
| 8 | 2/2 | 1,061 / 35.381 | 2/2 | 1,374 / 42.021 | 0/2 | 0 / 0.290 |

Route C failed during planning in every case before a model call:

> deliberation could not assemble enough grounded GraphTrail evidence scopes.
> need distinct dependency traces from .graphtrail/graphtrail.db

Diagnostics through `brigade code` found that the planner gets one context
scope, then appends `--markdown` to the caller, callee, and impact probes. The
installed graph CLI accepts that option for `context`, but not for those three
commands. The planner therefore receives only one scope and rejects the plan.
The experiment did not patch the mode.

Route B had no majority in cases 2, 6, and 7. In those cases, most samples
answered the attached code context instead of returning an allowed decision
label. The deterministic majority rule treats those responses as invalid
ballots.

Evidence:

- [Plan](https://github.com/escoffier-labs/brigade/blob/codex/issue-442-experiment/docs/deliberation-experiment.md)
- [Scored comparison, minority findings, and method](https://github.com/escoffier-labs/brigade/blob/codex/issue-442-experiment/runs/experiment/scored-comparison.md)
- [Case files with held-out answers](https://github.com/escoffier-labs/brigade/tree/codex/issue-442-experiment/runs/experiment/cases)
- [Raw route outputs and command logs](https://github.com/escoffier-labs/brigade/tree/codex/issue-442-experiment/runs/experiment)

This is a scored recommendation, not the final decision. The operator owns the
keep or kill call. I did not remove the flag or close this issue.
