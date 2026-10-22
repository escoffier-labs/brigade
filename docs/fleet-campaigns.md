# Fleet campaigns on the Hub

A campaign runs one task across a set of repositories as a single tracked unit.
Slices 1 and 2 of #1128 add membership, a rollup, and bounded member ownership.
Campaign creation launches nothing. An explicit node take grants ownership without
changing item status or recording an attempt.

Campaigns need the Worklore feature gate on the Hub, as `brigade fleet work` does.

## What a campaign is

A campaign is a Worklore item of kind `fleet`. Each member is a Worklore item of kind
`repo`, one per repository, titled `<campaign title> [<repo>]`. Two small Hub tables
(`work_campaigns`, `work_campaign_members`) record which items belong together, so
there is no second task store. Every item gets the usual `created` event, and adding
members to an existing campaign appends a `campaign-members-added` event to the
campaign item.

A member is keyed by canonical repository identity, not by the `repos.toml` id or by
checkout path. Identities are lowercased `owner/repo` (or `host/path`), so `Acme/One`
and `acme/one` are one member, and a leading `github.com/` is dropped, so
`github.com/acme/one` is the same member too. The identity comes from the repository's origin remote,
the same way run claims derive it, with any `@<worktree>` suffix removed. The `repos.toml` path must be the checkout
root itself (main checkout or linked worktree). A subdirectory or a missing path is
refused with `repository_identity_unresolved`, so it never borrows the enclosing repo. A repository
with no origin remote has no owner segment and is refused with
`repository_identity_unresolved`.

The spec is the campaign item's description and acceptance. A member stores only its
override text, which becomes the member item's description, and a fingerprint of the
combined spec it was created from.

## Create

```
brigade fleet campaign create --campaign-id KEY --title TEXT \
  --acceptance TEXT [--acceptance TEXT ...] \
  [--description-file PATH] [--overrides PATH] [--offer-file PATH] [--repos ID,ID] \
  [--priority P] [--target DIR] [--dry-run] [--json]
```

Repositories are the enabled `repos.toml` entries, or the ids given to `--repos`, with
the same refusal rules as `campaign preview`. The command refuses when an identity
cannot be resolved or when two entries resolve to the same identity. `--overrides` is a
JSON object that maps a `repos.toml` id to override text. An unknown id is refused.

`--dry-run` writes nothing. It prints the request it would send and, when a Hub is
configured, makes one read to mark which members already exist and whether the spec
or immutable offer would be refused. JSON reports `spec_changed` and `offer_changed`.
Text names `campaign-spec-changed` and `campaign-offer-conflict` independently when
both differ. A failed read or unreadable stored offer is reported as unchecked.

## Idempotency

`(campaign key, repository identity)` is the idempotency key. Running `create` again
adds only the members that are missing and reports `added`, `existing` and
`override_conflicts`. Existing members are never rewritten. A request whose override
differs from the stored one is listed under `override_conflicts` and the stored
override is kept. A different title on a re-run is ignored. New members use the stored
campaign title.

Each campaign is limited to 256 canonical repository identities in total across all
creates. An extension beyond this limit writes nothing. Retrying only existing members
at the limit is allowed. The Hub request body cap (`WORKLORE_BODY_BYTES`, 256 KiB)
separately limits how much override text one create can carry. Split a large override
set across repeated creates within the campaign total: each one adds only the members
that are missing.

Creation is one transaction. A campaign is never left with some of its members.
The CLI reports success only after a valid Hub acknowledgement. An unexpected response
exits nonzero because the write may have been applied. Retry with the same campaign key
and spec to add only missing members.

## Spec changes are refused

If a re-run carries a description or acceptance that differs from the stored campaign
item, the Hub refuses with `campaign-spec-changed` (HTTP 409) and writes nothing. To
change the spec, patch the campaign item (`brigade fleet work patch`).

## Drift

