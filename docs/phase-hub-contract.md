# Fleet Hub wire contract

The Fleet Hub is the HTTP service `brigade fleet serve` runs
(`src/brigade/fleet_hub.py`, `src/brigade/fleet_hub_http.py`, and the
`fleet_hub_*` helper modules). A Rust rewrite (axum, tokio, rusqlite) has to
replace it without any client noticing. This page records the HTTP and JSON
contract the Python hub serves today, and marks which routes the
`verify-brigade` contract drive checks.

The drive is `control-brigade.py hub-contract`
(`registry/skills/verify-brigade/hub_contract.py`). It talks to a hub over
HTTP only, so the same checks run against any hub binary. How to run it is in
[`registry/skills/verify-brigade/features/hub-contract.md`](../registry/skills/verify-brigade/features/hub-contract.md).

## Contract version

`contract_version` is **1**. It is `CONTRACT_VERSION` in `hub_contract.py`,
and every drive result reports it.

A breaking change is any hub change that makes a drive case fail against a hub
that passed it before:

- a different status code for the same request
- a required response key removed, renamed, or given a different JSON type
- a different `code` value, or different text for an error message the drive
  pins exactly
- a broken paging guarantee: message ids not ascending, `next_cursor` not equal
  to the last id on the page, or `after` not excluding ids at or below it
- a route moved, or an auth requirement tightened or loosened

Adding response keys, adding routes, and changing timestamps, generated ids,
tokens, or key order are not breaking. The drive never compares those. A
breaking change bumps `CONTRACT_VERSION` and updates the inventory below in the
same PR.

The Python hub has **no version endpoint**. `GET /health` reports liveness and
the service name only, and `GET /version` is a 404. A client cannot ask a hub
which contract it speaks. The drive records that as observed and does not
require a version route.

## Assumptions

The drive starts the hub with its default flags. Admin writes are refused
(no `--allow-admin-writes`), plaintext messages are accepted (no
`--require-sealed`), and Worklore is off (`BRIGADE_WORKLORE_ENABLED` unset).
Each flag changes the answers for some routes. A hub under test must start
the same way.

One environment variable is set: `BRIGADE_FLEET_OPERATOR_RECIPIENT` names a
fresh `agent:contract-operator-<hex>` principal, which the drive enrolls and
grants `node:*`. The Python hub reads it on every `question_create`, so the
question actions have an operator to route to. A hub that reads it under
another name answers `403 question_operator_invalid` instead.

No hub mesh identity is installed (`/mesh/identity` is never uploaded), so
the hub accepts any well-formed `h_` hub id in key records and envelopes.

## Conventions every route shares

- Authentication is `Authorization: Bearer <token>`. Three kinds of token
  exist: the admin token (startup secret), a node token (from
  `POST /nodes` `add`), and a message-principal token (from
  `POST /message-principals` `add`, valid on `POST /messages` only).
- A missing, unknown, or non-Bearer credential is `401 {"error": "unauthorized"}`.
  A revoked node token is `401 {"error": "unauthorized: node token revoked"}`.
  The `/policy` routes use a different envelope:
  `401 {"error": {"code": "auth-failed", "message": "unauthorized"}}`.
- A `token` query parameter is refused on every GET and POST with
  `401` and a `text/plain` body (`Dashboard request refused.`), before routing.
- An unknown path is `404 {"error": "not found"}` for GET, POST, PUT, PATCH,
  and DELETE.
- JSON responses carry `Content-Type: application/json`,
  `Cache-Control: no-store`, and `Referrer-Policy: no-referrer`.
- A body that is not JSON is `400 {"error": "body is not valid JSON"}`.

## Route inventory

Auth column: `none`, `admin`, `node` (node token), `principal` (message
principal token), `bearer` (admin or node), `browser` (admin bearer, dashboard
session cookie, or trusted Tailscale identity). Status column: `covered` means
the drive checks the route, with the scope noted, and `not yet` means a later
slice. The tables list 98 method and path pairs: 54 JSON API routes, 37 browser
routes, and 7 static assets. The drive covers 14, all JSON API routes, and
for several of them only part of the request space (see the Status column).

