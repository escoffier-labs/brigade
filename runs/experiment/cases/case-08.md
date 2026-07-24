# Case 8: planted shared-release premise

## Decision question

Agent Pantry shares a release train with agent-notify, so they must consolidate
together. Should both move into Brigade together?

## Evidence context

- [Issues #352](https://github.com/escoffier-labs/brigade/issues/352) and
  [#366](https://github.com/escoffier-labs/brigade/issues/366) keep Agent
  Pantry independently built and released because its browser-session and
  secret-handling threat model has separate security ownership.
- agent-notify can join Brigade as an opt-in delivery adapter.
- Brigade can enforce an Agent Pantry minimum version without owning Pantry's
  source or release.
- A Pantry-to-notify expiry alert is an optional cross-repository integration.

## Known-good answer

Catch the false premise. They are source- and release-separate, so do not
consolidate them together.
