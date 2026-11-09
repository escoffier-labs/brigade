- `brigade memory verify status` and `brigade memory verify run --dry-run --no-model` score and
  select hub cards for evidence-checked verification and print only card ids, scores, lanes
  and skip counts. A quarter of each run's slots, rounded up, goes to the cards with the
  oldest verification. A never-read count stays null until read counting covers its window.
  `[verify]` in `.brigade/memory-care.toml` binds each scope to one workspace root and an
  explicit archive project list, and refuses roots bound to two scopes, nested roots and hub
  URL or credential keys. The verify state file has an 8 MiB limit and fails closed with a
  fixed reason. The reflect lanes and verification share one fairness order helper.
