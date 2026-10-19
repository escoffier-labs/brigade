# Fleet Ownership: who owns what

Status (2026-10-19): design approved in direction (charter plus live holdings). Not built.
Scope: design only. The implementing agent works test-first from this document.
Deployment line: Brigade beta main. No release bump.

## 1. BLUF

The hub gets an ownership map. Each agent or surface in the fleet (Grok Bot, the
Miller dot bridge, the OpenClaw gateway and its clawchat client, T3 Code
sessions, brigade run seats) gets a declared charter: what work it owns, how
work reaches it, and what it must never touch. The charter lives in a new
`owners` section of the versioned fleet policy document, so it inherits
revisions, compare-and-swap saves, history and rollback with no new table.

A read-only page, `/deck/owners`, renders each charter beside what that owner
holds right now, joined from tables the hub already keeps: repo claims, Grok
Bot jobs, interactive sessions and policy delegations. Live items that no
charter matches land in an Unassigned list, which is the operator signal this
feature exists for. `GET /owners` returns the same view as JSON so agents can
ask the hub who owns a kind of work before they pick it up.

## 2. Problem

The hub knows a lot about individual work items but nothing about
responsibility. Today:

- Repo claims carry a free-text `harness` and `owner_node` per claim
  (`fleet_hub.py` `claims` table). They say who holds a lock, not whose job
  the work is.
- Grok Bot has a per-node actor policy (`grokbot_actor_policy`) that only
  covers Grok Bot roles.
- The policy `repositories` section has one free-text `owner` field per repo.
- The OpenClaw gateway has no hub integration at all, so its scheduled jobs and
  chat-driven work are invisible.
- Nothing states boundaries ("Grok Bot never deploys"), so overlaps and gaps
  are found by accident.

## 3. Goals and non-goals

Goals:

1. One declared charter per owner, edited through the existing policy save
   path and visible in policy history.
2. One page that shows every charter with its live holdings and an Unassigned
   list for work no charter covers.
3. A JSON read so agents can consult the charter.

Non-goals:

- Enforcement. The charter does not block claims, dispatch or merges in this
  phase. Enforcement can come later once the map is trusted.
- New reporting from surfaces that do not report today. OpenClaw shows its
  charter with an explicit "no hub feed" note until a separate change gives it
  a reporting path.
- Worklore items and the `events` history. Worklore attribution runs through
  `work_links` and is feature-flagged. Events are history, not holdings.
  Both can join later behind the same matcher.

## 4. Data model: the `owners` policy section

`brigade.fleet_policy.v1` gains an optional top-level key `owners`, a mapping
from owner name (`NAME_PATTERN`) to a record:

```json
{
  "owners": {
    "grokbot": {
      "label": "Grok Bot",
      "summary": "Builds approved issues and shepherds its own PRs.",
      "owns": ["Issues labeled grokbot-build-approved", "Merging its own green PRs per the shepherd policy"],
      "never": ["Deploys", "Releases", "Credential or secrets paths"],
      "intake": ["Approval label", "Builder wake webhook"],
      "contact": "agent:grokbot",
      "match": {"grokbot_jobs": true, "harnesses": ["grokbot"]}
    },
    "miller": {
      "label": "Miller (dot)",
      "owns": ["ChatGPT-side planning", "T3 dispatch it initiates", "Hub inbox triage"],
      "match": {"session_harnesses": ["dot"], "principals": ["miller"]}
    }
  }
}
```

Fields, all validated with the existing helpers (`_mapping`, `_reject_unknown`,
`_text`, `_name_list`, `MAX_TEXT`, `MAX_LIST_ITEMS`, `MAX_RECORDS`):

| Field | Type | Required | Notes |
|---|---|---|---|
| `label` | text | yes | Display name. |
| `summary` | text | no | One line. |
| `owns` | list of text | no | Kinds of work this owner is responsible for. |
| `never` | list of text | no | Explicit boundaries. |
| `intake` | list of text | no | How work reaches it. |
| `contact` | text | no | A fleet message address (`agent:<name>` or node id). Display only. |
| `match` | mapping | no | Live attribution selectors, below. Absent means charter-only. |

`match` keys (closed set, each a list of names unless noted):

