# Fleet claim fencing

The existing `/claims` endpoint accepts an optional integer `generation` from
0 through 9223372036854775807. This is an additive legacy field. There is no new
endpoint or capability negotiation. The v2 protocol remains a separate proposal
for #1737.

## Holder retirement

Each independent legacy claim uses a fresh holder token. The client creates a
`uuid4` token for a new acquire and reuses it only for the current hold's retry,
renewal, and release. Fresh generation-less acquires and current live holder
operations retain their existing behavior.

Release, expiry retirement, and dead-lock supersede durably retire the old
`(target, node, holder)` in the same transaction that advances the target fence.
A holder-scoped release on a missing claim also records that holder as retired.
A delayed acquire from that holder therefore cannot recreate a claim after
cleanup within the retirement horizon, including after a restart.
Force release retires the actual deleted holder, regardless of the operator's
node identity. An in-flight renew or reacquire cannot resurrect that holder.

Generation-less operations using an in-horizon retired holder return HTTP 409
with `reason: "stale-holder"` and the action's success field set to false.
A holder release on a missing claim returns HTTP 200 with
`{"released": false, "reason": "stale-holder"}`, including repeated releases.
The CLI preserves its JSON payload, prints the refusal reason, and exits 1.
Refused requests do not renew, release, or reacquire a newer claim. Refusals never
disclose holder tokens.
The client preserves the reason instead of treating it as a missing claim and
retrying acquire. Claim admission refuses it, and a heartbeat treats it as
terminal ownership loss under the selected loss policy. Operator callers should
inspect ownership and use a fresh holder for a new independent hold.

## Optional generations and force release

Inspection and claim payloads expose the durable target generation. An unseen
target omits the inspection field and has generation 0. A new acquire compares
the supplied generation with the current fence, then advances it. A live retry
by the same holder may send its pre-acquire generation or its current tenure
generation. Renew and release compare the exact current generation. An operation
with an obsolete generation returns HTTP 409 with `reason: "stale-generation"`
without acting on a newer tenure.

A caller deliberately reusing a retired holder must supply the current
generation on acquire and every subsequent operation. The generation distinguishes
that new incarnation from delayed generation-less operations using
the same identities. Ordinary clients do not reuse retired holders.

The client force-release path first inspects and sends that generation. A row
replacement between inspection and release causes refusal. On an older hub,
inspection may return no generation. The client sends generation 0, which also
fences an unseen target on a new hub. Older hubs ignore the additive field, and
their original fencing limitations still apply.

For older clients, a generation-less node or force release is bound to the
server's ingress generation before waiting for its write transaction. A changed
generation at mutation time refuses the request. A generation-less force request
first arriving after a replacement addresses the then-current claim. Clients
that need to preserve an earlier inspection must send its generation. Node
release also retains its existing `acquired_at` check.

A token-less release on a free target cannot identify an unseen, generation-less
holder. An acquire carrying the generation from before that release is refused.
Holder-scoped cleanup names the abandoned holder and fences its late acquire.

## Retention and capacity

`RETIRED_HOLDER_HORIZON_SECONDS` is 604,800 seconds (7 days). This covers the
maximum claim TTL of 86,400 seconds plus a queueing margin of 518,400 seconds
(6 days). A retirement exactly H seconds old remains protected. Rows strictly
older than H may be evicted. Each write transaction opportunistically deletes
at most 256 rows per eviction batch through an age index. Capacity checks reclaim
another bounded batch before refusal. An acquire prunes at most 32 unrelated
expired claims plus the requested target, which is always checked regardless
of its position in the backlog. The per-target `claim_fences` are never evicted.

Retirement storage allows 1,000,000 rows globally and 100,000 in-horizon rows
per node for missing-row holder releases and every non-retry acquire admission,
including superseding replacements.
An index on `(owner_node, retired_at)` supports the node quota. The transaction
counts global rows once and caches each checked node's indexed count.
A node that repeatedly releases fresh holders on a free target reaches its own
quota before it can fill the global budget. Updates to an existing in-horizon
marker require no new row. Expiry, live-holder release, node release, force
release, and supersede retirements are exempt from the per-node quota so existing
holders can retire. Superseding admission still checks the quota before replacing
the live holder, which prevents repeated replacements from filling global history.
The global cap applies to every retirement. After a crash, same-node supersede
admission remains subject to the per-node quota. Waiting for the dead claim's
TTL to expire does not restore admission: the node's own in-horizon retirement
rows must age out before its quota allows another tenure.

At 1 retirement per second, the former lifetime cap of 100,000 lasted 27.8 hours.
With seven-day retention, that rate retains at most 604,801 rows globally when
timestamps fall on whole seconds. The new global budget fills in 11.6 days
without eviction, or in 7 days at about 1.65 retirements per second across nodes.
A node reaches 100,000 in-horizon rows after 27.8 hours at 1 per second. Its
seven-day steady-state allowance is about 0.165 retirements per second.

For storage planning, a 256-byte target, 36-byte node UUID, 32-byte holder, and
16 bytes of generation/time take about 340 bytes of row data. The primary key
copies target/node/holder, and the age and node/age indexes add keys and row IDs.
Allow roughly 1 KiB per record after SQLite cell overhead and page slack, about
1 GiB at the global cap. Maximum-length ASCII identities and 1,024-byte targets
can require several GiB. These are estimates, not disk quotas. Reserve additional
space for WAL, backups, and filesystem overhead, and measure the actual database.

