# Worklore Ledger Specification

Date: 2026-08-29
Status: proposed for operator review

## Decision summary

Worklore is a durable, fleet-wide work ledger hosted by the Fleet Hub process. It accepts native work that has no repository, plus linked GitHub and Brigade records. Ops Deck becomes its main operator interface. Existing Fleet Hub claims and run events remain separate execution records.

Implementation keeps isolated modules until the integration precondition in the core plan passes: this branch is rebased onto a freshly fetched `origin/main`, `brigade fleet claims` shows no live conflicting owner for this checkout or the hub tree, and the operator has confirmed that the warned concurrent Fleet Hub work has landed. Worklore code stays additive. Isolated store, validation, HTTP, and client modules may land before that confirmation. Hub routing and the schema bump wait for it.

## Problem

The current Ops Deck sprint board reads `tracking/backlog.json`. The live file reported 20 tasks and a last-updated date of 2026-04-20 when inspected on 2026-08-29. It cannot represent current fleet work without manual edits.

The existing burn queue covers open GitHub issues carrying a `burn-queue` label. That excludes fleet maintenance, research, writing, administrative work, experiments, and ideas that do not belong in a repository. Brigade task ledgers hold detailed execution state, but each ledger belongs to one repository or workspace.

Worklore must answer 2 questions across all of these sources:

1. What useful work is available when prepaid model quota is about to reset?
2. What happened after an item was selected?

## Goals

- Store native work without requiring GitHub, Git, or Brigade.
- Present native, GitHub, and Brigade work in one queue.
- Keep scheduling metadata independent from normal business priority.
- Record an append-only sprint log for state changes and execution outcomes.
- Join a work item to Fleet Hub claims, runs, and verification evidence without copying those records.
- Give Ops Deck authenticated create, edit, defer, and archive actions.
- Preserve useful local work when Fleet Hub is unavailable.

## Non-goals

- Replace GitHub issues or Brigade task ledgers.
- Move Fleet Hub run journals into Worklore.
- Turn expiring Fleet Hub claims into durable tasks.
- Add automatic dispatch in the first release.
- Add a new daemon, database service, frontend framework, or runtime dependency.
- Store prompts, transcripts, credentials, private file paths, or arbitrary source payloads.
- Synchronize every GitHub issue or every Brigade task by default.

## Approaches considered

### A. Extend the existing Brigade task ledger

This reuses acceptance criteria, dependencies, seat class, and spend-by fields. It fails the fleet-wide requirement because ledgers are target-local and makes non-Brigade work depend on Brigade state.

### B. Put the ledger in Ops Deck API

This gives the UI a short path to CRUD operations. It makes a dashboard backend authoritative for work selected by multiple machines and leaves Fleet Hub execution joins dependent on another service.

### C. Add a Worklore module to Fleet Hub

This is the selected approach. It reuses the authenticated, fleet-reachable HTTP and SQLite process while keeping Worklore tables, validation, and routes separate from events and claims. Ops Deck API acts as a backend-for-frontend and holds no canonical Worklore records.

## Authority model

Worklore is authoritative for operator scheduling metadata:

- burn eligibility
- burn rank
- token appetite
- execution mode
- operator acceptance override (`work_items.acceptance_json`, API field `acceptance`)
- Worklore lifecycle state
- deferral reason and review date
- links between one work item and its external or execution records

Each external system remains authoritative for its own record:

| Source | External authority | Worklore authority |
| --- | --- | --- |
| Native | none | full record |
| GitHub | issue title, body-derived source acceptance, labels, open or closed state, URL, `source_policy` | scheduling fields, operator acceptance, Worklore state, execution links |
| Brigade | task text, source acceptance, dependencies, task completion, evidence references, `source_policy` | scheduling fields, operator acceptance, cross-source identity, sprint log |
| Fleet Hub | claims, run lifecycle, node identity, lease expiry | association of those records with a work item |

External sync never overwrites Worklore scheduling fields. That set includes operator `acceptance`, `burn_*`, `token_appetite`, `execution_mode`, Worklore `priority`, `status`, `blocker`, `review_after`, and `spend_by`.

Source acceptance lives on the link as `source_acceptance_json` (API field `source_acceptance`). Later observations refresh that link field. They do not write `work_items.acceptance_json`.

Effective acceptance is computed, not stored:

1. If operator `acceptance` is a non-empty array, use it.
2. Else use `source_acceptance` from the oldest link whose `source_policy` is `eligible`. That link is authoritative even when its `source_acceptance` is empty; a later eligible link is not consulted.
3. Else the effective list is empty.

`ready` and the burn queue use effective acceptance. A missing external record marks its link stale. It does not delete or complete the Worklore item.

## Lifecycle

The allowed Worklore states are:

```text
captured -> defining -> ready -> claimed -> running -> verifying -> completed
   |            |          |          |          |
   v            v          v          v          v
canceled     deferred   deferred   blocked    blocked
                |                     |
                +-----> ready <-------+

Any non-terminal state -> canceled
completed or canceled -> archived
```

`captured` may also go directly to `ready` or `canceled`. `defining` may go to `ready`, `deferred`, or `canceled`.

`claimed` in Worklore means the operator selected the item for an attempt. Fleet Hub's claim row remains the authority for whether a machine currently owns an execution target.

Every state change appends a `work_event`. The current state on `work_items` is a transactionally maintained projection used for fast queries. Events are never edited or deleted through the API.

## Burn eligibility

An item appears in the burn queue only when all conditions hold:

- `status = ready`
- `burn_eligible = true`
- effective acceptance has one to twenty criteria
- `execution_mode` is `agent` or `agent-with-review`
- no unresolved blocker is recorded
- `attempt_count < 2` unless an operator records `attempt-reset`
- `review_after` is absent or in the past
- no `work_links` row for the item has `source_policy` other than `eligible`

Normal priority and burn order are separate. The default burn ordering is:

1. smallest numeric `burn_rank`
2. earliest `spend_by`, with no deadline last
3. oldest `ready_at`
4. stable `work_id`

This preserves the existing oldest-first behavior when items share the default rank, while allowing the operator to order work without pretending it is a normal priority.

## Data model