The campaign spec fingerprint is the SHA-256 of the canonical JSON of the campaign
item's description and acceptance. A member fingerprint is the SHA-256 of the campaign
spec fingerprint plus the member override. A member is `drifted` when its stored
fingerprint differs from the one recomputed from the campaign item's current spec.
Drift is reported and never rewritten.

## List and show

```
brigade fleet campaigns [--limit N] [--json]
brigade fleet campaign show KEY [--json]
```

`campaigns` prints one row per campaign, newest first, with the rollup counts. The
list is capped at 50 campaigns (default 20). `show`
prints the summary, then one row per repository: bucket, status, attempts, owner node,
latest Brigade task (`task`), latest Fleet execution (`run`), latest GitHub PR and
whether it drifted. In JSON, `brigade` retains the Brigade task link and `fleet_run`
projects the latest `fleet-run` link recorded by `link_execution`. Text output strips
terminal control characters. The Hub routes are `POST /work/campaigns` (operator only,
201 when created, 200 otherwise), `GET /work/campaigns` and
`GET /work/campaigns/<key>`. The key in the path may be percent-encoded.

## Buckets

Each member falls in exactly one bucket, checked in this order:

| Bucket | Meaning |
| --- | --- |
| `completed`, `canceled`, `archived` | The item reached that status. |
| `blocked` | The item is blocked. |
| `awaiting_gate` | The item is verifying. |
| `in_flight` | Claimed or running, or a node holds its ownership. |
| `attempt_limited` | Not terminal, not in flight, and attempts are at the limit (2). |
| `unowned` | Everything else. |

A campaign is `done` when every member is completed, canceled or archived.


## Authorization offer

`--offer-file` supplies a bounded JSON object. The Hub records it as an immutable
`campaign-offered` event on the campaign item, in the same transaction as creation:

```json
{
  "allowed_nodes": ["node-a", "node-b"],
  "per_node_concurrency": 1,
  "attempt_budget": {"cap": 2, "source_ref": "budget-1"},
  "authorization_ref": "grant-1",
  "exclusions": ["acme/excluded"],
  "lease_seconds": 300
}
```

The HTTP create body uses the same object under `offer`. All fields except
`lease_seconds` are required. Allowed nodes must be distinct enrolled-node identifiers
(1 to 32 entries). Enrollment is checked at take time, so an offer can name a node
before it enrolls. Per-node concurrency is 1 to 32 and applies within this campaign.
The attempt cap is 1 to 100000, and assignment uses the lower of that cap and the
Worklore attempt limit (2), per member. Exclusions are exact canonical repository
identities (up to 32), normalized like member identities. They are never globs or
free-form instructions. Authorization and budget references use the existing bounded
ownership reference format. Lease duration is 1 to 3600 seconds, default 300.

A campaign without an offer refuses self-assignment, including campaigns created
before slice 2. An operator can attach an offer by repeating create with the same
spec and an explicit offer. Repeating an identical offer writes no new authorization
event. A different offer is refused with `campaign-offer-conflict` (409), including
any new members in that request. Omitting the offer preserves existing authorization.
This slice provides no offer editing or lease renewal. Cancel or block the campaign
to stop new takes and new holder progress. A holder can still release it.

The offer is included in campaign list/show JSON. `GET /work/items/<member-work-id>`
includes `item.campaign` with `campaign_key`, the campaign `work_id`, and the member's
canonical `repo`. This backlink is derived from membership for old and new items.

## Take a member

An enrolled node explicitly takes a known member through the existing ownership API:

```python
from brigade import worklore_client

nonce = worklore_client.new_ownership_nonce()  # Retain privately for this grant.
grant = worklore_client.ownership_action(
    member_work_id,
    {"action": "take", "generation": observed_generation},
    if_match=observed_ownership_revision,
    idempotency_key="take-request-1",
    holder_nonce=nonce,
)
```

