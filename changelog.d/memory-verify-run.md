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
- An expired verify stage keeps its request id as a tombstone for 90 days, so
  identical ops are resent under the same id instead of a new one. With 512
  live tombstones the pass opens no new proposal until one ages out.
- `[verify] enabled = false` now stops every verify run that would call a seat,
  scheduled runs included, with `verify_disabled` before any hub request.
- A verify card whose earlier request may have opened an unnamed proposal is
  held until that request's tombstone ages out, so it never gets a second
  proposal. Corrections deferred by the per-run cap, the pending ceiling or a
  quota stop are checked again next run instead of waiting out a cooldown.
  `memory verify doctor` probes no seat while `[verify] enabled = false`.
- A verify replay whose manifest is 23 hours old or more, or that the hub
  refuses as unbindable, is checked afresh with a new manifest instead of being
  cached as a final refusal. A full packet directory defers the card without
  a cooldown.
