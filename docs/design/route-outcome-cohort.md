# Route as an outcome cohort axis

Status: Phase 1 implemented. Sibling to `context-blind-spot.md`: a third
cohort axis on the outcome ledger. Phase 2 and Phase 3 remain proposed.

## The problem

The deterministic route brief (brigade #210, #227, #232) composes the stages a
`brigade run` must cover, records the route in `run.json`, and ships in 0.22.0.
It is exercised in live runs. What it is not is measured. The outcome ledger
scores skills and cards by verified exit codes, but nothing in the ledger knows
whether a run followed a composed route, so `outcome rank` cannot answer the one
question the feature exists to earn: do routed runs verify greener than unrouted
ones.

Today the route and the outcome live in two files that never meet:

- the route is in `.brigade/runs/<id>/run.json` under `route`,
- the outcome is captured from a verify receipt in
  `.brigade/work/verify-runs/<id>/receipt.json`.

The receipt already carries a `run_id`, and `outcome_cmd._resolve_run_receipt`
already reads `run.json`. The linkage exists in the plumbing. It is just not
threaded onto the outcome record, so the ledger is route-blind the same way it
was context-blind before #228.

## The mechanism

Route is a third cohort axis alongside the two already designed. Content
fingerprint sees what an artifact is made of. Capability fingerprint sees the
harness it ran under. Route fingerprint sees the shape of the plan the run
followed. All three are separate axes, never folded into each other, each with
its own graceful-degrade cohort.

**What gets stamped.** A `route` manifest and a low-cardinality
`route_fingerprint` on each new `OutcomeRecord`, resolved at capture time from
the run the outcome belongs to:

- `path` (code / docs / system), `size` (XS..XXL), and the sorted `signals`
  list, hashed into `route_fingerprint`. Coarse on purpose, the same lesson
  context learned: the full ordered stage list is higher cardinality and
  fragments cohorts to n=1, so the fingerprint keys on path + size + sorted
  signals, not the stage sequence.
- `followed`: whether the run carried a route at all. A bare
  `brigade work verify run` with no owning `brigade run`, or a `--no-route` run,
  records `followed: false` and no fingerprint. This is the grandfathered
  cohort, exactly like a record with no capability manifest.
- `coverage`: whether the plan covered every required stage, read from the
  `plan-attempts.json` the route already writes (`coverage_missing`,
  `unknown_covers`). A run that shipped with an uncovered stage is a different
  cohort from one that covered cleanly.

**Where it comes from.** Capture already resolves the run. Extend that path:
verify receipt -> `run_id` -> `run.json` -> `route` payload -> manifest. Capture
what Brigade computes, never what the model asserts, the same integrity posture
as exit codes. No route in the run, no manifest, `followed: false`.

## Phased plan

### Phase 1: record and surface, no scoring change (DONE)

Stamp the `route` manifest and `route_fingerprint` onto new outcome records
(`route_manifest`/`route_fingerprint` in `outcome_cmd.py`). Surface a
routed-vs-unrouted breakdown in `outcome explain` (`_route_breakdown`). The default score,
rank order, and promotion ratchet are unchanged. This is the data foundation:
cohort-aware comparison means nothing until records carry the fingerprint, the
same sequencing content fingerprints (#218 record, #219 score) and context
fingerprints (#228 record, later score) followed. Records without a route
manifest are grandfathered.

### Phase 2: the comparison the feature exists for

`outcome rank --by-route` (and a line in `outcome explain`) split each artifact's
score by route cohort: routed vs unrouted, and by route shape where a cohort has
enough samples. Reuse `split_by_fingerprint` and the Wilson lower bound already
in `outcome.py`, so a thin route cohort shrinks toward the pooled rate and never
out-ranks a vetted one on a single lucky run. The output is the receipt this
feature is missing: a number for whether following the route helped.

### Phase 3 (deferred): route-arm paired attribution

The honest version of the question is counterfactual: would this run have
verified green without the route. Borrow the paired-attribution idea from
`context-blind-spot.md` Phase 3, on a deterministically-sampled fraction: run the
same task once routed and once with `--no-route`, record
`delta = +1 if the routed arm passes and the unrouted arm fails`. Two exit codes,
no judge. Expensive (doubles the run on sampled tasks, needs a disposable
workspace), gated to safe tasks, deferred until Phase 2 shows the correlation is
worth chasing.

## What this does not do

- **It does not change scoring in Phase 1.** A route cohort that looks worse
  must never demote a skill on the record-only milestone. Same rule as context.
- **It does not fingerprint the stage sequence.** Path + size + sorted signals
  only. The ordered stage list is a Phase 2 display detail, never a cohort key.
- **It does not credit the route for the skill's own work.** The route axis
  answers "did routed runs verify greener," not "did the route write the code."
  It stratifies attribution, it does not claim it.
- **It does not require every run to be routed.** Unrouted and `--no-route` runs
  are a valid cohort, not missing data.

## Why it is worth building

Brigade's whole pitch is that the receipt tells the truth about a run. The route
is currently the one shipped feature with no receipt of its own: it is used, it
is exercised, and its value is asserted rather than measured. This closes that.
Phase 1 is a small, precedented change (one manifest on a record, one line in
`explain`), it reuses the cohort machinery already built twice, and it turns the
next hundred `brigade run` invocations into the evidence for whether the router
earned its keep.

## Decision needed

Green-light Phase 1 as its own PR (record + surface, no scoring change),
matching the #218 shape. Phase 2 follows once records carry the fingerprint.
Phase 3 stays deferred behind Phase 2's result.
