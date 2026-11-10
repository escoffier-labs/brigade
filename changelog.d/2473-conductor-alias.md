- `python -m brigade.dot runtime-template --pack dot|duet` prints a placeholder runtime document whose
  `hub.recipients` lists a `conductor` alias, and the Dot and Duet guides name the alias, so a fresh
  install keeps a way to start a message to the conductor.
- `fleet_digest` carries an `unread` section: the connector's own pending inbox count, and its own sent
  messages that each recipient has not acknowledged in the last 7 days. Both come from the connector
  principal's own credential.
- `brigade fleet msg sent --undelivered-after MINUTES` marks each sent message `acknowledged`, `pending`
  or `undelivered`, and lists the ids that passed the deadline with no acknowledgement.
- `inbox_list` and `fleet_digest` add `sender_alias` to a message whose Hub-authenticated sender is a
  configured recipient. It authenticates provenance only.
- `inbox_list` and `fleet_digest` add a connector-computed `assignment` object to a plain, unthreaded,
  non-reply message from the configured `conductor` principal whose `ref` is exactly `owner/repo#N` with
  the repository listed in the new `assignments.repositories` runtime key (default empty). The Dot and
  Duet operating instructions let only that field authorize one branch, commits, a push and one pull
  request for that issue. Message text authorizes nothing.