Worklore uses 6 additive SQLite tables. All timestamps are UTC ISO 8601 strings. JSON fields contain bounded arrays or objects validated before storage. `worklore_store` issues `PRAGMA foreign_keys=ON` on every connection it touches. Fleet Hub `open_db` and `init_db` do not set that pragma today.

API field `acceptance` maps to column `acceptance_json`. API field `source_acceptance` maps to column `source_acceptance_json`. API field `detail` maps to column `detail_json`. Responses never expose the `_json` suffix.

### `work_items`

| Column | Type | Rules |
| --- | --- | --- |
| `work_id` | TEXT PRIMARY KEY | server-minted opaque ID |
| `title` | TEXT NOT NULL | 1 to 240 characters |
| `description` | TEXT | maximum 8,000 characters |
| `kind` | TEXT NOT NULL | `repo`, `fleet`, `research`, `writing`, `admin`, `experiment`, `idea`, `other` |
| `scope` | TEXT | operator label, maximum 128 characters |
| `status` | TEXT NOT NULL | allowed lifecycle state |
| `priority` | TEXT NOT NULL | `low`, `normal`, `high`, `urgent` |
| `burn_eligible` | INTEGER NOT NULL | boolean |
| `burn_rank` | INTEGER NOT NULL | 0 through 10,000, default 1,000 |
| `token_appetite` | TEXT NOT NULL | `small`, `medium`, `large`, `max` |
| `execution_mode` | TEXT NOT NULL | `manual`, `agent`, `agent-with-review` |
| `acceptance_json` | TEXT NOT NULL | operator override, JSON array, default `[]` |
| `blocker` | TEXT | maximum 2,000 characters |
| `review_after` | TEXT | optional ISO-8601 timestamp, validated on create and patch |
| `spend_by` | TEXT | optional ISO-8601 timestamp, validated on create and patch |
| `ready_at` | TEXT | set when entering ready |
| `attempt_count` | INTEGER NOT NULL | failed burn attempts, non-negative |
| `version` | INTEGER NOT NULL | optimistic concurrency token |
| `created_at` | TEXT NOT NULL | immutable |
| `updated_at` | TEXT NOT NULL | changes with projection |
| `archived_at` | TEXT | set only for archived items |

### `work_links`

| Column | Type | Rules |
| --- | --- | --- |
| `link_id` | TEXT PRIMARY KEY | server-minted opaque ID |
| `work_id` | TEXT NOT NULL | foreign key with delete restricted |
| `link_type` | TEXT NOT NULL | `github`, `brigade`, `fleet-run`, `fleet-claim`, `url` |
| `external_key` | TEXT NOT NULL | canonical provider identity |
| `display_ref` | TEXT | bounded safe label |
| `url` | TEXT | `https` URL only when present |
| `external_state` | TEXT | bounded provider state |
| `external_updated_at` | TEXT | provider timestamp |
| `source_policy` | TEXT NOT NULL | `eligible`, `label-removed`, `closed`, `completed`. Default `eligible` |
| `source_acceptance_json` | TEXT NOT NULL | last observed source acceptance, default `[]` |
| `synced_at` | TEXT NOT NULL | last successful observation, refreshed even when the observation is unchanged, or hub `received_at` for operator-created links |
| `stale_at` | TEXT | set when the source cannot be found |
| `adapter_id` | TEXT | adapter that first imported this identity, `NULL` for operator and fleet links |
| `owner_node` | TEXT | node that owned that adapter on the first import |
| `source_version` | TEXT | highest `external_updated_at` ever accepted for this identity. Internal; never in a response body |

`UNIQUE(link_type, external_key)` prevents 2 Worklore items from mirroring the same external record.

`source_version` is the rollback guard for import replay, and a usable revision is what buys
an adapter the right to write. Any observation that would change the projection, including
the one that creates the item, must carry an `external_updated_at` that parses as a
timestamp. Once a high-water exists the revision must also be later than it: an equal
revision carrying a different payload is refused, because two different payloads cannot both
be the state at one revision, and accepting the second is exactly how A -> expire -> B ->
replay A rolls back when a source stamps both observations with the same time. A refused
observation writes nothing at all, not even `synced_at`, and is reported in `refused` with a
named reason rather than folded into `unchanged`; "we kept the older projection" and
"nothing was different" are not the same answer. Timestamps compare as instants, so a
`-05:00` offset and a `Z` value order correctly rather than lexicographically. The guard
costs one nullable column on a row that already exists, so it is bounded by the imported
identities themselves and adds no table an adapter can grow.

Identities imported before the column existed are seeded once, at migration, from their
stored `external_updated_at` when it parses. That value is the revision of the projection
those rows currently hold, which is what the guard compares against, so seeding it closes
the window in which a just-migrated identity had no guard at all and could be replayed
straight back. Unparseable values seed nothing.

Requiring the revision on creation is what makes the guard total. An identity minted without
one would carry no high-water, so the hub could not later tell a fresh observation from a
replay of the one that minted it, and that identity would sit in the ledger permanently
rollback-able. Refusing it instead means there is no projection in the ledger that a replay
can roll back, and it costs an operator nothing they cannot see: the refusal names
`source-revision-missing` per identity, and the valid siblings in the same batch still
import.

The one case a missing revision survives is an observation that changes nothing. It writes
no projection, so there is no state for a revision to protect, and refusing every re-poll of
a settled identity would be noise rather than a rollback worth reporting. After this change
that case is reachable only for a link migrated from before the column existed whose
`external_updated_at` was also NULL, since nothing else can enter the ledger unproven.

One item carries at most 200 links of all types combined. Operator links, adapter imports, and fleet-run links claim from that one ceiling inside the writing transaction, so no caller can grow a single item's link set without limit and make every read that touches it expensive. Past the ceiling a link route returns 409 `link-conflict` and an import returns 409 `import-conflict`; no existing link is disturbed.

The ceiling binds new writes only. A database written before it existed may hold an item over it, and that item is served rather than refused: every read path is cut by SQL, and an operator repairs the item by unlinking through the ordinary route on a running hub.

`fleet-run` links are the one class ordinary execution grows on its own, so each item retains its most recent 50 of them and older ones expire when a new run is linked. Retention runs before the ceiling claim, so a busy item always has room for its next run instead of freezing at 200 and refusing every future one. It writes no `unlinked` event: this is storage policy, not an operator decision, and an event per expired run would only move the unbounded growth into the event log.

