- The connector `handoff_write` path now has an integration test over a temporary workspace: the
  real handoff lint refuses a bad handoff, a valid one lands in the profile's writer inbox and is
  never overwritten, and after `brigade ingest` the card is found by `brigade memory search`. The
  Dot and Duet connector docs describe the path.