When a new marker cannot fit, that retiring operation rolls back. An unrelated
expired claim that cannot be retired stays in place and does not block another
node's live retry. An acquire on that expired target still refuses. A fresh
acquire requires retirement capacity regardless of whether it supplies a
generation, so a delayed acquire cannot slip through while its missing-holder
cleanup is refused for capacity.
Existing live-holder retries and renewals keep their normal behavior.

Capacity checks reclaim a bounded batch of out-of-horizon rows before refusing.
A large expired backlog may require several transactions to reclaim. No
in-horizon marker is evicted to admit a request. Corrupt or missing current-schema storage refuses
without repair. Claim mutation fencing failures return HTTP 409 with
`reason: "claim-fence-refused"` and `owner: {}`. The empty owner object carries
no ownership identity. It makes pre-fencing clients classify the refusal as
`held`, since an owner-less 409 maps to `missing` and can permit local-only work
after a retry. This is a terminal authority refusal. The client
raises from `repo_claim` instead of admitting work under only the local lock,
and a heartbeat treats it as terminal ownership loss. Transport outages retain
their existing handling. Read-only dashboard, Command Deck, and API GETs return
HTTP 503 with the same reason so uptime monitors detect corrupt fence storage.
New targets over 1,024 UTF-8 bytes return HTTP 409 with
`reason: "claim-target-too-long"` and `owner: {}`, preventing local fallback in
both current and base clients. Invalid UTF-8 retains its validation refusal.
Orphan cleanup retries `claim-fence-refused` with
backoff until its TTL deadline. An ambiguous initial acquire followed by this
refusal schedules cleanup without admitting work. Final release refusals on exit
or after a rejected late grant also schedule background cleanup so recovery can
retire the holder. Ordinary exit also schedules a holder-scoped background
release on `hub-unavailable`, with retries bounded by the TTL. The daemon may
end earlier if the process exits.

`GET /status` includes `claim_fencing.retired_holder_rows` and
`claim_fencing.retired_holder_horizon_seconds`. On refusal, stop the source of
excess retirements and inspect those values and the error text. A per-node quota
needs that node's oldest records to age out. Global exhaustion needs sufficient
rows to age out or a reviewed capacity increase. Ordinary writes reclaim eligible
rows. Retried cleanup and acquisition must receive a successful authoritative
answer before work resumes. Back up before investigating corrupt storage, restore
a verified consistent database, and never delete target fences or recent
retirement records to clear an error.

## Finite-horizon residual

A generation-less request from a holder retired more than H seconds ago is
treated as a fresh holder. A delayed acquire can recreate that holder's claim.
Later delayed renewals with the same target, node, and holder can repeatedly
extend it, and a delayed holder release can delete it. The residual has no
one-TTL bound. Reusing those same identities after marker eviction also permits
same-holder ABA: an older generation-less renew or release can affect the new
tenure. Missing-holder release records a new cleanup marker, but cannot provide
protection against arbitrary replay after that marker also ages out.

Target generation fences are never evicted, so requests with an obsolete
explicit generation remain refused after holder history ages out. Protection
against requests delayed arbitrarily long belongs to #1737's v2 protocol.

The horizon depends on the hub wall clock. This implementation trusts clock
integrity rather than comparing ages to the newest marker, which may itself be
written after a forward jump. A forward jump beyond seven days can evict fresh
markers and enable the replay sequence above. A backward jump retains markers
longer and can exhaust capacity. Before relying on the horizon, check the hub's
UTC time against an independent trusted clock and verify time synchronization
with `timedatectl status` or the platform equivalent. Monitor for clock steps.
If a jump occurs, pause claim writers and investigate the clock and consistent
database backups. Correcting the clock cannot reconstruct evicted history.

## Upgrade and rollback

Schema 32 to 33 introduced retirement records. Schema 34 adds `retired_at` and
both retention indexes. Forward migration preserves every live claim and target
generation. A v33 record has no original retirement time, so migration assigns
the upgrade time and retains it conservatively for another full horizon. An
unmigrated v32 export validates its target fences without requiring retirement
storage. The hub continues refusing newer schemas. Upgrade cannot reconstruct
holders retired before retirement storage existed.

Upgrade clients before relying on capacity fail-closed. The 409 plus empty
owner compatibility response prevents fallback in the base client tested here,
but older hubs and other client versions may still fall back to local-only work.
Verify the deployed client behavior and the hub clock before rollout.

Before upgrade, stop writers and take a consistent SQLite backup. Keep the hub
binary and database backup together. For rollback from v33 or v34 to a v32 hub,
stop the upgraded hub and all claim writers, drain live claims, and allow at
least H since the final old-client request or abandon the old request queues.
Restore the paired pre-upgrade backup and binary, then restart writers with fresh
holders. Restoration can lose claim and fence changes after that backup. Keep
writers stopped until the operator has reconciled that loss with actual work.
Do not merely lower `user_version`, drop retirement storage, or start an older
binary against the upgraded database. An in-place v34 to v33 downgrade is not
supported. Restore the paired v33 backup under the same stopped-writer procedure.

The per-target fence retains its 100,000-target cap, 1,024-byte new-target bound,
and signed-64-bit exhaustion refusal. Restore epochs and exact-attempt
cancellation remain outside this legacy slice. No deployment is part of this fix.
