- Memory verification now has its prompt contract and reply checks: versioned
  instructions with per-call nonce markers, a strict `brigade.memory-verify.v1`
  reply validator, the deterministic evidence, novelty and supersede checks,
  and op-level agreement between the propose and confirm seats. Nothing calls
  it yet. The run command arrives in a later slice.
