# Case 4: mixed-battery process exit

## Decision question

A model-trial battery contains both a legitimate rejection and a measurement
failure. Which condition should control the process exit?

## Evidence context

- [Issue #435](https://github.com/escoffier-labs/brigade/issues/435) separates
  a valid rejection from failures in the adapter, grader, transport, or
  provider.
- Exit `1` means a legitimate rejection. Exit `3` means the apparatus did not
  produce a trustworthy result.
- A mixed battery has one process exit, so the exit must preserve the more
  severe loss of measurement integrity.

## Known-good answer

The measurement failure dominates. Return exit `3`, not exit `1`.
