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
cleanup, even if its first arrival occurs after the final cleanup or a restart.
Force release retires the actual deleted holder, regardless of the operator's
node identity. An in-flight renew or reacquire cannot resurrect that holder.

Generation-less operations using a retired holder return HTTP 409 with
`reason: "stale-holder"` and the action's success field set to false. They do not
renew, release, or reacquire a newer claim. Refusals never disclose holder tokens.
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

## Storage and upgrade

Schema 32 to 33 adds `claim_retired_holders` without changing live claims or
existing target generations. The hub continues refusing newer schemas. Upgrade
the hub before relying on retirement protection. Migration cannot reconstruct
holders retired before the upgrade, so those historical requests are outside
the protection boundary.

Retired holders are retained indefinitely, with a separate cap of 100,000
distinct `(target, node, holder)` records. Existing records may be updated at
capacity. An operation needing a new record refuses with HTTP 503 and
`claim retired-holder fence capacity exceeded`. The entire transition rolls
back, including any expiry pruning. Existing fences are never evicted. Corrupt
or missing current-schema retirement state refuses startup and operations
without repair. A schema upgrade preserves and validates any retirement table
already present.

The per-target fence retains its existing 100,000-target cap, 1,024-byte new-target
bound, and signed-64-bit exhaustion refusal. Capacity recovery requires reviewed
capacity planning while preserving all fences. These finite budgets can refuse
otherwise valid operations. Backup rollback and deleted free-target history
remain outside the authority this database can establish. Restore epochs and
exact-attempt cancellation belong to the later protocol work.
