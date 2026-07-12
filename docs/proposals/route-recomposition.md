# Proposal: route recomposition after the implementer runs

Status: draft, awaiting green-light. Not implemented.

## The gap

The route brief composes once, at plan time, from the task text and whatever
paths are already dirty. That is the wrong moment for the richest signal. Ask
`brigade run` to "clean up the config loader," the loader turns out to parse
auth tokens, and no `security-review` ever joins the route: the word "auth" was
never in the task, and the tree was clean when the route was derived, so
`derive_signals` had nothing to catch. The plan covered every stage the route
named. The route just named the wrong stages.

alp-river does not have this gap. Its loop recomposes every turn: a stage emits
a signal, the router runs again, and the route grows. Brigade took the router
and the catalog but kept its own single-shot plan-then-dispatch shape, so the
route is frozen before any code exists.

## What recomposition would add

After the implementer runs and a real `diff` exists, re-derive signals from the
diff itself (changed paths, added imports, touched symbols from the GraphTrail
delta already in the receipt), union them with the plan-time signals, and
recompose. New surfaces pull their review lenses retroactively: a diff that
added a call to `verify_token` pulls `security-review` even though the task said
"clean up the loader."

The router already supports this. `compute_route` takes `already_run` and drops
stages that have run, so a recompose after implement naturally yields only the
newly-required stages (the reviews the fresh diff earned). The missing piece is
the orchestrator loop calling it a second time.

## Sketch

1. Plan-time route as today (task text + dirty paths).
2. Implementer runs, produces `diff` and the GraphTrail `code_graph_delta`.
3. Re-derive: `derive_signals(task, changed_paths=diff_paths)` unioned with the
   plan-time signals, plus a diff-content pass (imports/symbols the delta names
   that map to a surface, e.g. an added `auth`-segment path -> `auth-surface`).
4. `compute_route(catalog, live', available', already_run=ran)` -> the delta
   route: the review stages the real change earned but the task text missed.
5. Dispatch that delta as a second review wave. Record both routes in
   `run.json` (`route` and `route_recomposed`) so the receipt shows what the
   frozen route missed and the recompose caught.

## Cost and risks

- **Loop change, not a pure-function change.** Everything so far has been
  testable code under a frozen orchestrator loop. This touches the loop: a
  second derive+compose+dispatch after implement. That is why it is a proposal,
  not a PR.
- **Cost.** A second review wave on every run that surfaces new signals. Bound
  it: only recompose when the diff introduces a surface the plan-time route did
  not already cover, and cap the delta at the review stages (never re-plan, never
  re-implement).
- **The `already_run` contract must hold under recompose.** The property fuzzer
  covers `already_run`, but a live recompose needs its own e2e: implement a
  change that touches auth without saying so, confirm `security-review` joins the
  second wave and lands a receipt.

## Why it is worth it

The single-shot route is honest about what the task text asked for. It is blind
to what the change turned out to be. Brigade's whole pitch is that the receipt
tells the truth about a run. A route that cannot see the diff it produced is the
one place the receipt still trusts the plan over the evidence. Recomposition
closes that.

## Decision needed

Green-light to build, or park with the gap documented. If built, it is its own
arc: loop change, a live e2e, and a blog beat about teaching the router to read
its own diff.
