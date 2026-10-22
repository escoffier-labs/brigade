# Fleet campaigns on the Hub

A campaign runs one task across a set of repositories as a single tracked unit.
Slice 1 of #1128 adds membership and a rollup. It launches nothing: no run, claim,
lease or dispatch is created, and no member changes state because a campaign exists.

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
  [--description-file PATH] [--overrides PATH] [--repos ID,ID] \
  [--priority P] [--target DIR] [--dry-run] [--json]
```

Repositories are the enabled `repos.toml` entries, or the ids given to `--repos`, with
the same refusal rules as `campaign preview`. The command refuses when an identity
cannot be resolved or when two entries resolve to the same identity. `--overrides` is a
JSON object that maps a `repos.toml` id to override text. An unknown id is refused.

`--dry-run` writes nothing. It prints the request it would send and, when a Hub is
configured, makes one read to mark which members already exist and whether the spec
would be refused. A failed read is reported in the output.

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