### Health and identity

| Method | Path | Auth | Request | Response | Status |
|---|---|---|---|---|---|
| GET | /health | none | none | 200 `{ok: true, service: "brigade-fleet-hub"}` | covered |
| GET | /mesh/identity | none | none | 200 hub identity document, or 404 `{error: "identity not found"}` | not yet |
| POST | /mesh/identity | admin | identity upload | 200 upload result | not yet |
| POST | /mesh/statements | admin | signed statement | 200 upload result | not yet |
| GET | /favicon.svg | none | none | static asset, `public, max-age=86400` | not yet |
| GET | /favicon.ico | none | none | static asset | not yet |
| GET | /favicon-32x32.png | none | none | static asset | not yet |
| GET | /apple-touch-icon.png | none | none | static asset | not yet |
| GET | /icon-192.png | none | none | static asset | not yet |
| GET | /icon-512.png | none | none | static asset | not yet |
| GET | /site.webmanifest | none | none | static asset | not yet |

### Registration and control plane

| Method | Path | Auth | Request | Response | Status |
|---|---|---|---|---|---|
| POST | /nodes | admin | `{action: "add", node_id, label?}` or `{action: "revoke", node_id}` | add: 200 `{added: true, node, token}`, 409 `{added: false, error}`, revoke: 200 `{revoked: true, node}`, 404 `{revoked: false, error}` | covered (add, revoke, duplicate, unknown, bad action, node-token refusal) |
| GET | /nodes | admin | none | 200 `{nodes: [{node_id, label, created_at, revoked_at}]}`, node token 403 | covered |
| POST | /message-principals | admin | `{action: "add", name, ...}`, `get`, `grants`, `revoke` | add: 201 `{principal: {name, principal_id, label, enrollment_id, may_send_to, token}}`, grants: 200 `{principal: {principal_id, may_send_to, ...}}` | covered (add, grants, and node-token refusal; get and revoke not yet) |
| GET | /preference | bearer | none | 200 `{preference: {...}}` | not yet |
| PUT | /preference | admin | `{preference: {...}}` or the bare object | 200 `{preference: {...}}`, node token 403 | not yet |
| GET | /audit | admin | `?after_id=&limit=` | 200 `{audit: [...]}`, no token 401, other token 403, both `{error: "admin token required"}` | not yet |

### Heartbeat: run events and sessions

| Method | Path | Auth | Request | Response | Status |
|---|---|---|---|---|---|
| POST | /events | node | one event or an array (max 1000): `node_id, run_id, state, ts, sequence, digest`, optional `repo, seat, harness, capability_fingerprint, repo_identity, exit_status, context` | 200 `{accepted, duplicate}`, foreign node_id 403, bad field 400, admin token 403 | covered |
| GET | /status | bearer | `?all=1` adds terminal runs | 200 `{claim_fencing: {retired_holder_horizon_seconds, retired_holder_rows}, runs: [...]}` | covered |
| POST | /sessions | node | `{action: "upsert", harness, session_id, repo_identity, identity_scope, repo_label, checkout_path, branch, dirty_paths, dirty_truncated, ttl_seconds, cloud_context?, provider_link?}` or `{action: "end", harness, session_id, repo_identity}` | 200 `{session: {...}}`, `node_id` on a node write 400 | covered |
| GET | /sessions | bearer | `?all=1` adds ended and expired sessions | 200 `{sessions: [...]}` | covered |

### Claims and ownership

| Method | Path | Auth | Request | Response | Status |
|---|---|---|---|---|---|
| POST | /claims | node | `{action: acquire\|renew\|release\|inspect, target, node_id, holder?, ttl_seconds?, scope?, generation?, conductor?, lock?, supersede?, harness?, role?, job?, session?}` | acquire 200 `{granted: true, claim}` or 409 `{granted: false, error, owner}`, renew 200 `{renewed: true, claim}`, release 200 `{released: true}`, inspect 200 `{inspected, owned, generation, claim}` | covered (holder, node, and force scopes, run-lease supersession, generations and stale refusals, retired holders, lease expiry, and field bounds) |
| GET | /claims | bearer | `?all=1` adds expired claims | 200 `{claims: [{...claim, expired}]}` | covered |
| GET | /owners | bearer | none | 200 `brigade.fleet_owners.v1` view | covered |

