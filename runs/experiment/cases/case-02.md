# Case 2: agent-notify source placement

## Decision question

Should agent-notify live at `stations/notify/` or under `engines/`?

## Evidence context

- [Issue #431](https://github.com/escoffier-labs/brigade/issues/431) records
  that the component manifest, provenance checks, installer, and resolver key
  on component IDs, asset names, and exact asset counts, not source
  directories.
- Existing `engines/` paths belong to engine-specific CI and build jobs. A
  station can receive its own path filter and working directory.
- Putting a registered station under `engines/` conflicts with the
  `registry.py` station contract and removes no release work.

## Known-good answer

Use `stations/notify/`.
