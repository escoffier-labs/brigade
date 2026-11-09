- `memory verify run` now expands abbreviated commit ids through the hardened git resolver instead of
  abstaining on every one. Each non-dry run opens one resolver run that all its cards share, so they share one
  fetch per repository, and closes it when the run ends, even after a failure or an early return.
- A dry run runs no git, because the resolver's fetch writes remote-tracking refs. Its commit expansions abstain
  with `verify_commit_unresolved`. On a platform where the resolver cannot run, a non-dry run reports
  `verify_resolver_unsupported_platform` once under `skipped`.