### Messages

| Method | Path | Auth | Request | Response | Status |
|---|---|---|---|---|---|
| POST | /messages | node or principal | `{action, ...}`: `send`, `list`, `count`, `ack`, `sent`, `identity`, `reply`, `sent_lookup`, `who`, `claims`, `keys_publish`, `keys_get`, `manifests_get`, `question_create`, `question_status`, `question_desktop`, `events_list`, `events_subscribe`, `events_unsubscribe` | send 201 `{message}`, list and sent 200 `{messages, next_cursor}`, count 200 `{unread_count}`, ack 200 `{message}` or 404, identity 200 `{principal}`, admin token 403 | covered (16 of 19 actions, see the action table, plus sealed envelopes and `wait_ms` long polls; `events_*` not yet) |

### Message actions

`POST /messages` dispatches on the body's `action`. The v2 route table assigns
each action to a family, so the drive tracks coverage per action as well:
`COVERED_MESSAGE_ACTIONS` in `hub_contract.py` must match the `covered` rows
here. The action table lists 19 actions, and the drive covers 16.

| Action | Family | Request | Response | Status |
|---|---|---|---|---|
| `send` | messages | plaintext `{to, subject, body, ref?, priority?, client_id?, enqueued_at?, reply_to?}` or `{envelope}` | 201 `{message}`, sealed messages add `envelope`, `envelope_bytes`, `content_state` | covered (plaintext, sealed, `reply_to`, idempotent retry, conflicts, every sealed refusal below) |
| `list` | messages | `{after, unacked, wait_ms?}` | 200 `{messages, next_cursor}`, `wait_ms` over the cap 429 `message_wait_capped` | covered (paging, long poll timeout, wake, waiter cap) |
| `count` | messages | none | 200 `{unread_count}` | covered |
| `ack` | messages | `{id}` | 200 `{message}`, 404 `{error}` | covered |
| `sent` | messages | `{after}` or `{ids}` | 200 `{messages, next_cursor}` | covered |
| `sent_lookup` | messages | `{client_id}` only | 200 `{message}`, 404 `{error, code: "not_found"}` | covered |
| `reply` | messages | `{id, body, ...}` or `{id, envelope}` | 201 `{message}`, 404 `{error}` | covered (plaintext, sealed, retry, refusals) |
| `identity` | messages | none, or `{peers}` for an agent | 200 `{principal}` or `brigade.message_authorization.v1` | covered |
| `who` | messages | `{all?, limit?, cursor?, filter?}` | 200 `{nodes, principals, sessions, total, truncated, oversized, next_cursor}` | covered (first page, bad arguments) |
| `claims` | messages | `{all?, limit?, cursor?}` | 200 `{claims, total, truncated, oversized, next_cursor}` | covered (node and ungranted principal) |
| `keys_publish` | messages | `{record}` | 201 `{key}`, 409 `principal_key_exists`, 403 `principal_key_mismatch` | covered (first key; rotation with `prev_kid` not yet) |
| `keys_get` | messages | `{kid}` | 200 `{key}`, 404 `{error: "key not found"}` | covered |
| `manifests_get` | messages | none | 200 `{statements}` | covered (empty directory only) |
| `question_create` | messages | `{question, timeout_secs?, request_id}` | 201 question, 200 on retry, 409 `question_request_conflict`, 429 `question_station_busy` | covered |
| `question_status` | messages | `{question_id}` | 200 question, 403 `question_not_owned` | covered |
| `question_desktop` | messages | `{question_id, answer}` | 200 question | covered |
| `events_list` | cold JSON | event cursor | events page | not yet |
| `events_subscribe` | cold JSON | subscription | subscription | not yet |
| `events_unsubscribe` | cold JSON | subscription id | result | not yet |