| Key | Matches |
|---|---|
| `nodes` | `claims.owner_node`, `interactive_sessions.node_id`, delegation `origin_node` / `target_node`, `grokbot_jobs.claimant_node` |
| `harnesses` | `claims.harness` |
| `conductors` | `claims.owner_conductor` |
| `session_harnesses` | `interactive_sessions.harness` |
| `adapters` | `fleet_policy_delegations.adapter` (for example `t3-fleet`) |
| `grokbot_jobs` | boolean: all `grokbot_jobs` rows |
| `principals` | `fleet_message_principals.name`. Shows that the principal exists, no message contents |

A live item matches an owner when any one selector matches (OR within the
record). An item that matches two owners appears under both and is counted in
an Overlaps line. Overlap is shown, not resolved: it usually means two charters
claim the same work and the operator should edit one.

Validation rules: `match` lists are names, not patterns. No wildcards in this
phase. Unknown keys inside a record or inside `match` are rejected, as every
other policy section does.

### Compatibility

`parse_document` rejects unknown top-level keys, so a hub older than this
change refuses to save, roll back to, or re-parse a revision containing
`owners`. Clients parse the document too: T3 routing calls
`fleet_t3_transport.target_binding`, which parses the hub policy and treats a
parse failure as `t3-binding-missing`. An install that predates this change
would therefore block every T3 route once an `owners` revision is live. This
change makes `target_binding` drop top-level keys the install does not know
before parsing, but older installs do not have that fix. Rollout order: deploy
the new hub and upgrade every enrolled install, then save the first `owners`
revision. New code treats a missing `owners` key as `{}`, so old documents
parse unchanged.

## 5. Live holdings

A new module `src/brigade/fleet_owners.py` (pure, no HTTP) provides:

- `load_view(conn, *, now_epoch) -> OwnersView`: reads the current policy
  revision via `fleet_hub_policy.current_policy`, then the live rows:
  - active claims (`fleet_hub_claims.list_claims`, unexpired only),
  - `grokbot_jobs` in non-terminal states,
  - live `interactive_sessions` (`fleet_hub_sessions.list_sessions`),
  - active `fleet_policy_delegations`.
- `attribute(charters, items) -> (by_owner, unassigned, overlaps)`: the
  matcher, a pure function over plain dataclasses so it is unit tested without
  a database.

Each holding is normalized to one shape: `kind` (claim, grokbot-job, session,
delegation), `ref` (claim repo, job id, session id, delegation id), `repo`,
`node_label` (joined through `nodes.label`, falling back to a short id),
`detail` (role, seat or state), and `since`.

If the policy read fails, the page still renders live items, all in
Unassigned, with a banner naming the failure. If one live source fails, its
section shows "unknown" and the others render, matching the Command Deck
coverage-gap convention.

## 6. Surfaces

- `/deck/owners` (HTML, read-only, same auth as `/deck/work` via
  `_roster_auth()`). Layout: one card per owner in name order with label,
  summary, owns, never, intake, contact, then a holdings table. Owners with no
  `match` show "charter only, no live attribution". After the cards, an
  Unassigned table and an Overlaps line. Rendering lives in a new
  `src/brigade/fleet_owners_page.py` following `fleet_work_page.py`.
- `GET /owners` (JSON, enrolled node token or admin token). Body:
  `{"schema": "brigade.fleet_owners.v1", "policy_version": N, "owners": [...],
  "unassigned": [...], "overlaps": [...]}`. Agents use it to check a charter
  before taking work.
- Nav: add an "Owners" link to each hard-coded deck nav string (Command Deck,
  roster, policy, repo policy, work, dashboard). The Command Deck edit must
  stay one line, since that module has 47 lines of headroom under the size ratchet.
- Editing: `brigade fleet policy save --file` with the whole document, as for
  every other section. A `brigade fleet owners show` CLI prints the JSON view.
  A section-level `owners set` command and a `/deck/policy` editor panel are
  deferred. `fleet_policy_page.py` has 101 lines of headroom and should not
  absorb a form.

`fleet_hub_http.py` changes stay under about 30 lines: one `do_GET` branch per
route and thin `_serve_owners` / `_serve_owners_json` methods that open the
database `query_only`, call `fleet_owners.load_view`, and render.

## 7. Known attribution gaps

- T3 Code interactive sessions report through `interactive_sessions` with
  their underlying harness (`claude`, `codex`), so a T3 charter matching on
  `session_harnesses` would also catch plain CLI sessions. In this phase T3 is
  attributed through `adapters: ["t3-fleet"]` delegations and by node.
  Distinguishing T3-hosted interactive sessions needs the session report to
  carry its host app, filed as a follow-up.
