# Fleet roster page on the hub deck

Status: approved design, 2026-09-02. Tracked here because `docs/specs/` and `docs/plans/` are gitignored in this repo. Next step: implementation plan (recipe).

Tracks: #1259 (master roster visibility, first slice), #1417 (roster
list/show), #1223 (run preference pin, extended here).

## Problem

The fleet hub already holds a versioned model roster (13 seats at revision
15), consumer defaults, and a three-field run preference pin
(`impl`, `review`, `chef`). None of it is visible without the CLI, and the
operator's real routing policy is still spoken to each session:

- everyday implementation on Gemini 3.8 Flash through the Antigravity seat
- research only on the Codex luna seat, no other Codex seat
- Cursor only through the Other Models pool, never the Cursor Models pool
- security scans on the Daybreak Blue seat
- cloud work on Jules and Grok Bot

The hub cannot express "security goes here" or "research goes here", and no
interactive session reads the pin at start.

## Decisions

1. **The editor is a hub-served HTML page at `/deck/roster`.** Basis:
   stated-constraint (operator choice). Stdlib only, no JavaScript required,
   same cookie enrollment as the Command Deck.
2. **The first slice edits roles, seat on/off, cloud lane on/off, consumer
   defaults, and notes.** Basis: stated-constraint. Provider, model,
   reasoning, limit, and bindings stay CLI-only (`brigade fleet models set`).
3. **The run preference grows three role slots: `research`, `security`,
   `scout`.** Basis: evidence+judgment. These are the three spoken routing
   intents that are not "impl", "review", or "chef". Cloud lanes are not a
   role slot; they are the existing per-provider cloud policy rows.
4. **One Save is one hub transaction, and the roster revision bumps at most
   once.** Basis: judgment. A save that touches four seats and two consumer
   defaults bumps the revision once, so an LKG cache sees one coherent
   document. Roles and notes live in the preference row, which is outside
   the roster digest; they carry their own `updated_at` fence.
5. **Writes need the admin-derived dashboard cookie or the admin bearer.**
   Basis: stated-constraint (existing trust model). Node tokens and the
   Tailscale identity header are read-only for this page.
6. **Sessions learn the pin from `brigade work brief`.** Basis:
   evidence. The Claude SessionStart hook already injects the brief, so a
   `fleet_routing` block there reaches every session with no new hook.

## Alternatives considered

- **Editor inside `brigade center serve`.** Keeps the hub read-only, but the
  page exists only on a machine running center and the hub deck stays blind.
  Rejected by the operator.
- **JSON API plus a browser script.** The deck convention is "works with
  JavaScript disabled" and the CSP allows only nonce scripts. Rejected.
- **Cloud lanes as a role slot (`cloud = jules`).** Cloud providers are not
  roster seats; a role naming a provider would need a second resolver.
  Rejected in favor of the existing cloud policy toggles.

## Data

### `run_preference` (hub schema v18 -> v19)

Additive columns, no data rewrite:

| Column | Type | Rule |
| --- | --- | --- |
| `research` | TEXT | Roster seat name or NULL |
| `security` | TEXT | Roster seat name or NULL |
| `scout` | TEXT | Roster seat name or NULL |

`updated_by` stores the literal `deck-form` or `cli`, never an identity.
`SCHEMA_VERSION` in `fleet_hub.py` becomes 19. A hub built from an older ref
refuses a v19 database, so the runbook backup (section 8) precedes the
rollout.

### `run_preference.py`

`ALLOWED_FIELDS` becomes `impl, review, chef, research, security, scout,
notes`. The seat-name regex, the secret key regex, the secret value regex, and
the 240 character notes cap are unchanged. `planner_prefix()` prints the new
roles as `- research: <seat>`, `- security: <seat>`, `- scout: <seat>` after
`chef`. `resolve_worker()` is unchanged: only `impl` resolves a direct worker.
The cache file `~/.brigade/run-preference.toml` carries the new keys.

An older client reading a newer hub raises `unknown preference field` inside
`refresh_cache()` and silently keeps its cache; that is the existing behavior
and is acceptable for the rollout window. No new warning is added.