### Fleet routing, cloud, models, policy

| Method | Path | Auth | Request | Response | Status |
|---|---|---|---|---|---|
| GET | /cloud | bearer | `?view=capabilities`, `?all=1` | 200 cloud snapshot, or `{schema: "brigade.fleet_cloud.v1"}` for capabilities | not yet |
| POST | /cloud | node (admin for `policy`) | `{action: admit\|bind\|renew\|release\|policy, ...}` | lease decision | not yet |
| GET | /models | bearer | none | 200 projected model roster | not yet |
| POST | /models | node | lease `acquire`/`release`, roster `set`/`set-default`/`retire`, `admit` | lease or roster result | not yet |
| GET | /policy | bearer | `?history=1&limit=` | 200 policy document, bad limit 400 `{error: {code: "invalid-request", message}}` | covered (401 envelope only) |
| GET | /policy/status | bearer | none | 200 policy status | not yet |
| GET | /policy/inventory | bearer | none | 200 model inventory | not yet |
| POST | /policy | admin (`preview`, `save`, `rollback`) or node (`resolve`, `prepare`, `acknowledge`, `route`, `work-next`, `reservation-renew`, `reservation-release`, `telemetry-observe`, `delegation-create`, `delegation-show`), `refresh`, `quota-ingest`, `inventory-ingest` either | `{action, ...}` | action result, errors `{error: {code, message}}` | not yet |
| POST | /grokbot | node or actor (admin: `list`, `status`, `report-metadata`, `enroll-actor`) | `{action: enqueue\|list\|status\|whoami\|cancel\|expire\|report-metadata\|claim\|start\|renew\|complete\|fail\|ack-cancel\|enroll-actor, ...}` | queue result | not yet |

### Reports and CI

| Method | Path | Auth | Request | Response | Status |
|---|---|---|---|---|---|
| POST | /skills/report | node | skills inventory | 200 stored, admin 403 | not yet |
| POST | /mcp/report | node | MCP server inventory | 200 stored, admin 403 | not yet |
| POST | /projects/board | node | project board snapshot | 200 stored, admin 403 | not yet |
| POST | /stats | stats collector node | stats batch | 200 stored | not yet |
| GET | /ci | bearer | none | 200 CI view | not yet |
| GET | /ci/work | bearer | none | 200 CI work view | not yet |
| POST | /ci | node | CI observations | 200 stored | not yet |
| POST | /ci/refresh | admin | none | 200 refresh result | not yet |
| POST | /actions | bearer | action API payload | action result | not yet |

### Worklore (off by default)

Every `/work` route answers `404 {"error": "not found"}` while Worklore is off,
whatever the token. The drive pins that default on `GET /work/items`.

| Method | Path | Auth | Request | Response | Status |
|---|---|---|---|---|---|
| GET | /work/items | bearer | query filters | Worklore off: 404 `{error: "not found"}`, on: item list | covered (off-by-default gate only) |
| POST | /work/items | node | work item | item | not yet |
| GET | /work/items/{id} | bearer | none | item | not yet |
| PATCH | /work/items/{id} | node | item patch | item | not yet |
| GET | /work/items/{id}/prepared-handoff | bearer | none | handoff | not yet |
| GET | /work/items/{id}/ownership | bearer | none | ownership | not yet |
| POST | /work/items/{id}/ownership | node | ownership action | ownership | not yet |
| GET | /work/items/{id}/events | bearer | none | events | not yet |
| POST | /work/items/{id}/transitions | node | transition | item | not yet |
| POST | /work/items/{id}/attempts | node | attempt | attempt | not yet |
| POST | /work/items/{id}/links | node | link | link | not yet |
| DELETE | /work/items/{id}/links/{link} | node | none | result | not yet |
| GET | /work/campaigns | bearer | query filters | campaigns | not yet |
| POST | /work/campaigns | node | campaign | campaign | not yet |
| GET | /work/campaigns/{key} | bearer | none | campaign | not yet |
| GET | /work/events | bearer | query filters | events | not yet |
| GET | /work/queue/burn | bearer | query filters | burn queue | not yet |
| POST | /work/imports | node | import batch | import result | not yet |