- OpenClaw has no hub feed until slice 2 (section 10) ships. Until then its
  charter renders with no holdings.

## 8. Testing

- `tests/test_fleet_policy_owners.py`: parse accepts a full record, rejects
  unknown record and `match` keys, bounds list and text sizes, treats a
  missing section as empty, and round-trips through save and rollback.
- `tests/test_fleet_owners.py`: the matcher on plain data: single match,
  OR across selectors, overlap reported under both owners, unmatched item to
  Unassigned, charter with no `match`.
- `tests/test_fleet_owners_page.py` (pattern of `test_fleet_work_page.py`):
  401 without auth, read-only cookie renders cards, Unassigned and Overlaps,
  policy read failure banner, `/owners` JSON schema and node-token auth, and
  nav link present on each deck page.
- Size ratchet: no listed module grows past its frozen size.

## 9. Rollout

1. Merge and deploy the hub (Fleet Hub container) and the pipx installs.
2. Confirm every enrolled install is upgraded (see section 4, Compatibility).
   Draft the first `owners` revision from the current lane map and save it
   with `brigade fleet policy save --expected-version N --reason "seed owners"`.
   The real charter text lives only in the hub database, never in this
   repository. Each charter edit bumps the policy revision, and prepare and
   launch refuse a delegation whose policy version or digest changed. Edit
   charters when no delegations are live.
3. Review the Unassigned list for a week, editing charters until it is empty or
   every remaining item is understood.

## 10. Slice 2: OpenClaw reporting

The operator is moving day-to-day agent work from Discord to clawchat, so the
OpenClaw gateway becomes a first-class owner with live holdings. Slice 1 (the
page) ships first and does not wait on this.

### Mapping

An OpenClaw workspace plugin, `fleet-presence`, reports through the existing
`brigade work presence-hook` command, which already publishes to
`POST /sessions` with the host's enrolled node token. The hub accepts any
opaque harness string, so `openclaw` needs no allowlist change.

| OpenClaw hook | Presence call |
|---|---|
| `session_start` | `--event start --harness openclaw --session <sessionKey>` |
| `agent_end`, `message_received` | `--event heartbeat`, throttled to one per session per 60 s |
| `session_end`, `subagent_ended` | `--event end` |
| `cron_changed` status `started` | `--event start --session cron:<jobId> --kind cron --label <job name>` |
| `cron_changed` status `finished` | `--event end --session cron:<jobId>` |
| `gateway_stop` | `--event end` for every session the plugin started |

The plugin spawns the command without a shell and does not await it, so a slow
or unreachable hub never delays a chat turn. `--target` is the OpenClaw
workspace checkout, which gives a stable fleet-scoped repo identity. Session
keys keep their `agent:<agent>:<surface>:<id>` form, so the page can group by
surface (clawchat, discord, cron, subagent) without a lookup.

The OpenClaw charter matches with `session_harnesses: ["openclaw"]`.

### Brigade changes

1. `interactive_sessions` gains two nullable columns: `label` (text, at most
   `MAX_TEXT`) and `kind` (name pattern, for example `chat`, `cron`,
   `subagent`). Additive columns go in `_ADDITIVE_COLUMNS` and
   `SCHEMA_VERSION` goes from 32 to 33, as the hub schema rules require.
2. `_UPSERT_FIELDS` accepts `label` and `kind`. Both are optional and
   validated. Older clients that omit them are unaffected.
3. `brigade work presence-hook` gains `--label` and `--kind`, passed through
   to the upsert.
4. `fleet_deck_brands.py` gains an `openclaw` brand so these rows stop
   rendering as the grey unknown badge.
5. The owners page shows `label` when present and groups OpenClaw holdings by
   session-key surface.

### OpenClaw changes (outside this repository)

The plugin lives in the operator's OpenClaw workspace extensions directory
with `package.json` (`openclaw.extensions`), `openclaw.plugin.json` and
`index.ts`. It is listed in both `plugins.allow` and `plugins.load.paths`.
It reads metadata only, so it does not request conversation access. Enabling
it needs one gateway restart.

### Testing

- Brigade: upsert round-trips `label` and `kind`. It rejects an over-long
  label and a `kind` outside the name pattern. A v32 database migrates to v33
  with existing rows intact, and presence-hook passes both flags through.
- Plugin: a dry-run mode logs the command lines it would spawn. Verify with
  one clawchat message and one forced cron run, then confirm both rows appear
  in `brigade fleet sessions --json` and end on schedule.