### Seats, consumer defaults, retired families, cloud lanes

No schema change. The page reads `model_policy`, `model_consumer_defaults`,
`retired_models`, `model_roster_meta`, and the cloud provider policy
(`fleet_hub._cloud_policy`). Writes reuse the existing hub writers:
`_write_set` (with the seat's current provider, model, reasoning, limit,
bindings, and notes carried through unchanged; only `enabled` changes; notes
are read straight from `model_policy` because the versioned seat projection
omits them), `_write_default`, `set_run_preference`, and `_set_cloud_policy`.
The cloud writer overwrites every column it is handed, so the page reads the
current policy first and passes `limit`, `hosted`, `circuit_state`, and
`subscription_pool` through unchanged; re-enabling a lane clears `reason`,
`reset_at`, and `expires_at`, while disabling a lane carries them through
unchanged; only `enabled` differs.

## Page: `GET /deck/roster`

Linked from the Command Deck nav as `roster`. Authorization is identical to
`/deck`: admin bearer, dashboard cookie, or the Tailscale identity header
when the hub trusts it. Non-token query parameters are never reflected; the
only recognized parameters are `token` (cookie enrollment, then 303 to the
bare path) and `saved=<revision>` (renders a one-line "saved as revision N"
banner when it matches the current revision).

One `<form method="post" action="/deck/roster">` with five blocks, in this
order:

1. **Roles.** Six `<select>` elements named `role.impl`, `role.review`,
   `role.chef`, `role.research`, `role.security`, `role.scout`. Options are
   `(unset)` plus every seat, enabled seats first, disabled seats listed
   under a `disabled` optgroup so a stale pin stays visible. The current
   value is selected. A `<textarea name="notes" maxlength="240">` follows.
2. **Seats.** One table row per `model_policy` row sorted by seat: seat,
   provider/model, reasoning, limit, Brigade CLI binding, T3 instance
   binding, and `<input type="checkbox" name="seat.<seat>" value="1">`.
   Disabled rows carry a `seat--off` class. A row whose model matches a
   retired family shows a `retired` flag and its checkbox is disabled.
3. **Cloud lanes.** One row per provider from the cloud policy: provider,
   `<input type="checkbox" name="cloud.<provider>" value="1">`, limit,
   hosted, circuit state (last three read-only text).
4. **Consumer defaults.** Two `<select>` elements named
   `default.brigade-run` and `default.t3-fleet`. Options are `(unset)` plus
   every seat, with seats lacking that consumer's binding shown under a
   `no binding` optgroup.
5. **Retired families.** Read-only list: provider, family, permanent flag,
   reason code.

Hidden inputs: `expected_revision` (current roster revision),
`expected_preference_updated_at` (the preference row's `updated_at`, or empty
when unset), `expected_cloud_state` (a digest of provider on/off pairs), and
`csrf`. A header line shows
`revision N, updated <stamp> by <deck-form|admin|schema-v15>`; `admin` is what
the CLI writers record today.
One `<button type="submit">Save</button>` at the bottom.

When the request is authorized only by the Tailscale identity header, every
input renders with the `disabled` attribute, the Save button is omitted, and
a line says "read-only: enroll with the fleet token to edit". No identity
value is rendered.

The page reuses the deck's `_document` wrapper (same CSS, nonce, manifest,
icons) and adds form styles to the deck stylesheet. The 10 second meta
refresh is omitted on this page so an unsaved form is not discarded.

## Write: `POST /deck/roster`

Request contract:

- Content-Type must be `application/x-www-form-urlencoded`; anything else
  answers 415.
- Body cap 64 KiB; larger answers 413.
- Authorization: admin bearer or dashboard cookie. A node bearer answers 403
  `{"error": "the admin token is required to edit the roster"}`. A request
  authorized only by the Tailscale identity header answers 403. No cookie
  and no bearer answers 401.