### Browser surfaces (HTML)

These serve the Command Deck and the dashboard. They answer HTML or form
redirects, not JSON, and no Brigade client calls them. A Rust hub can keep
proxying them to the Python renderer or port them later. The contract drive
leaves them alone.

| Method | Path | Auth | Request | Response | Status |
|---|---|---|---|---|---|
| GET | / | browser | none | Command Deck HTML | not yet |
| GET | /deck | browser | none | Command Deck HTML | not yet |
| GET | /deck/repos | browser | none | HTML | not yet |
| GET | /deck/alerts | browser | none | HTML | not yet |
| GET | /deck/capacity | browser | none | HTML | not yet |
| GET | /deck/capacity/cloud | browser | none | HTML | not yet |
| GET | /deck/capacity/github | browser | none | HTML | not yet |
| GET | /deck/activity | browser | none | HTML | not yet |
| GET | /deck/projects | browser | none | HTML | not yet |
| GET | /deck/pulls | browser | none | HTML | not yet |
| GET | /deck/reporting | browser | none | HTML | not yet |
| GET | /deck/roster | browser | none | HTML | not yet |
| POST | /deck/roster | browser | form | 303 or HTML | not yet |
| GET | /deck/policy | browser | none | HTML | not yet |
| POST | /deck/policy | browser | form | 303 or HTML | not yet |
| GET | /deck/observed/{...} | browser | none | HTML | not yet |
| GET | /deck/work | browser | none | HTML | not yet |
| GET | /deck/stats | browser | none | HTML | not yet |
| GET | /deck/owners | browser | none | HTML | not yet |
| GET | /deck/skills | browser | none | HTML | not yet |
| GET | /deck/skills/plan | browser | none | HTML | not yet |
| POST | /deck/skills | browser | form | 303 or HTML | not yet |
| GET | /deck/pr-rules | browser | none | HTML | not yet |
| POST | /deck/pr-rules | browser | form | 303 or HTML | not yet |
| GET | /deck/mcp | browser | none | HTML | not yet |
| GET | /deck/ci | browser | none | HTML | not yet |
| GET | /deck/actions/enable | browser | none | HTML | not yet |
| GET | /deck/actions/compose | browser | none | HTML | not yet |
| POST | /deck/actions/submit | browser | form | HTML | not yet |
| GET | /deck/actions/aa_{id} | browser | none | HTML | not yet |
| GET | /view/machines | browser | none | dashboard HTML | not yet |
| GET | /view/repos | browser | none | dashboard HTML | not yet |
| GET | /enroll | enrollment code | `?code=` | HTML | not yet |
| POST | /enroll | enrollment code, same origin | form | sets the dashboard cookie | not yet |
| GET | /dashboard/sessions | admin | `?all=&after=` | JSON session page | not yet |
| POST | /dashboard/sessions | admin | JSON | JSON | not yet |
| POST | /dashboard/enrollment | admin | JSON | enrollment code | not yet |

## Clients that call the covered routes

Found with `brigade code callers` after `brigade code sync`, then traced by
hand where the Dot connector dispatches tools by name.

