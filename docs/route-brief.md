# Route brief

`brigade run` plans with an LLM orchestrator. The route brief is the deterministic
layer under it: pure code that reads the task and names the stages the plan must
cover, so the same task always demands the same coverage regardless of which model
plans it.

## How it works

1. **Signals.** `route_catalog.derive_signals` maps the task text (plus an optional
   template hint and changed paths) to signals: the path (`code`, `docs`, `system`)
   plus surfaces like `auth-surface`, `ui-touched`, `perf-surface`, `migration`,
   `bug`, `needs-tests`, `destructive-op`, `ship-requested`. Keyword and path
   heuristics only, no model call.
2. **Route.** `brigade/router.py` composes the stages those signals pull in from
   `route_catalog.DEFAULT_CATALOG`. Stages declare what they subscribe to, the
   artifacts they need and produce, and optional while/until locks. The router
   drops stages whose required inputs nothing produces, holds locked stages, and
   topo-sorts the rest into parallel waves. The algorithm is adapted from
   [alp-river](https://github.com/alp82/alp-river) (MIT, Alper Ortac).
3. **Plan coverage.** The plan prompt carries the route and asks the orchestrator
   to tag each assignment with `covers: ["<stage>", ...]`. A parsed plan that
   misses required stages gets one corrective retry. Coverage gaps are recorded in
   `plan-attempts.json` (`coverage_missing`), never fatal: the constraint is
   advisory-but-checked, and a run the orchestrator can finish is not bricked by
   the checker.
4. **Telemetry.** The composed route (signals, stages, waves, holds, size) lands in
   `run.json` under `route`.

## Holds

Two stages carry locks that user approval releases:

- `ship` is held whenever the task requests a commit, push, PR, or release. The
  brief tells the orchestrator no worker may perform it. `--approve-ship` releases
  it.
- `system-execute` is held when the task smells destructive (`destructive-op`),
  pending `destructive-approved`.

A held stage is visible in the brief and in `brigade route` output, so "the run
did not push" is a designed hold, not a worker forgetting.

## Inspect a route

```bash
brigade route "add rate limiting to the login endpoint"
brigade route "implement the export module and open a PR" --json
brigade route "add pagination" --template vertical-slice
```

Sample output:

```
signals: code, auth-surface, needs-tests
size: M (6 stages)
  wave 1: test-author  (#needs-tests)
  wave 2: implement  (#code)
  wave 3: correctness-review  (#code)
  wave 3: security-review  (#auth-surface)
  wave 3: test-gap-review  (#needs-tests)
  wave 3: verify  (#code)
```

## Opting out

`brigade run --no-route` skips signal derivation, the prompt section, and the
coverage check entirely. Runs behave exactly as before the route brief existed.
