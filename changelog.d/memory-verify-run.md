- `brigade memory verify run` now runs the verification pass: it checks the
  hub's verifier floor and principal flags before any model call, calls both
  seats, attaches a manifest and opens one pinned proposal per corrected card,
  and keeps an owner-only review packet for each proposal for 14 days after it
  settles. Commit-id corrections stay off until the git resolver lands.
- `brigade memory care install --entry memory-verify` schedules the pass at
  11:00 and refuses the reflect job's folder and fleet credentials.
- A rate-limited verify scope is now scored from the last complete read count
  snapshot when it is at most two days old.
- Verify submissions are staged on disk before they are sent. A crash after a
  proposal opens no longer loses its review packet, and a submission that met
  a transient error is replayed with the same request id by the next run
  instead of waiting out the card's cooldown. A run that only skipped no
  longer clears the pause counter.
- Verify stages expire 7 days after creation and are never replayed when their
  ops no longer match the ledger or their scope is no longer bound. A
  transient attach error is retried instead of treated as final, and a
  duplicate pending reply publishes a packet only when the hub lists its
  manifest as bound to that proposal.
- An expired verify stage keeps its request id as a tombstone for 90 days, at
  most 512 at a time, so identical ops are resent under the same id instead of
  a new one.
- `[verify] enabled = false` now stops every verify run that would call a seat,
  scheduled runs included, with `verify_disabled` before any hub request.