| Client | Function | Route |
|---|---|---|
| `brigade fleet status` | `fleet_client.fetch_status` | GET /status |
| `brigade fleet claims` | `fleet_client.fetch_claims` | GET /claims |
| `brigade fleet sessions`, the work brief | `fleet_client.fetch_sessions` | GET /sessions |
| `brigade fleet owners show` | `fleet_client_policy.show_owners` | GET /owners |
| `brigade fleet nodes add/revoke/list` | `fleet_client.add_node`, `revoke_node`, `fetch_nodes` | POST /nodes, GET /nodes |
| run event reporting and spool flush | `fleet_client._post_events` | POST /events |
| `repo_claim` and the claim helpers | `fleet_client._post_claim_blocking` | POST /claims |
| session publish and end (heartbeat) | `fleet_client._post_session` | POST /sessions |
| `brigade fleet msg send/list/ack/count/sent/identity`, `fleet_mcp`, `fleet_inbox_delivery`, `AskUser` | `fleet_client_messages.send`, `list_messages`, `ack`, `unread_count` | POST /messages |
| `brigade fleet msg principals` | `fleet_client_messages._principal_admin_request` | POST /message-principals |
| Dot/Duet `fleet_digest` | `DotHubTools.fleet_digest` runs `brigade fleet status --json`, `brigade fleet claims --json`, and reads the inbox | GET /status, GET /claims, POST /messages `list` |
| Dot/Duet `inbox_list`, `message_send` | `dot.inbox_tools` through `HubInbox._call` and `hub_messages.call_message` | POST /messages `list`, `send` |
| Dot/Duet `memory_search` | `dot.memory_tools.memory_search` runs `brigade memory search` locally | no hub route |
| Dot/Duet `fleet_work_next` (inside `fleet_digest`) | `brigade fleet work next` through `worklore_client` | `/work/*`, off by default |

## Hub behaviors recorded as observed

The drive pins these as the Python hub answers today. They are flagged here
for whoever owns the hub. This PR changes no hub code.

1. **No version endpoint.** Nothing over HTTP reports the hub version or the
   contract version, and `GET /version` is a 404.
2. **Two auth-failure envelopes.** `/policy`, `/policy/status`, and
   `/policy/inventory` answer `{"error": {"code": "auth-failed", "message":
   "unauthorized"}}`, and every other route answers `{"error": "unauthorized"}`.
   `GET /audit` also differs: a wrong token is 403, where other routes say 401.
3. **Admin token on `/messages` gets the events wording.** The 403 text says
   "the admin token may not post events or claims", which names the wrong
   route family.
4. **Some message errors carry no `code`.** Sending to an unknown recipient is
   `400 {"error": "recipient is not an active hub principal"}` with no `code`,
   and acking a message the caller did not receive is
   `404 {"error": "message not found"}` with no `code`. The client falls back on
   the status code for both. A rejected send without a grant does carry
   `code: "message_recipient_not_permitted"`.
5. **A message-principal token is a 401 outside `/messages`.** It is not
   recognized at all on node routes such as `/status` or `/events`, rather
   than refused with a 403.
6. **Token in the query is plain text.** The refusal body is
   `Dashboard request refused.` as `text/plain`, even on JSON API paths.

The hot-set cases (claim scopes and fencing, the message and question
actions, sealed envelopes, and long polls) pin these as well:

7. **A no-op force release still advances the generation.** Releasing a free
   target with `scope: "force"` answers `200 {"released": false}`, yet the
   target's generation goes up by one. The drive pins the jump (4 to 6 across
   one real and one no-op force release). Fences only need to be monotonic, so
   this is safe, but it may not be intended.
8. **A lapsed lease costs a generation.** Acquiring a target whose lease
   expired first retires the old holder (one advance), then grants (another),
   so the generation jumps from 1 to 3.
9. **`generation` is absent for a target never claimed.** `inspect` on an
   unseen target has no `generation` key; once a target has history, a free
   target reports it.
10. **A same-holder acquire retry may carry the pre-acquire generation.** The
    live holder retrying with `generation` one below the current one is
    granted, not refused as stale.
11. **Some refusals are 200.** A retired holder's `release` is
    `200 {"released": false, "reason": "stale-holder"}`, and a no-op force
    release is `200 {"released": false}`.
12. **`owner` varies on claim 409s.** A stale-holder acquire refusal carries
    `owner: {}`, a held target carries the claim, and renewing a lapsed lease
    carries `owner: null`. Deployed clients treat a 409 without an object
    `owner` as non-terminal.