Reads never load a whole link set to decide anything. Burn eligibility and effective acceptance come from a per-item SQL summary: the first non-eligible `source_policy` and the `source_acceptance_json` of the oldest eligible imported `github` or `brigade` link, read with `ORDER BY synced_at, link_id LIMIT 1`. That source link is the authority whenever operator acceptance is empty, including when its acceptance list is empty. A list page projects at most 50 links per item and sets `links_truncated` when it cut the set. Item detail returns one page of links with `links_next_cursor`. The full set is walked through that cursor.

The per-item cut happens in SQL, not in Python. The list projection runs one indexed `WHERE work_id = ? ORDER BY synced_at, link_id LIMIT n + 1` query for each listed item. It returns at most 51 rows when `n` is 50, and the extra row is the only evidence needed to set `links_truncated`. This does not depend on the 200-link write ceiling holding: a database written before that ceiling existed can hold an item with far more links, and the page still costs its own budget rather than the item's history.

GitHub keys use `owner/repository#number`. Brigade keys use a canonical repository identity plus the ledger task ID. Fleet run keys use `node_id/run_id`.

`fleet-claim` is reserved. No first-release route creates it. Do not add a create path in any slice.

`fleet-run` is created only by `POST /work/items/{work_id}/execution`.

### `work_events`

| Column | Type | Rules |
| --- | --- | --- |
| `work_id` | TEXT NOT NULL | foreign key with delete restricted |
| `event_id` | TEXT NOT NULL | client key scoped to this item, or server-minted ID |
| `event_type` | TEXT NOT NULL | bounded allowlist |
| `from_status` | TEXT | required for transitions after creation |
| `to_status` | TEXT | required for transitions |
| `actor_type` | TEXT NOT NULL | `operator`, `node`, `adapter`, `system` |
| `actor_id` | TEXT | authenticated or configured identity |
| `node_id` | TEXT | authenticated node when applicable |
| `run_id` | TEXT | associated Fleet Hub run when applicable |
| `detail_json` | TEXT NOT NULL | bounded, event-specific safe fields |
| `occurred_at` | TEXT NOT NULL | source timestamp |
| `received_at` | TEXT NOT NULL | hub timestamp |
| `seq` | INTEGER NOT NULL | hub-assigned insertion sequence. Server-internal ordering key, never returned in an event body |

Primary key is `(work_id, event_id)`. A client-supplied `event_id` matching `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$` is stored as given and is unique only inside that `work_id`. Two items may reuse the same client key. Two events on one item may not. When the client omits the key, the server mints `evt-` plus 24 hex.

Adapter observation events are the one event class an external source can grow without limit, so each item retains only its most recent 200 `sync-observed` and `sync-stale` events written with `actor_type=adapter`. The trim runs in the import transaction for the item that was just observed. Lifecycle events are never trimmed: `created`, `linked`, `unlinked`, `transitioned`, `execution-linked`, and every `attempt-*` event survive, as does any event an operator or node wrote. Adapter ownership is read from `work_links`, and the backfill that seeds it reads the `created` and `linked` events, so trimming observations never loses an ownership claim.

`seq` is a monotonic counter assigned inside the writing transaction. It, not `occurred_at`, defines causal order, so an import batch that writes many events under one timestamp still reads back in the order it was written. Per-item event pages are ordered by `seq` ascending and their cursor carries only `seq`. The fleet-wide log stays reverse-chronological on `occurred_at` and breaks ties on `seq` descending, so its cursor carries `(occurred_at, seq)`. Cursors are opaque; `seq` is not part of any event response body.

Required event types in the first release are `created`, `updated`, `transitioned`, `linked`, `unlinked`, `sync-observed`, `sync-stale`, `attempt-started`, `attempt-failed`, `attempt-reset`, and `execution-linked`.

### `work_sync_cursors`

| Column | Type | Rules |
| --- | --- | --- |
| `adapter_id` | TEXT PRIMARY KEY | configured source identity |
| `source_type` | TEXT NOT NULL | `github` or `brigade` |
| `owner_node` | TEXT NOT NULL | node id recorded on first successful import |
| `cursor` | TEXT | opaque adapter cursor or content fingerprint |
| `last_success_at` | TEXT | successful batch timestamp |
| `last_error_code` | TEXT | bounded stable code, no raw error body |
| `updated_at` | TEXT NOT NULL | hub timestamp |

`adapter_id` must be its `source_type` followed by `:` and a bounded identifier (`github:escoffier-labs`, `brigade:rocinante`), so one source can never claim another's namespace. The first import that carries at least one observation records `owner_node` from the authenticated node (or `admin` when the admin token posts). An empty batch for an unclaimed `adapter_id` writes nothing at all, so it cannot reserve the namespace. Later imports from a different node return 403 `adapter-owner-mismatch` and do not write observations or the cursor.

Importing is an operator-node authority, not an ordinary node one: `POST /work/imports` requires the admin token or a node listed in `fleet.worklore.operator_nodes`, so an enrolled but unlisted node cannot claim an adapter namespace at all. `admin` is a reserved node id for the same reason. It is the actor id and import owner the hub writes for the admin token, so a node presenting it would author events and own namespaces indistinguishable from the admin's. A node actor sending it is refused with 403 `forbidden`, and it is never accepted as a configured operator node.

An external identity is `(work_id, link_type, external_key)`, never the external key alone. One item may carry the same key under two link types, an operator `url` link and an adapter-owned `github` link, say, and those are two separate identities: reading the key alone would let an adapter reach across types to claim an operator link, and let an eligible adapter link block the operator from deleting their own url.

### `work_import_keys`

| Column | Type | Rules |
| --- | --- | --- |
| `adapter_id` | TEXT NOT NULL | configured source identity |
| `idempotency_key` | TEXT NOT NULL | client batch key, max 256 characters |
| `fingerprint` | TEXT NOT NULL | SHA-256 hex of the canonical observation payload |
| `created` | INTEGER NOT NULL | created count from the first accepted apply |
| `updated` | INTEGER NOT NULL | updated count from the first accepted apply |
| `unchanged` | INTEGER NOT NULL | unchanged count from the first accepted apply |
| `refused` | INTEGER NOT NULL | per-identity refusals from the first accepted apply |
| `received_at` | TEXT NOT NULL | hub timestamp of first accept |
| `owner_node` | TEXT | node that owned the adapter on first accept, backfilled from `work_sync_cursors` |

