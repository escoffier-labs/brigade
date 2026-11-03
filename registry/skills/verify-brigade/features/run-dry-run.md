---
status: paused
proof: drive
surfaces: run
reason: issue 2191 renames the chef role to orchestrator, and that rename changes the role names in the plan this command prints
unpause: #2191
---

# run --dry-run

`brigade run --dry-run "<task>"` asks the orchestrator for a plan and prints
it without dispatching any worker. It is the cheapest way to see what a
`brigade run` would do, and the first thing to check when a roster or routing
change lands.

## Sub-features

- `--dry-run` prints the plan and dispatches nothing.
- `--show-plan` and `--verbose` add the parsed assignments and status lines.
- `--roster` picks a roster file other than `.brigade/roster.toml`.
- `--worker`, `--read-only`, `--route-template`, and `--route-signal` change
  what the plan contains.

## How to get to it (user POV)

An operator about to start a run checks the plan first:

```bash
brigade run --dry-run "describe the change"
```

They read which seats the plan picked and which stages it holds before they
run it for real.

## Driving it with control-brigade

Not driven yet. There is no helper subcommand for it, and this map has no
recorded drive. Do not cite `tests/test_run_*.py` as proof that `--dry-run`
works: those are stand-in proofs (see [MAINTENANCE.md](MAINTENANCE.md)).

When #2191 lands, add a helper step that writes a throwaway roster into the
temp target, runs `brigade run --dry-run` against it, and checks that nothing
was dispatched. Then record the observed plan here and set the status to
`active`.

## Gotchas

- The plan output names roles. #2191 renames `chef` to `orchestrator`, so any
  observation recorded before that lands would go stale at once. That is why
  this feature is paused instead of driven now.
- Nothing on this page has been observed on a temp target yet. Treat the
  sub-feature list as the `--help` surface, not as driven behavior.
