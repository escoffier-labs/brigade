# Grounded deliberation experiment

## Goal

Benchmark the opt-in grounded deliberation mode from issue #442 against a
normal Brigade run and single-agent self-consistency. The experiment measures
decision quality per output token on prior Brigade decisions with known
outcomes. It does not change the deliberation implementation, remove the flag,
close issue #442, or perform a release.

The operator makes the final keep or kill decision.

## Base revision

The experiment runs from fetched `origin/main` at
`0941c3e66274b76c36a8277c106cf85ffd8a90c9`.

## Case set

| ID | Decision | Known-good result |
| --- | --- | --- |
| 1 | Should Agent Pantry and agent-notify be inside the first consolidation? | No. Keep source and release separate. |
| 2 | Should agent-notify live at `stations/notify/` or under `engines/`? | `stations/notify/`. `engines/` conflicts with the `registry.py` station contract and gives no release or CI simplification. |
| 3 | Should the dead `grader_result.v1` `exit_code` field remain before the stable freeze? | Drop it. The field carries no signal, removal after stable is breaking, and removal before stable is free. |
| 4 | If a battery has a rejection and a measurement failure, which controls process exit? | Measurement failure controls process exit: exit 3 takes precedence over exit 1. |
| 5 | Should the from-scratch roster implementation in #462 or the incremental extension in #463 land? | Land the incremental implementation and close the from-scratch conflict. |
| 6 | Should Brigade define its own versioned code-reference schema or retain the cross-repository GraphTrail and MiseLedger contracts? | Define `brigade.code-reference.v1` under Brigade. It supersedes the cross-repository contracts. |
| 7 | Planted contradiction: "The release manifest and installer key on source directory, so `engines/` is required for release membership." | Catch the premise as false. Membership keys on component IDs and asset names. `stations/notify/` still works. |
| 8 | Planted contradiction: "Agent Pantry shares a release train with agent-notify, so they must consolidate together." | Catch the premise as false. They are source- and release-separate. |

Each case artifact contains the decision question, curated evidence read with
`gh issue view`, provenance links, the planted premise where applicable, and
the held-out known-good result. Route prompts contain the question and evidence
but omit the known-good result. The known-good sections are added only after
all route outputs are complete.

## Routes

All routes use `gpt-5.6-terra` with low reasoning and read-only execution.

### Route A: normal Brigade run

Run normal `brigade run` with one planner, one analyst assignment, and final
synthesis. The planner is instructed to emit exactly one assignment so this is
the ordinary single-plan route, not a hidden fan-out.

### Route B: single-agent self-consistency

Run three independent samples of one model with identical prompts and no shared
history. Select the route verdict by deterministic majority over the samples'
machine-readable answer labels. A 3-way split is recorded as no majority and
does not match known-good. Minority content is retained in the raw samples.

### Route C: grounded deliberation

Run `brigade run --deliberate` with two independently grounded perspectives,
one later challenger, and final synthesis. The treatment is the opt-in mode
merged in #452, including its `brigade.deliberation.v1` artifact.

## Matched-budget rule

Each case and route receives the same 4,000 output-token ceiling. The ceiling
includes every model call belonging to the route:

- Route A: planner, worker, and synthesis.
- Route B: all three independent samples. Majority selection is deterministic
  and consumes no model tokens.
- Route C: both perspectives, challenger, and synthesis.

Every prompt also requires a concise response of at most 180 words per call.
Provider-reported output tokens from the matching Codex session receipts are
summed for the route. If any route exceeds 4,000 output tokens, that case-route
cell is invalid and must be rerun under the same ceiling before scoring.

The token ceiling is matched. Actual token use is reported separately and
longer or repeated answers receive no quality credit.

## Metrics

For every case and route, record:

- whether the final route answer matches the held-out known-good answer.
- whether the planted contradiction was caught, for cases 7 and 8.
- whether a useful minority finding was surfaced, plus the concrete finding.
- provider-reported output-token count across all route calls.
- wall-clock latency in seconds.
- run IDs and raw artifact paths.

Verdict scoring is a factual comparison against the answer in the case
artifact. No LLM judges match quality or contradiction detection. Minority
findings are reported separately and do not change the kill-gate score.

## Kill gate

For each route:

`primary score = known-good matches across 8 cases + contradictions caught across 2 planted cases`

The maximum primary score is 10. Grounded deliberation beats
self-consistency only if Route C's primary score is strictly greater than Route
B's score at the matched 4,000-token ceiling.

- If C strictly beats B, recommend keeping the flag for further operator
  evaluation.
- If C ties or trails B, recommend killing the flag under issue #442's stated
  criterion.

The recommendation is evidence for the operator. The operator owns the final
keep or kill decision.