Primary key is `(adapter_id, idempotency_key)`. A replay with the same fingerprint returns the stored `created`, `updated`, `unchanged`, and `refused` counts from the first accepted apply. Refusal reasons are not stored, so a replay returns an empty `refused_details` array because it did not take those decisions. It recomputes `items` from current `work_links` rows matching the observation `external_key`s, updates `work_sync_cursors.last_success_at` and `updated_at`, and writes no observations or events. A replay with a different fingerprint returns 409 `import-conflict`. Keys survive later cursor advances.

Keys are retained, not kept forever. Each accepted import expires keys past the most recent 500 for its adapter and the most recent 5,000 for its owning node, in the same transaction, indexed on `(adapter_id, received_at)` and `(owner_node, received_at)`. A replay inside the window returns its stored counts exactly. This is what keeps an adapter that posts an unchanged batch under a new key every minute from growing import-key storage without limit.

A replay of an expired key is not short-circuited, because the key that proved it was a replay is gone. It is re-applied as a fresh batch, and `work_links.source_version` is what makes that safe: every observation in it is held against the high-water of its own external identity, so an expired batch carrying older observations than what a later batch already projected is refused per identity and reported in `refused`. It cannot roll the projection back to the state it captured. A re-applied batch whose observations are still the newest ones is applied and finds nothing to change, and reports the counts that apply produced, which may differ from the counts the original apply reported. A replayed observation that carries no usable revision is refused for the same reason, and so is the observation that would have created the identity in the first place, so there is no identity anywhere in the ledger for which replay is still last-write-wins. Both first-party adapters supply the revision when the source does. The GitHub adapter reports `updatedAt` and the Brigade adapter reports the task's `updated_at`, each emitted only when the hub can parse it as an instant. Neither fabricates one: a source record that reports no revision is passed through and refused by the hub, where the refusal is counted and named, rather than dropped in the adapter where nothing would report it.

### `work_create_keys`

| Column | Type | Rules |
| --- | --- | --- |
| `actor_id` | TEXT NOT NULL | operator identity (`admin` or the authenticated node id) |
| `idempotency_key` | TEXT NOT NULL | `Idempotency-Key` header, max 256 characters |
| `fingerprint` | TEXT NOT NULL | SHA-256 hex of the canonical native create payload |
| `work_id` | TEXT NOT NULL | the item created on first accept |
| `received_at` | TEXT NOT NULL | hub timestamp of first accept |

Primary key is `(actor_id, idempotency_key)`. A replay with the same fingerprint returns the original item and writes no second `created` event. A replay with a different fingerprint returns 409 `import-conflict`. Keys are scoped per operator identity, so two operators may reuse the same key.

Create keys are kept permanently, and that is deliberate. A row is written only by a create that actually minted an item, so the table holds exactly one identity row per durable work item that was created with an `Idempotency-Key`, and never more: replays return early and write nothing, and a create without the header writes no row at all. `work_id` is a foreign key with delete restricted, so a row cannot outlive its item or be quietly detached from it. That is the bound. There is no sweep, and there must not be one: unlike an import key, which a re-apply can safely reconstruct, expiring a create key would let a client retrying a create that already succeeded mint a second item. Permanent create idempotency is worth one bounded row per item.

## Import observation schema

The import envelope accepts only `adapter_id`, `source_type`, `idempotency_key`, and `observations`. Any other envelope key is 400 `unknown-field`. Each object in `observations` accepts only the keys in this table. Any other observation key is 400 `unknown-field`. Adapters drop provider-only fields such as `body` and `labels` after they derive `source_policy` and `acceptance`.

| Key | Required | Stored on | Later observation |
| --- | --- | --- | --- |
| `external_key` | yes | `work_links.external_key` | identity, never rewritten |
| `link_type` | yes | `work_links.link_type` | must stay `github` or `brigade` |
| `title` | yes | `work_items.title` | refreshed. Source-authoritative |
| `source_policy` | yes | `work_links.source_policy` | refreshed |
| `description` | no | `work_items.description` on first create only | dropped |
| `acceptance` | no | `work_links.source_acceptance_json` | refreshed. Never writes operator `acceptance` |
| `external_state` | no | `work_links.external_state` | refreshed |
| `external_updated_at` | no | `work_links.external_updated_at` | refreshed |
| `url` | no | `work_links.url` | refreshed. `https` only |
| `display_ref` | no | `work_links.display_ref` | refreshed |
| `proposed_status` | no | `work_events.detail_json` on `sync-observed` | stored only. Never applied |
| `priority` | no | `work_events.detail_json` on `sync-observed` | source metadata only. Never writes Worklore `priority` |
| `dependencies` | no | `work_events.detail_json` on `sync-observed` | stored only |
| `evidence_refs` | no | `work_events.detail_json` on `sync-observed` | stored only |
| `stale` | no | `work_links.stale_at` when true | appends `sync-stale` |

`source_policy` values: `eligible`, `label-removed`, `closed`, `completed`.

`proposed_status` values when present: `completed`.

`dependencies` items are `{ "type": "...", "id": "..." }` with both strings bounded to 128 characters.

`evidence_refs` items match `^[A-Za-z0-9._-]{1,128}$`.

## HTTP contract

All responses are JSON unless noted. Unknown request fields are rejected. Request bodies use the existing Fleet Hub size limit, with Worklore endpoints applying smaller per-field bounds. Mutation bodies max 262144 bytes. Import bodies max 1048576 bytes.

Error envelope:

```json
{ "error": "bounded operator text", "code": "stable-code" }
```

Stable codes: `unauthorized`, `forbidden`, `not-found`, `version-conflict`, `if-match-required`, `invalid-transition`, `unknown-field`, `field-bound`, `private-data`, `acceptance-required`, `import-conflict`, `adapter-owner-mismatch`, `execution-mismatch`, `attempt-forbidden`, `link-forbidden`, `link-conflict`, `hub-unavailable`, `internal-error`.

