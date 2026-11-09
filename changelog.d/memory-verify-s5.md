- `brigade memory verify status` and `brigade memory verify run --dry-run --no-model` score and
  select hub cards for evidence-checked verification and print only card ids, scores, lanes
  and skip counts. A quarter of each run's slots, rounded up, goes to the cards with the
  oldest verification. A never-read count stays null until read counting covers its window.
  `[verify]` in `.brigade/memory-care.toml` binds each scope to one workspace root and an
  explicit archive project list. Roots must exist and are compared by directory identity, so
  roots bound to two scopes and nested roots are refused even through case-only spellings or
  symlinks, and so are hub URL or credential keys. After a 429 from `read_stats`, a scope is
  scored from cached counts only while they are at most `cached_counts_max_age_hours` old, and
  its never-read count stays null. The verify state file has an 8 MiB limit and fails closed with a
  fixed reason. The reflect lanes and verification share one fairness order helper.
