# Issue #442 grounded-deliberation experiment results

## Recommendation

Recommend killing the opt-in flag under the stated gate. Route C did not beat
route B on known-good matches plus contradiction detection at the matched
budget:

| Route | Known-good matches | Contradictions caught | Primary score |
| --- | ---: | ---: | ---: |
| A: normal run | 8/8 | 2/2 | 10/10 |
| B: self-consistency | 5/8 | 1/2 | 6/10 |
| C: grounded deliberation | 0/8 | 0/2 | 0/10 |

Route C failed during planning in all eight cases and made zero model calls.
It produced no verdicts to compare. A missing verdict does not match a
known-good answer and does not catch a contradiction.

This recommendation applies the experiment's predeclared kill gate. The final
keep or kill decision belongs to the operator. This experiment did not remove
the flag or close issue #442.

## Per-case evidence

`KG` is the factual known-good comparison. `PC` is planted-contradiction
detection and is `n/a` for the six cases without a planted premise. `MF` is
whether the route surfaced a useful minority finding. Tokens are provider
output tokens, including reasoning output tokens. Latency is wall-clock
seconds for the route.

| Case | Route | Status | KG | PC | MF | Tokens | Latency |
| ---: | :---: | --- | :---: | :---: | :---: | ---: | ---: |
| 1 | A | ok | yes | n/a | yes | 1,214 | 36.912 |
| 1 | B | ok | yes | n/a | yes | 1,391 | 40.585 |
| 1 | C | planning failure | no | n/a | no | 0 | 0.291 |
| 2 | A | ok | yes | n/a | yes | 1,128 | 40.299 |
| 2 | B | no majority | no | n/a | yes | 738 | 26.131 |
| 2 | C | planning failure | no | n/a | no | 0 | 0.302 |
| 3 | A | ok | yes | n/a | no | 827 | 28.716 |
| 3 | B | ok | yes | n/a | yes | 1,184 | 33.738 |
| 3 | C | planning failure | no | n/a | no | 0 | 0.293 |
| 4 | A | ok | yes | n/a | yes | 837 | 26.436 |
| 4 | B | ok | yes | n/a | yes | 1,021 | 33.874 |
| 4 | C | planning failure | no | n/a | no | 0 | 0.322 |
| 5 | A | ok | yes | n/a | yes | 1,185 | 42.538 |
| 5 | B | ok | yes | n/a | yes | 1,116 | 41.344 |
| 5 | C | planning failure | no | n/a | no | 0 | 0.297 |
| 6 | A | ok | yes | n/a | yes | 1,552 | 47.201 |
| 6 | B | no majority | no | n/a | no | 618 | 21.045 |
| 6 | C | planning failure | no | n/a | no | 0 | 0.315 |
| 7 | A | ok | yes | yes | yes | 956 | 30.212 |
| 7 | B | no majority | no | no | yes | 820 | 31.670 |
| 7 | C | planning failure | no | no | no | 0 | 0.286 |
| 8 | A | ok | yes | yes | yes | 1,061 | 35.381 |
| 8 | B | ok | yes | yes | yes | 1,374 | 42.021 |
| 8 | C | planning failure | no | no | no | 0 | 0.290 |

All successful A and B routes stayed below the 4,000-token target. Route C
also stayed below it because it failed before dispatch.

## Useful minority findings

| Case | A | B | C |
| ---: | --- | --- | --- |
| 1 | Optional alerts do not require shared source ownership. | agent-notify alone is low-risk, but the decision covers both components. | None. |
| 2 | The station needs scoped Go CI and a publish working-directory override. | The one valid ballot found the same operational work. | None. |
| 3 | None. | Keeping the field could reserve an extension point, but the dead field is misleading now. | None. |
| 4 | Exit 1 still applies to a rejection-only battery. | The rejection remains useful evidence even though exit 3 dominates. | None. |
| 5 | Parts of #462 could be ported only after removing duplicate resolver work. | Useful ideas from #462 could be ported selectively. | None. |
| 6 | Keep process boundaries and use compatibility tests for existing consumers. | None. | None. |
| 7 | Station-specific CI still needs its own path filter and working directory. | The one valid ballot found the same operational work. | None. |
| 8 | A coordinated rollout need not merge ownership. | A minimum-version constraint can provide compatibility without consolidation. | None. |

## Self-consistency majority details

Route B used three independent samples. A majority required at least two
samples to return the same allowed `answer_label`. A response without an
allowed label was an invalid ballot.

- Cases 1, 3, 4, 5, and 8 had unanimous correct labels.
- Case 2 had one `stations/notify` ballot and two invalid ballots.
- Case 6 had three invalid ballots.
- Case 7 had one `stations/notify` ballot that caught the premise and two
  invalid ballots. The route therefore had no majority and did not receive
  either primary point.

The invalid ballots answered attached code-context questions instead of the
decision question. The raw responses are retained with the run artifacts.

## Route C planning failure

Every route C invocation returned:

> deliberation could not assemble enough grounded GraphTrail evidence scopes.
> need distinct dependency traces from .graphtrail/graphtrail.db

The failure occurred in about 0.3 seconds per case before any model call.
Inspection through `brigade code` found that the planner obtained one context
scope, then failed to obtain the additional caller, callee, or impact scopes.
On the tested main commit, `_graphtrail_markdown` appends `--markdown` to every
graph command. The installed graph CLI accepts that option for `context`, but
not for `callers`, `callees`, or `impact`. The deliberation planner then
rejects the single-scope result because it requires at least two distinct
scopes.

The experiment did not patch this behavior because the task explicitly
excluded changes to the #452 mode.

## Reproduction and accounting

- Base: `0941c3e66274b76c36a8277c106cf85ffd8a90c9`, latest `main` when the
  experiment began.
- Model: `gpt-5.6-terra`, low reasoning effort, for every dispatched seat.
- Per-route output target: 4,000 provider output tokens per case.
- Route B: three sequential independent samples.
- Route C roster: two perspectives, one challenger, and synthesis.
- Token accounting: matched Codex session records to the exact Brigade command
  interval, then summed `output_tokens`, which includes reasoning output.
- Latency: monotonic wall time around each complete route command. Route B
  includes the failed lock-contention attempts and sequential retries in cases
  1 and 2.
- Verdict scoring: direct comparison with the held-out answer labels. No
  model judged the verdict.

The machine-readable scores are in `scored-results.json`. Token records are in
`token-accounting.json`. Each raw route directory includes its Brigade
`run.json`, plan, synthesis or worker outputs where produced, and final text.