- CSRF: the hidden `csrf` value must equal
  `HMAC-SHA256(key=admin token, msg=b"brigade.fleet-roster-form.v1")` as
  lowercase hex, compared with `hmac.compare_digest`. Mismatch answers 403.
  The value is distinct from the dashboard cookie value and is never logged.
- Same-origin: when a `Sec-Fetch-Site` header is present it must be
  `same-origin`; when absent and an `Origin` header is present, its host
  must equal the request `Host`. Otherwise 403. A bearer-authenticated
  `curl` sends neither header and passes.
- `expected_revision` must parse as an integer; otherwise 400.

Apply, in one `BEGIN IMMEDIATE` transaction on the hub:

1. Read the current revision, the preference row's `updated_at`, and the
   current cloud state. If any differs from its hidden field, roll back and
   re-render the page (status 409) with the banner "roster changed underneath
   you: revision N is now M. Reload before saving." (or "the run preference
   changed underneath you" or "the cloud lanes changed underneath you. Reload
   before saving."). No table changes.
2. Build the target state: for every seat, `enabled` = checkbox present;
   for every cloud provider, `enabled` = checkbox present; roles and
   consumer defaults from the selects (`(unset)` -> NULL); notes trimmed.
3. Validate against the target state, before any write:
   - a role that names a seat must name a seat that is enabled in the target
     state and not retired;
   - a consumer default that names a seat must name a seat that is enabled
     in the target state and has that consumer's binding;
   - notes pass `run_preference.parse_preference` (secret regexes, 240 cap);
   - every submitted seat and provider name must exist on the hub (unknown
     names are ignored, never created).
   Any failure rolls back and re-renders (status 422) with the error text
   and the submitted values preserved. No table changes.
4. Write only the differences: changed seat `enabled` values through the
   existing seat writer, changed cloud `enabled` values through the cloud
   policy writer, changed consumer defaults through the default writer, and
   the preference row through `set_run_preference` with `updated_by =
   "deck-form"`.
5. Bump the roster revision once (`updated_by = "deck-form"`) when any seat
   or consumer default changed. A save that changes only roles, notes, or
   cloud lanes writes those rows but leaves the roster revision alone,
   because they are not part of the roster document digest.
6. Commit and answer 303 to `/deck/roster?saved=<revision>`.

A no-op save (nothing differs) commits nothing and redirects with the
current revision.

## Sessions learn the pin

### `brigade work brief`

New keys in the text output, after `latest_run`:

```
fleet_routing: impl=agy_flash review=claude_standby chef=chef research=researcher security=daybreak scout=cursor_scout
fleet_routing_notes: <notes or (none)>
fleet_disabled_seats: coder, cursor_composer, cursor_grok, reviewer
fleet_cloud_lanes: jules=on grok-bot=on codex=off cursor=off claude=off
fleet_routing_source: hub revision 16, cache age 42s
```

Source rules: the brief calls `run_preference.refresh_cache()` and the
versioned roster snapshot with a 3 second total budget; on any failure it
prints the cached preference and cached roster with their ages and
`fleet_routing_source: cache (hub unreachable)`. When no fleet is configured
(`~/.brigade/fleet.toml` absent and no env override) the keys are omitted.
The JSON brief carries the same data under `fleet_routing`. The brief never
fails or exceeds its existing timeout because of the hub.

### `brigade run`

`planner_prefix()` now includes the three new roles, so the chef prompt
reads "security: daybreak" and "research: researcher" without being told.
`--worker` and a spoken seat name still win. Disabled seats are already
denied by `resolve_fleet_model_policy`; nothing changes there.

### CLI

- `brigade fleet preference set` gains `--research`, `--security`,
  `--scout`. `get`, `pull`, and `fleet status` print the new fields.
- `brigade fleet models` (no subcommand) keeps its table; #1417's
  `list`/`show` verbs are not part of this slice.

## Failure handling

| Situation | Behavior |
| --- | --- |
| Hub down at session start | brief prints cache with age; no error |
| Hub down at `brigade run` | existing cache behavior, unchanged |
| CLI mutation between page load and Save | 409 re-render, no write |
| Role points at a seat disabled in the same save | 422 re-render, no write |
| Consumer default points at a seat without that binding | 422, no write |
| Notes contain a token, env value, or home path | 422, no write |
| Old client, new hub | client keeps cache silently (existing behavior) |
| Hub rollback after v19 | restore the section 8 backup per runbook |