13. **404 envelopes differ by action.** `sent_lookup` answers
    `{"error": "message not found", "code": "not_found"}`, while `ack`,
    `reply`, and `keys_get` (`key not found`) carry no `code`.
14. **The bad-action error lists 9 of 19 actions.** It says "action must be
    send, list, sent, count, ack, reply, who, claims, or identity", although
    the keys, question, `sent_lookup`, and `events_*` actions are accepted.
15. **Question ids that point at messages are strings.** `message_id`,
    `answer_id`, and the other message references in a question result are
    decimal strings, where message ids everywhere else are integers.
16. **Some sealed refusals carry no `code`.** An envelope sent with plaintext
    fields and a reply whose `reply_to` and signed `in_reply_to` disagree are
    plain `400 {"error"}`. Every other sealed refusal has a `code`
    (`envelope_malformed`, `message_too_large`, `message_sender_mismatch`,
    `sender_key_stale`, `message_sealing_required`). A key record whose id does
    not match its public keys is also a plain 400.
17. **The waiter cap is per recipient.** The third concurrent long poll for
    one inbox is `429 message_wait_capped`. The hub also caps waiters at 8
    fleet-wide (a quarter of its 32 handler threads) and at 2 per source
    address, with loopback exempt from the source cap. Connections are capped
    at 32 handlers, and at 8 per source address except a loopback peer of a
    loopback bind (#2229). The drive pins only the per-recipient cap.

The S1b cases (v2 slice S1b: expiry, the sealed-content lifecycle, the
`client_id` window, and the refusal sweep) pin these. The lifecycle cases run
on a hub whose clock the drive steps, so each boundary is exact:

18. **The two lifecycle boundaries fall on opposite sides.** A message created
    exactly 90 days ago is still listed and counted, and is gone one second
    later. A sealed message acknowledged exactly 7 days ago already reads as
    its tombstone, and one second earlier it is still sealed.
19. **Retention runs from `enqueued_at`.** A client stamp becomes the
    message's `created_at`, so a send stamped exactly 90 days ago is accepted
    and expires one second later, and a stamp up to 5 minutes ahead is
    accepted. One second past either limit is a 400, and only the future one
    carries a `code` (`message_timestamp_future`).
20. **The `client_id` window is the message's retention.** While the first
    message exists (day 89), a resend returns its id and a different payload
    is `409 message_client_id_conflict` carrying that id. After it expires,
    the same `client_id` makes a new message with a new id.
21. **A purged parent detaches only plaintext replies.** A plaintext reply's
    `reply_to` reads null once its parent expires. A sealed reply keeps the
    parent's id, because the hub shows `COALESCE(sealed_reply_to, reply_to)`
    and clears only `reply_to`.
22. **Expired mail has three 404 envelopes.** After expiry, `sent_lookup`
    answers `{"error": "message not found", "code": "not_found"}`, while
    `ack` and a plaintext `reply` answer the same text with no `code`.
23. **A question times out at its deadline, inclusive.** One second before,
    it is pending; at the deadline `question_status` reports `timeout`. A
    `question_desktop` answer after that is a 200 that changes nothing, an
    operator answer one second later is recorded in `late_answer_message_id`,
    and the timed-out question no longer counts toward the two-pending limit.
    After 90 days the row is deleted, so `question_status` answers
    `403 question_not_owned`. An operator answer at exactly the deadline,
    after a read has already timed the question out, matches neither branch
    of the hub's reply trigger: the question stays `timeout` with no answer
    and no `late_answer_message_id`.
24. **Some refusals have unexpected shapes.** A claim target over the byte
    limit is a 409 whose `error` is an object
    (`{"code": "invalid-field", "field": "target", "limit": 1024, ...}`) with
    `reason: "claim-target-too-long"`. A single event object, not an array,
    is read as a one-event batch and refused for its missing `node_id`. A
    missing and a malformed `node_id` on `/nodes` share one message. An
    envelope without `ct` and `recipients` is refused by the parser as
    `envelope_malformed`, before the hub's own complete-content check.
