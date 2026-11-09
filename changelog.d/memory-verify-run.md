- `brigade memory verify run` now runs the verification pass: it checks the
  hub's verifier floor and principal flags before any model call, calls both
  seats, attaches a manifest and opens one pinned proposal per corrected card,
  and keeps an owner-only review packet for each proposal for 14 days after it
  settles. Commit-id corrections stay off until the git resolver lands.
- `brigade memory care install --entry memory-verify` schedules the pass at
  11:00 and refuses the reflect job's folder and fleet credentials.
- A rate-limited verify scope is now scored from the last complete read count
  snapshot when it is at most two days old.
