# Case 6: code-reference contract ownership

## Decision question

Should code-reference evidence use a Brigade-owned versioned schema or keep
the separate GraphTrail #40 and MiseLedger #42 contracts?

## Evidence context

- [Issue #352](https://github.com/escoffier-labs/brigade/issues/352) makes
  Brigade the compositor between code intelligence and evidence memory.
- [Issue #361](https://github.com/escoffier-labs/brigade/issues/361) moves
  shared receipt, graph-reference, and evidence contracts under Brigade while
  retaining engine process boundaries.
- The prior design allowed both a direct engine adapter and Brigade
  composition, leaving two owners for one lifecycle.
- Exact code-reference lookup must precede an explicit lexical fallback.

## Known-good answer

Use the Brigade-owned `brigade.code-reference.v1` schema. It supersedes the
cross-repository contracts.
