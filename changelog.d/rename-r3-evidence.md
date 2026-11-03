- The evidence engine now takes its identity from the name it was invoked as.
  `brigade-evidence` prints `brigade-evidence 0.6.0`, names itself in usage text
  and reports `brigade-evidence` as its MCP `serverInfo.name`. The legacy entry
  point keeps its old banner and server name, and prints one stderr line saying
  that name is deprecated and will be removed in v0.33.0. `doctor --mcp`
  accepts either server name.
- A fresh home now stores evidence in `<data>/brigade/evidence/evidence.db`,
  with config and cache under `brigade/evidence/` in their XDG roots. An
  existing archive in the legacy directory is still opened and written in
  place, together with its bundle MAC key, and no canonical directory is created
  beside it.
- `brigade evidence crawl memory` accepts either engine banner when it checks
  the engine version floor.