## Tests

Hub (`tests/test_fleet_roster_page.py`, new):

- v18 database migrates to v19 with existing preference row intact.
- GET renders all five blocks, the current revision, and selected values;
  the body contains no admin token, cookie value, node token, or CSRF value
  other than in the hidden input.
- GET without auth answers 401; with a node bearer answers 401 for the deck
  path as today.
- GET under Tailscale identity renders every input disabled and no Save.
- POST without auth 401; with node bearer 403; under Tailscale identity 403;
  wrong CSRF 403; cross-site `Sec-Fetch-Site` 403; wrong Content-Type 415;
  oversized body 413.
- Stale `expected_revision`: 409 and every roster table is byte-identical
  before and after. Stale `expected_preference_updated_at` (a CLI
  `preference set` between load and save): 409, nothing written.
- A save that disables two seats, sets `role.security`, sets
  `default.t3-fleet`, toggles one cloud lane, and edits notes bumps the
  revision exactly once and lands every value.
- A save whose `role.impl` names a seat unchecked in the same form: 422 and
  no table changes.
- Hostile notes (`<script>`, control characters) are escaped on re-render
  and rejected when they match the secret regexes.
- A no-op save leaves the revision unchanged.

Preference (`tests/test_run_preference.py`):

- The three new fields parse, round-trip through the cache, and appear in
  the planner prefix; `resolve_worker` still ignores them.
- The secret key regex still rejects `token`, `env`, `path`, `home` keys
  and does not reject `security`.

Brief (`tests/test_work_cmd_session.py`, next to the existing brief assertions):

- Cached preference and roster render the five `fleet_` keys.
- Hub unreachable renders the cache source line within the time budget.
- No fleet configured omits the keys.

CLI:

- `preference set --security daybreak` posts the field and refreshes the
  cache; `fleet status` prints it.

## Files

- Create `src/brigade/fleet_hub_roster_page.py`: read projection, HTML
  render, form parse, validation, apply. Pure functions over a connection
  so tests render without a socket.
- Modify `src/brigade/fleet_hub_preference.py`: three columns, `updated_by`
  values, additive migration.
- Modify `src/brigade/fleet_hub.py`: `SCHEMA_VERSION = 19`.
- Modify `src/brigade/fleet_hub_http.py`: route `GET/POST /deck/roster`,
  CSRF and same-origin checks, form body reader.
- Modify `src/brigade/fleet_command_deck.py`: nav link, form CSS.
- Modify `src/brigade/run_preference.py`: fields, prefix.
- Modify `src/brigade/cli/fleet.py`: three flags, printing.
- Modify `src/brigade/work_cmd/session/briefing.py` (the brief text and
  JSON producer): the `fleet_routing` block with a 3 second budget, in a
  helper small enough to keep that module under its frozen ceiling.
- Modify `docs/fleet-sync.md`: the page, the write contract, the role set,
  schema v19.
- Modify `docs/runbooks/fleet-hub-proxmox.md`: endpoint list.

Module size ratchet: the new module must stay under 2000 lines and every
modified module under its frozen ceiling; the render and apply halves may be
split if the first exceeds it.

## Rollout

1. Merge, then refresh the pipx install on each client (workstation first).
2. Back up the hub database (runbook section 8).
3. Refresh the hub CT's pipx install and restart `brigade-fleet.service`.
4. Open `/deck/roster`, set the six roles and toggles to the spoken policy,
   Save.
5. Start a fresh Claude session in a wired repo and confirm the brief shows
   `fleet_routing` with revision 16 or later.

## Non-goals

- Editing provider, model, reasoning, limit, or bindings in the browser.
- Publishing a full roster overlay to nodes (#1259 remains open).
- `fleet models list` / `show` (#1417).
- Any change to admission, leases, LKG cache, or Grok Bot queue authority.