- 400 `private-data`: a durable string carried a private home path (`/home`, `/Users`, a Windows user profile) or a credential-shaped value, including URL userinfo. Bounds, types, and control characters stay 400 `field-bound`.

- 403 `link-forbidden`: the link type is fleet managed (`fleet-run`, `fleet-claim`), or the link is adapter-owned and its `source_policy` is still `eligible`.
- 409 `link-conflict`: that `(link_type, external_key)` already belongs to a work item, including a `fleet-run` already joined to a different item.
- 500 `internal-error`: an unexpected SQLite failure. The `error` string is the fixed text `internal error`; SQLite messages are never echoed to the caller.

`If-Match` is the decimal integer `version` as a string (`1`, `2`, ...). It is required on `PATCH /work/items/{work_id}`, `POST /work/items/{work_id}/transitions`, and `POST /work/items/{work_id}/attempts`. It is optional on `DELETE /work/items/{work_id}/links/{link_id}`. A supplied stale value on any of those routes returns 409 `version-conflict`; a missing required header returns 400 `if-match-required`.

Configured operator nodes and the admin token may mutate Worklore routes without `--allow-admin-writes`. That flag stays scoped to `POST /events` and `POST /claims` (`src/brigade/fleet_hub_http.py:575-584`). This is a deliberate Worklore exception. The admin token remains a backwards-compatible override. Normal Ops Deck and CLI work uses an enrolled node token listed in `fleet.worklore.operator_nodes` (optional bounded override: `BRIGADE_WORKLORE_OPERATOR_NODES`). Do not require or deploy the shared Hub admin token for Ops Deck.

The read-only dashboard cookie does not authorize JSON routes. `_caller` in `src/brigade/fleet_hub_http.py:155-173` is bearer-only.

### Read routes

- `GET /work/items`: admin, operator node, or ordinary node token. Query: `status`, `kind`, `burn_eligible`, `source` (`github` / `brigade` / `native`), `limit` (1-100, default 50), `cursor`.
- `GET /work/items/{work_id}`: admin, operator node, or ordinary node token. Item, one page of links, and up to 20 recent events, all cut by the database rather than read whole and sliced. Query: `links_limit` (1-100, default 50), `links_cursor`.
- `GET /work/items/{work_id}/events`: admin, operator node, or ordinary node token. Cursor-paginated events for one item in hub insertion order.
- `GET /work/events`: admin, operator node, or ordinary node token. Fleet-wide reverse-chronological sprint log. Query: `limit` (1-100, default 50), `cursor`, optional `work_id`, optional `event_type`.
- `GET /work/queue/burn`: admin, operator node, or ordinary node token. Eligible items in deterministic order plus exclusion counts. Query: `limit` (1-100, default 50), `cursor`.

`GET /work/queue/burn` is paged the same way. One read returns at most `limit` eligible items and examines at most 2,000 item rows in burn order, so a ledger that has grown without limit still answers in bounded time. `exclusions` counts the rows that read examined; a caller that wants whole-ledger totals follows `next_cursor` to the end and sums them, and a ledger smaller than one page reports them in a single read. A page loads links only for the item ids on that page.

`GET /work/items` is paged. A reconciling adapter must follow `next_cursor` to the end of the listing, not read only the first page, or it cannot see label removal and staleness past the first `limit` items. `source=github` or `source=brigade` returns items that have at least one link of that type. `source=native` returns items with no `github` or `brigade` link. An unfiltered page reads directly from the item keyset. A filtered page examines at most 2,000 candidates in that same keyset order, and its source check is one indexed probe per candidate. If that budget ends before the page fills, the response can contain fewer than `limit` items but still carries the cursor of the last scanned row; callers continue from that cursor rather than treating an empty page as terminal. List items always include a `links` array so adapters can read `external_key` and `source_policy`, bounded to the first 50 links per item with `links_truncated` set when the set was cut. An adapter that sees `links_truncated` reads the rest through `GET /work/items/{work_id}` and its `links_next_cursor`.

`GET /work/events` uses the same rule whenever `work_id` or `event_type` is supplied: it examines at most 2,000 reverse-chronological candidates and advances the cursor through the last scanned event, including on a sparse or empty filtered page. The unfiltered sprint log remains a direct indexed keyset page.

`GET /work/items/{work_id}` returns links keyed on `(synced_at, link_id)` with `links_next_cursor`. A caller that follows that cursor to the end sees every link on the item exactly once, and one read costs a page rather than the item's whole link set.

### Mutation routes

- `POST /work/items`: admin token or configured operator node token. Creates a native item and its `created` event in one transaction. Optional `Idempotency-Key` (1-256 characters) is keyed by operator identity. Same key and same payload returns the original item with no second event. Same key and a different payload returns 409 `import-conflict`.
- `PATCH /work/items/{work_id}`: admin token or configured operator node token. Requires `If-Match`.
- `POST /work/items/{work_id}/transitions`: admin token or configured operator node token. Requires `If-Match`. Validates the lifecycle edge and appends the event in one transaction.
- `POST /work/items/{work_id}/attempts`: body `{ "action", "run_id"? }` with `action` in `{started, failed, reset}`. Requires `If-Match`. `node_id` is the authenticated caller, not a body field. `reset` is admin or operator-node and sets `attempt_count` to 0. `started` appends `attempt-started`, increments `version`, and does not change `attempt_count`. `failed` increments `attempt_count` and `version` and appends `attempt-failed`. A node may send `started` or `failed` only when `run_id` names a `fleet-run` link whose `external_key` is `{node_id}/{run_id}`. Otherwise 403 `attempt-forbidden`.
- `POST /work/items/{work_id}/links`: admin token or configured operator node token. Operator-managed `url`, `github`, or `brigade` links. Sets `synced_at` to hub `received_at`. Rejects `fleet-run` and `fleet-claim`.
- `DELETE /work/items/{work_id}/links/{link_id}`: admin token or configured operator node token. Optional `If-Match` guards against a stale item version and returns 409 `version-conflict` when it does not match. Removes a link and records exactly one `unlinked` event. Work items are never deleted, and no scheduling field (`burn_rank`, `review_after`, `spend_by`, `ready_at`, `attempt_count`) changes. An adapter-owned `github` or `brigade` link may be removed only once its `source_policy` has left `eligible`; an eligible adapter link is 403 `link-forbidden`. Deleting the last non-eligible source link is how an operator converts an item to native, and that event's detail carries `became_native: true`.
- `POST /work/imports`: admin token or configured operator node token. Body `{adapter_id, source_type, idempotency_key, observations}` with up to 500 observations. Each observation's `link_type` must equal the batch `source_type`. Returns `created`, `updated`, `unchanged`, `refused`, up to 20 `refused_details` (`external_key`, `link_type`, `reason`), and the observed items. A refusal is per identity and never aborts the batch: an identity an operator already linked by hand has no adapter owner and is refused `operator-managed-identity` while its valid siblings import, and an observation that cannot prove it is newer than the projection is refused `source-revision-stale`, `source-revision-missing`, or `source-revision-not-advanced`. There is still no adapter adoption of operator-managed links. A conflict that is about the batch rather than one identity, an identity owned by a different adapter or a different node, still returns 409 `import-conflict` or 403 `adapter-owner-mismatch`. A replay inside the key window returns the stored counts and an empty `refused_details` array because refusal reasons are not stored.
- `POST /work/items/{work_id}/execution`: node token or admin token. Body `{ "node_id", "run_id" }`. The hub verifies that the run exists in Fleet Hub `events` for that node. A node may link only its own `node_id`. An admin may link any existing `(node_id, run_id)` so a mislinked run can be repaired. The hub decides that from the authenticated caller's admin flag, never from a reserved `actor_id` string, and the `execution-linked` event uses the same `actor_type` rule as every other route.

