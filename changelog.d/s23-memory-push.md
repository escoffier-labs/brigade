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
- Dot and Duet `memory_card` read hub cards as the connector's own principal with the hub's new
  `card` `push: true` mode, never with the host node's credential or the replica. The hub serves
  only a live head with a current clean scan receipt and push trust, so a card id an agent is
  given or guesses opens no more than `memory_hits` could send.
- With hub memory on, Dot and Duet `memory_search` search the hub with `query_source: "connector"` as
  the connector principal, never with the host node credential, an admin token in its slot or the
  replica, so a search returns no card text `memory_hits` could not send. Local workspace cards
  come back apart as `local_matches`. The Grok Bot memory profile stays grant-scoped.
  `connector` applies the push filter and counts its matches as reads, as any explicit search
  does. The unsolicited `memory_hits` searches (`push`) are not counted.
- A push `card` read without a scope considers only push-eligible copies, so withheld copies
  never cause `memory_card_ambiguous` or appear in its scope list.
