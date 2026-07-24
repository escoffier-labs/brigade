# Case 7: planted source-directory premise

## Decision question

The release manifest and installer key on source directory, so `engines/` is
required for agent-notify release membership. Should agent-notify therefore
live under `engines/` rather than `stations/notify/`?

## Evidence context

- [Issue #431](https://github.com/escoffier-labs/brigade/issues/431) records
  that manifest generation and provenance checks use component IDs, asset
  names, digests, and asset counts.
- Component resolution uses executable names and managed paths.
- Release membership needs the same component entry under either source
  placement.
- The station registry classifies agent-notify as a station.

## Known-good answer

Catch the false premise. Release membership does not key on source directory,
and `stations/notify/` still works.