An authenticated node is marked operator only when its `node_id` is in `fleet.worklore.operator_nodes` or the bounded `BRIGADE_WORKLORE_OPERATOR_NODES` override (comma-separated, max 32 ids, each matching `[A-Za-z0-9._-]{1,128}`, never `unknown` and never the reserved `admin`). Operator-node audit events keep that `node_id` and use `actor_type=operator`. Admin-token events keep `actor_id=admin` and `node_id` unset.

### Request and response shapes

Create body:

```json
{
  "title": "Rotate NAS restic password",
  "kind": "fleet",
  "description": "",
  "scope": "",
  "priority": "normal",
  "burn_eligible": false,
  "burn_rank": 1000,
  "token_appetite": "medium",
  "execution_mode": "manual",
  "acceptance": [],
  "blocker": null,
  "review_after": null,
  "spend_by": null
}
```

Only `title` and `kind` are required. Other keys use the create defaults.

Patch body: any create-body field except `kind`. Unknown keys are rejected.

Item object:

```json
{
  "work_id": "wl-0123456789abcdef01234567",
  "title": "Rotate NAS restic password",
  "description": "",
  "kind": "fleet",
  "scope": null,
  "status": "captured",
  "priority": "normal",
  "burn_eligible": false,
  "burn_rank": 1000,
  "token_appetite": "medium",
  "execution_mode": "manual",
  "acceptance": [],
  "effective_acceptance": [],
  "blocker": null,
  "review_after": null,
  "spend_by": null,
  "ready_at": null,
  "attempt_count": 0,
  "version": 1,
  "created_at": "2026-08-29T00:00:00+00:00",
  "updated_at": "2026-08-29T00:00:00+00:00",
  "archived_at": null
}
```

Link object:

```json
{
  "link_id": "lnk-0123456789abcdef01234567",
  "work_id": "wl-0123456789abcdef01234567",
  "link_type": "github",
  "external_key": "escoffier-labs/brigade#1",
  "display_ref": "escoffier-labs/brigade#1",
  "url": "https://github.com/escoffier-labs/brigade/issues/1",
  "external_state": "open",
  "external_updated_at": "2026-08-29T00:00:00+00:00",
  "source_policy": "eligible",
  "source_acceptance": ["tests pass"],
  "synced_at": "2026-08-29T00:00:00+00:00",
  "stale_at": null
}
```

Event object:

```json
{
  "event_id": "evt-0123456789abcdef01234567",
  "work_id": "wl-0123456789abcdef01234567",
  "event_type": "created",
  "from_status": null,
  "to_status": "captured",
  "actor_type": "operator",
  "actor_id": "admin",
  "node_id": null,
  "run_id": null,
  "detail": {},
  "occurred_at": "2026-08-29T00:00:00+00:00",
  "received_at": "2026-08-29T00:00:00+00:00"
}
```

`GET /work/items` 200: `{ "items": [ { ...Item, "links": [Link], "links_truncated": false } ], "next_cursor": null }`.

`GET /work/items/{work_id}` 200: `{ "item": Item, "links": [Link], "links_next_cursor": null, "recent_events": [Event] }`.

`GET /work/items/{work_id}/events` and `GET /work/events` 200: `{ "events": [Event], "next_cursor": null }`.

`GET /work/queue/burn` 200:

```json
{
  "items": [Item],
  "next_cursor": null,
  "exclusions": {
    "acceptance-required": 0,
    "not-ready": 0,
    "not-eligible": 0,
    "manual-mode": 0,
    "blocker": 0,
    "attempt-limit": 0,
    "review-after": 0,
    "source-policy": 0
  }
}
```

An item is counted in exactly one exclusion bucket. Check in this order and stop at the first failure:

1. `not-ready` when `status` is not `ready`
2. `not-eligible` when `burn_eligible` is false
3. `acceptance-required` when effective acceptance is empty or longer than twenty criteria
4. `manual-mode` when `execution_mode` is `manual`
5. `blocker` when an unresolved blocker is recorded
6. `attempt-limit` when `attempt_count >= 2`
7. `review-after` when `review_after` is in the future
8. `source-policy` when any link has `source_policy` other than `eligible`

`POST /work/items` 201: `{ "item": Item }`.

`PATCH` and transition and attempt 200: `{ "item": Item }`.

`POST /work/items/{work_id}/attempts` body:

```json
{ "action": "failed", "run_id": "run-1" }
```

`action` is required. `run_id` is required when the caller is a node and omitted for admin `reset`. Admin `started` and `failed` may omit `run_id`.

`POST /work/items/{work_id}/links` 201: `{ "link": Link }`.

