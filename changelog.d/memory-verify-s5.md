- `brigade memory verify status` and `brigade memory verify run --dry-run --no-model` score and
  select hub cards for evidence-checked verification and print only card ids, scores, lanes
  and skip counts. A quarter of each run's slots, rounded up, goes to the cards with the
  oldest verification. A never-read count stays null until read counting covers its window.
  `[verify]` in `.brigade/memory-care.toml` binds each scope to one workspace root and an
  explicit archive project list. Roots must exist and are compared by directory identity, so
  roots bound to two scopes and nested roots are refused even through case-only spellings or
  symlinks, and so are roots without a stable inode and hub URL or credential keys. A scope
  that `read_stats` rate limits is skipped for the run, and cached counts are never used in
  place of a reply. Card history is bound to the hub, principal and log epoch and ignored when
  any of them changes, and the per-scope card bound applies to bound scopes only. The verify state file has an 8 MiB limit and fails closed with a
  fixed reason. The reflect lanes and verification share one fairness order helper.
