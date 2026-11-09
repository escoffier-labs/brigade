- Dot and Duet `inbox_list` and `fleet_digest` push memory hits from hub memory when the
  workspace's `[hub]` mode is `read` or `sync`. The connector asks the hub as its own message
  principal, so only that principal's grants apply, and the runbook grants Dot and Duet read on
  `fleet`. Hits keep the one `memory_hits` object and add `scope`, `card_id`, a short `hash`,
  `trust`, `score` and `source: "hub"`, with the title, summary and tags wrapped as untrusted
  text. A local card is never pushed in these modes, and an unavailable or refusing hub leaves
  the field out.
- `fleet_digest` carries up to 5 hits and `inbox_list` up to 3, the wrapped card text stays
  within 300 characters, and the whole `memory_hits` object stays within 4 KiB, dropping
  trailing hits to fit.
- Hub memory `search` accepts `query_source: "push"`. It answers only reviewed,
  policy-promoted and imported cards, plus unreviewed cards to the credential that wrote them,
  after re-reading each head and its current clean scan receipt, so a retired, lowered or
  superseded card in a stale index snapshot is dropped. Private-class and flagged text never
  leaves on this path. `eval` and `ranking: "raw"` are refused with it.