`DELETE .../links/{link_id}` 200: `{ "ok": true }`.

`POST /work/items/{work_id}/execution` 201: `{ "link": Link }` with `link_type=fleet-run`.

`POST /work/imports` body:

```json
{
  "adapter_id": "github:escoffier-labs",
  "source_type": "github",
  "idempotency_key": "github:escoffier-labs:0123456789abcdef01234567",
  "observations": [
    {
      "external_key": "escoffier-labs/brigade#1",
      "link_type": "github",
      "title": "Burn queue item",
      "source_policy": "eligible",
      "description": "Do the work",
      "acceptance": ["tests pass"],
      "external_state": "open",
      "external_updated_at": "2026-08-29T00:00:00+00:00",
      "url": "https://github.com/escoffier-labs/brigade/issues/1",
      "display_ref": "escoffier-labs/brigade#1",
      "proposed_status": null
    }
  ]
}
```

`POST /work/imports` 200: `{ "created": 1, "updated": 0, "unchanged": 0, "refused": 0, "refused_details": [], "items": [ { "work_id": "wl-...", "external_key": "escoffier-labs/brigade#1" } ] }`. A same-fingerprint replay returns the stored counts and an empty `refused_details` array, recomputes `items` as defined on `work_import_keys`, and still advances the cursor timestamps.

Ops Deck API uses a server-held admin credential for browser mutations and never sends that credential to the browser. Every `/api/worklore` route, including GET, requires `X-API-Key`.

## Source adapters

Adapters normalize records and send idempotent batches to `POST /work/imports`. They do not write the Fleet Hub database directly. `import_batch` and `list_items` live in the core client only.

### Native adapter

Ops Deck creates native items through its API proxy. Native items can later gain GitHub, Brigade, or Fleet execution links without changing identity.

### GitHub adapter

The adapter discovers issues labeled `burn-queue` from configured organizations using the approved GitHub read path. It sends the issue identity, title, body-derived source acceptance, open or closed state, URL, source timestamp, and `source_policy`. GitHub credentials stay on the node running the adapter.

The first observation creates a Worklore item with default scheduling metadata and writes source acceptance on the link. Later observations update only externally authoritative fields and the link snapshot, including `source_acceptance` and `source_policy`. Removing the label marks `source_policy=label-removed`. Closing an issue marks `source_policy=closed` and may send `proposed_status=completed`. The adapter never POSTs a transition. A 404 received through octopool is inconclusive and does not set `stale_at`. A 404 received through direct `gh` sets `stale_at` and appends `sync-stale`.

### Brigade adapter

The adapter reads selected local `.brigade/work/tasks.json` ledgers through Brigade's public task command and sends normalized task identity, text, source acceptance, dependencies, source priority, status, and evidence references. It never sends absolute paths or raw receipt contents.

Selection is explicit by configured target. Worklore does not crawl a home directory for `.brigade` folders. A completed Brigade task records `source_policy=completed` and may send `proposed_status=completed`. The Worklore transition follows the same operator rule as GitHub.

## Ops Deck integration

Ops Deck API adds a narrow proxy under `/api/worklore`. Every method requires `X-API-Key`.

- `GET /api/worklore/items`
- `GET /api/worklore/items/{work_id}`
- `GET /api/worklore/items/{work_id}/events`
- `GET /api/worklore/events`
- `GET /api/worklore/queue/burn`
- `POST /api/worklore/items`
- `PATCH /api/worklore/items/{work_id}`
- `POST /api/worklore/items/{work_id}/transitions`
- `POST /api/worklore/items/{work_id}/attempts`

The API validates browser input before forwarding it and maps upstream connection failures to 503 with a stable error code. It does not cache mutations. Read requests may return a clearly marked last-known-good snapshot when Fleet Hub is unavailable. The snapshot file mode is `0600`.

The existing Tasks page gains 3 views:

- `Burn Queue`: eligible items, exclusion summary, token appetite, attempt count, and source.
- `Work Board`: columns driven by Worklore status.
- `Sprint Log`: `GET /api/worklore/events`, reverse-chronological, with item, transition, actor, run, and outcome links.

The stale `tracking/backlog.json` route remains available during migration. It is removed only after native records are imported and the Worklore view has passed acceptance checks.

## Failure and concurrency behavior

- Fleet Hub downtime never blocks local GitHub, Brigade, or manual work. It blocks new canonical native mutations until the service returns.
- Adapter batches are idempotent through `work_import_keys`. A failed batch can be retried without duplicate items, links, or events, including after a newer batch has advanced the cursor. Once the key has expired, the retry is re-applied rather than short-circuited, and `work_links.source_version` holds it to the per-identity high-water so it cannot roll a newer projection back.
- Adapters retain a local retry receipt, not a copy of the Worklore database. The first release uses explicit retry rather than a background daemon. Receipt filenames replace `:`, `/`, `\`, `<`, `>`, `"`, `|`, `?`, and `*` with `-`. File mode is `0600`.
- Each item mutation and its event append share one SQLite transaction.
- Optimistic version checks prevent 2 Ops Deck sessions from silently overwriting each other.
- Import conflict resolution follows the authority table. Conflicts produce a stable error or a `sync-observed` event. They never select a winner by arrival time.
- Newer database versions fail closed under the existing Fleet Hub migration policy.
- Worklore tables use foreign keys and indexes for status, burn eligibility, ready time, event work ID, external identity, adapter ownership, and burn order.
- Imported identities are bounded per adapter (20,000 links) and per owning node (100,000 links). Past a ceiling an import refuses a new identity with 409 `import-conflict` and still accepts updates to work the adapter already imported.
- Links are bounded per item (200 across all types), import keys are retained per adapter (500) and per owning node (5,000), and adapter observation events are retained per item (200). Native create keys are the one deliberate exception: they are permanent, at exactly one row per durable item, because expiring one would permit a duplicate create. No adapter, operator, or node can grow Worklore storage or read cost without limit, and no bound is reachable by ordinary use: a normal item carries 1 to 3 links and a normal adapter posts one batch per sync.
- Migration holds existing rows to those ceilings rather than assuming they were written under them, and pays for each reconciliation according to what it costs. Schema setup prunes import keys and adapter observation events to their retention windows across every adapter, owner, and item, including partitions no recent import touched; that pass stays on every start, because an adapter that goes quiet leaves rows nothing else revisits, but it first names the over-quota partitions with an indexed aggregate, so a healthy hub finds none and writes nothing. Seeding `source_version` on identities imported before the column existed reads every imported link, so it runs once: `work_schema_meta` records the reconciliation version a database has completed and a hub that has done the work skips past it.
- Links are not pruned. Each one is a distinct external identity, and dropping some would detach real imported work and free the identity for another adapter to claim. Nor does a database already over the 200-link ceiling refuse to start. Reads of an over-ceiling item are cut by SQL rather than by trusting the ceiling, new writes still enforce it, and the operator unlinks the excess through the ordinary route on a running hub. Refusing at startup instead would take the hub down and leave no running route to repair it with.
- Every durable native text field (`title`, `description`, `scope`, each `acceptance` item, `blocker`) runs through the same private-path and credential checks as an imported observation, on create and on patch. A leak returns 422 `private-data` and nothing is written.
- One GitHub sync run spends at most 200 issue refreshes and 300 seconds of reconciliation across every configured organization. Links left unrefreshed are reported in `skipped` and picked up by the next run.

