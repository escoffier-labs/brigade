# Case 3: dead grader-result field

## Decision question

Before the stable schema freeze, should `brigade.grader_result.v1` keep its
`exit_code` field?

## Evidence context

- [Issue #435](https://github.com/escoffier-labs/brigade/issues/435) records
  that the field is hardcoded to `0` for scored results and `null` otherwise.
- No shipped grader supplies an independent process exit through the field.
- Consumers can infer meaning from a named field even when it carries no
  signal.
- Removal after the stable freeze would be a breaking schema change.

## Known-good answer

Drop `exit_code` before the freeze.