This sends `POST /work/items/<member-work-id>/ownership`, with `If-Match`,
`Idempotency-Key`, and `X-Worklore-Holder`. Identity comes from the authenticated node
bearer. The Hub admin bearer cannot take ownership, and the body accepts no node id.
Read the current generation and ownership revision with `get_ownership` first.
The ownership revision is independent of the work-item version.

The Hub checks campaign authorization, member eligibility, the node's occupied slots,
and the ownership grant under one `BEGIN IMMEDIATE`. It rejects excluded repositories,
terminal/blocked/deferred campaigns, terminal/blocked/deferred/in-flight members,
specification drift, future review dates, blockers, exhausted attempts, and nodes
outside the offer. A fresh member can be captured or ready. A claimed or running
member can be reclaimed only after its previous campaign grant expires and while
its attempts remain under the limit. Verifying members wait on their gate.

A successful take appends `ownership-taken` with a new generation, the authorization
event reference, copied offer bounds, authenticated owner, and `expires_at`. The holder
capability stays header-only and is stored only as a digest. A node cannot bypass the
campaign offer through the older per-item offer, accept, or handoff actions. Existing
noncampaign ownership keeps that protocol. Checkpoints must name the member's
canonical repository and still pass path admission.

The idempotency key belongs to the authenticated node. An exact replay returns the
original grant, even at the concurrency cap, without a new event, slot, generation,
attempt, or deadline. Reusing the key for a different member, revision, body, or nonce
is refused. A revoked node cannot replay. A historical reply carries its original
generation and deadline, so it cannot restore authority after expiry or reclaim.

Expired campaign grants cease to occupy a slot. A new take needs the current revision,
generation, and a fresh nonce. It fences the old holder and clears the previous
checkpoint scope and report. Before reclaim, expired checkpoint/report writes are
already refused. Release remains generation- and capability-checked. Legacy grants
without a campaign deadline never expire implicitly. Their holders must release them
(or an operator must withdraw an outstanding legacy offer) before take can succeed.

The deadline fences the Hub ownership protocol. It does not kill a process or replace
physical checkout locks. Path reservations remain conservative until a release or
reclaim clears them. No node consumer, process launch, cloud lease, completion gate,
or automatic selection loop is added by this slice. The request body limit remains
256 KiB.

## Campaign execution and attempt writes

New node writes to `/work/items/<id>/execution` and `/attempts` require both
`X-Worklore-Holder` and `X-Worklore-Generation` for the current unexpired grant.
The Python client's `link_execution` and `record_attempt` accept the corresponding
`holder_nonce` and `generation` keyword arguments. Attempt requests still use the
item version in `If-Match`. These checks run inside each existing write transaction.
New execution events bind the run to that grant's generation, nonce digest, and
authorization event. An old run cannot spend a reclaimed grant's attempt budget,
even when the same node becomes holder again. Use a new run for the new grant.
Cancellation, expiry, reclaim, or an exhausted budget prevents new node writes.
A historical own-run replay can return its existing record without adding events,
links, or attempt counts. It cannot rebind the run or restore authority.
After the 50-link retention window evicts a campaign execution, relinking returns
`link-conflict`, even for its original grant or an admin or operator observation.
The immutable execution history reserves that node/run identity across generations
and work items, including noncampaign destinations. A campaign member also refuses
relinking unbound legacy execution history, which carries no authenticated grant.
Retained unbound observations remain inert replays and cannot authorize node attempts.
Ordinary noncampaign runs with only unbound history keep their existing relink behavior.

Admin and configured operator observations use their existing explicit trusted
authority, including recording failures or resetting attempts after cancellation.
An operator's execution observation does not grant a node holder capability.
Only the admin can observe another node's run. Noncampaign execution and attempt
behavior is unchanged. Explicit campaign reservation recovery requires the
acknowledged physical quiescence and audited operator action documented in
[ownership recovery](worklore-ownership.md#capacity-and-recovery).