## Privacy and security

- Admin credentials remain on trusted servers for node enrollment. They are not required for Ops Deck or `brigade fleet work` mutations.
- Ordinary node tokens may read the ledger and link their own runs. They cannot import, edit scheduling metadata, transition work, or reset attempt counts.
- Configured operator nodes use the same enrolled node token. They may import adapter batches, create, patch, transition, reset attempts, and manage operator links. Audit events from those nodes keep the node identity and use `actor_type=operator`. Adapter sync hosts must therefore be listed in `fleet.worklore.operator_nodes`.
- Titles and descriptions are treated as untrusted text when rendered.
- One canonical observation validator runs before every imported create and update, so a link created by a safe batch cannot be mutated by a later unsafe one. It rejects control characters, credential-shaped values, private home paths, malformed URLs and URL userinfo, over-long acceptance strings, and over-long acceptance, dependency, and evidence-reference lists, as well as raw prompts, transcripts, and receipt bodies.
- URLs must use `https` and are rendered with escaping and safe link attributes.
- API errors contain stable codes and bounded descriptions. They do not return SQLite errors, tokens, filesystem paths, or upstream response bodies.
- SQLite exports omit private actor details unless the authenticated admin explicitly requests them.

## Delivery slices

### Slice 1: Native ledger and API

Add isolated Worklore store, validation, migrations, native CRUD, transitions, attempts, event history, burn selection, `import_batch`, `list_items`, and CLI/API contract tests. Integrate with Fleet Hub routing only after the integration precondition passes.

### Slice 2: GitHub adapter

Add explicit synchronization of configured `burn-queue` issues, external identity dedupe, stale-link behavior, and source-policy exclusions. This slice imports core client functions. It does not add them.

### Slice 3: Brigade adapter

Add explicit target configuration, normalized ledger observations, dependency and evidence references, and source-state proposals. This slice imports core client functions. It does not add them.

### Slice 4: Ops Deck

Add the authenticated API proxy, Burn Queue, Work Board, Sprint Log, native item editor, stale-read behavior, and migration from the legacy JSON backlog.

Each slice has its own implementation plan and can ship behind a disabled-by-default route or UI flag. No slice requires automatic dispatch.

## Acceptance criteria

- A native fleet-maintenance item can be created without a repository and appears in the burn queue after it becomes ready.
- The same GitHub issue observed twice creates one item, one link, and no duplicate observation event for an identical source revision.
- The same Brigade task observed from 2 nodes resolves to one external link.
- Worklore scheduling edits and operator acceptance survive later external synchronization.
- An item without effective acceptance criteria cannot enter `ready` or the burn queue.
- A second failed burn attempt excludes the item until an operator records `attempt-reset` through `POST /work/items/{work_id}/attempts`.
- Concurrent edits with the same version yield one success and one 409 conflict. A missing `If-Match` on PATCH, transition, or attempt yields 400 `if-match-required`.
- A node cannot link another node's run to an item. An admin can repair a mislinked run.
- Fleet Hub unavailability leaves local source work usable and produces an explicit retryable adapter failure.
- Ops Deck can display a last-known-good read snapshot but cannot report a stale mutation as successful. Unauthenticated `/api/worklore` reads fail.
- Existing Fleet Hub events, claims, cloud leases, model leases, dashboards, and CLI behavior pass unchanged.

## Pressure-test decisions

The following calls were made in sous mode after the operator authorized all 3 sources.

- `evidence`: Host Worklore in Fleet Hub because the process already provides authenticated fleet access, SQLite WAL storage, schema migrations, and node identity.
- `judgment`: Keep Worklore in separate modules and tables because planning records have a different retention and authority model from expiring claims and replicated run events.
- `stated-constraint`: Support native, GitHub, and Brigade work. The operator explicitly authorized all 3 rather than selecting a native-only first product.
- `judgment`: Deliver the sources in separate slices. This reduces conflict with concurrent Fleet Hub work and gives each adapter an independently testable contract.
- `evidence`: Keep GitHub credentials off the hub. The current fleet design stores dedicated fleet credentials and treats nodes as authenticated producers.
- `judgment`: Make imports node-side normalized batches. This avoids provisioning unrelated provider credentials on the hub and matches the current store-and-forward direction.
- `evidence`: Keep Ops Deck API as a proxy. Its current Tasks page already consumes HTTP data, while the API already protects write routes with a server-side credential.
- `judgment`: Require operator transitions for externally proposed completion in the first release. This avoids silently closing a cross-source item before evidence rules are defined.
- `judgment`: Treat Worklore admin mutations as an explicit exception to `--allow-admin-writes`, which remains a guard for event and claim impersonation.

## Deferred decisions

These do not block the first 4 slices:

- automatic dispatch from `GET /work/queue/burn`
- recurrence rules for maintenance work
- bulk editing and saved filters
- notifications when a reset window opens
- long-term archival outside Fleet Hub SQLite
- automatic completion policies based on merged pull requests or Brigade verification receipts
- a first-release HTTP route that creates `fleet-claim` links
