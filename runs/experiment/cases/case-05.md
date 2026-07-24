# Case 5: competing roster changes

## Decision question

Which roster change should land, the from-scratch work in #462 or the
incremental work in #463?

## Evidence context

- [Issue #444](https://github.com/escoffier-labs/brigade/issues/444) defined
  the roster work.
- [PR #454](https://github.com/escoffier-labs/brigade/pull/454) had already
  merged packaged presets and fallback resolution in `roster.py`.
- [PR #462](https://github.com/escoffier-labs/brigade/pull/462) started from an
  older base and recreated the resolver in `roster_resolution.py`, including a
  second `minimal.toml`.
- [PR #463](https://github.com/escoffier-labs/brigade/pull/463) extends the
  merged resolver with suggestions, receipt-backed stats, and doctor warnings.

## Known-good answer

Land the incremental #463 approach and close the from-scratch conflict in
#462.
