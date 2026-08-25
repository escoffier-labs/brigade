# Command Deck: Fleet Hub Dashboard Replacement

Status: reviewed implementation specification, awaiting operator approval before any code lands.
Scope: design only. The implementing agent works test-first from this document.
Deployment line: Brigade beta main (`main`). Version 0.27.0 stays as it is. nothing here cuts or bumps a release.
Provenance: past-work evidence id `a347f29f260af60ce27f7c1d`. supersedes the machine board from issue #1124 / PR #1145 as the primary hub view after canary approval.
Tracking: file one implementation issue at approval ("Command Deck: replace the fleet machine board") and link it here. the placeholder below is `<DECK-ISSUE>`.

## 1. BLUF and problem statement

Command Deck is a replacement first screen for the private fleet hub: one server-rendered page that answers, for the three operator-configured hosts, whether anything needs the operator, how busy each host is against its fixed capacity (6, 4, 4), what every live run is doing, where claims collide, and what recently finished. It ships read-only on beta main, behind the existing tailnet-only auth, with zero new runtime dependencies. Real host names stay in the CT-local config and never enter this public repository.

The current dashboard (issue #1124, merged in PR #1145) is live at the hub and fails as an operating picture:

- It presents observed node IDs as machines. `node_summary()` derives "machines" from `SELECT ... FROM events GROUP BY node_id` (`src/brigade/fleet_hub.py:509`), so every cloud runner or pre-enrollment poster that ever emitted one event renders as a peer card, and `_summary()` counts them as "N machines" (`src/brigade/fleet_dashboard.py:360`, `src/brigade/fleet_dashboard.py:657`). The three real hosts are visually indistinguishable from noise.
- It shows raw UUIDs. `short_node()` truncates a uuid4 to 12 hex characters (`src/brigade/fleet_dashboard.py:285`) and uses it as the card title (`src/brigade/fleet_dashboard.py:492`). The `nodes.label` column written by `brigade fleet nodes add <NODE_ID> --label <HOSTNAME>` exists in schema v4 (`src/brigade/fleet_hub.py:207`) but is never selected for display.
- It overflows horizontally. Cards sit on `repeat(auto-fit, minmax(320px, 1fr))` (`src/brigade/fleet_dashboard.py:739`) around fixed-width tables (`table.data-table { width: 100% }`, `src/brigade/fleet_dashboard.py:779`) whose cells hold unbreakable run IDs and nested badge lists. The stylesheet has no media query, so on a phone the repo board and loaded machine cards push the viewport wide.
- It mixes stale and historical rows into the operating picture. The handler fetches every run ever recorded with `latest_status(conn, include_all=True)` (`src/brigade/fleet_hub.py:1112`). the default view hides terminal rows client-side (`src/brigade/fleet_dashboard.py:222`) but the summary counts still draw from the full set, the repos board builds its "last outcome" column from all history (`src/brigade/fleet_dashboard.py:563`), and the query cost grows without bound. Meanwhile a failed run is terminal, so it vanishes from the default board entirely: the worst news disappears first.

The approved direction fixes this by making enrollment, not observation, the unit of the page, and by separating the operating picture (live only) from outcomes (a bounded recent timeline).

## 2. Goals and non-goals

Goals:

1. One screen that answers section 3 within five seconds of load, refresh included.
2. Exactly three named host stations in fixed operator-configured order, each with capacity 6, 4, and 4 respectively, sourced from CT-local operator config rather than inference.
3. Host identity resolved from enrollment and labels. raw UUIDs demoted to tooltip/detail level only.
4. Live-only operating picture. terminal runs appear only in a bounded recent-outcome timeline and the attention rail.
5. Claim ownership, TTL remaining, and collision visibility on both the deck and the secondary repo view.
6. Unknown or cloud-origin observers visible but segregated, never counted as machines.
7. Same security posture as today: tailnet-only bind, bearer-or-cookie auth, identical CSP and header set, read-only GET routes, nothing secret rendered.
8. Zero runtime dependencies preserved: stdlib HTTP + SQLite + server-rendered HTML, working with JavaScript disabled.
9. Bounded deck response sets, with the whole-history latest-state query cost explicitly left to #1146 rather than hidden behind UI limits.

Non-goals for this feature:

- Any write path: no POST routes, no claim mutation, no node management, no acknowledge or resolve buttons.
- Public hosting of any kind, including Vercel or any non-tailnet exposure. The hub keeps its required `--host` bind.
- Cutting v0.27.0 or changing the release process. rollout rides the beta main ref swap documented in `docs/runbooks/fleet-hub-hogwarts.md`.
- Fixing the legacy boards' unbounded queries, their login-cookie lifetime, or hub lock behavior beyond what the deck itself needs. those remain separate open follow-ups.
- Campaigns (issue #1128). Deck renders whatever the claims table already arbitrates. campaign semantics land later and must not change deck contracts except by adding rows.
- New event types, heartbeats, or client-side changes to `fleet_client.py`. The deck is a hub-side projection of existing tables.
- A JS framework, build step, CDN asset, or websocket live push. `<meta http-equiv="refresh">` stays.

## 3. Operator questions the page must answer within five seconds

Each question maps to one region of section 5:

| # | Question | Answered by |
| --- | --- | --- |
| Q1 | Is anything on fire right now? | Global status bar verdict plus attention rail count |
| Q2 | Are all three hosts reporting, and how full is each? | Station headers: `5/6 busy`, `heard 20s ago` |
| Q3 | What is each host working on right now? | Active task tiles per station |
| Q4 | What needs me and where is it? | Attention rail entries naming host, repo, state |
| Q5 | Are two machines stepping on one repo? | Collision markers on tiles and the repo view |
| Q6 | Did recent work land or fail? | Recent outcome timeline (bounded window) |

Five seconds assumes the tailnet path and a warm browser. the page must render fully from HTML with scripts blocked, since the inline script only ticks timers.

## 4. Information architecture and wireframes

Two routes, one mental model:

- `GET /deck`: the operating picture. Global status bar, three stations in fixed order, attention rail, recent outcome timeline, other observers (collapsed).
- `GET /deck/repos`: the secondary repo view. One row per claim target or live repo: owner, TTL, who is on it now, collision flag.

Navigation between them is two text links in the deck header. Query parameters stay minimal: none on `/deck` in slice 1 except optional `?compact=1` deferred. `/deck/repos` accepts none. Filters exist only as links that preset nothing (no filter form on the deck. the legacy boards keep theirs).

### Desktop wireframe (1440x900)

```text
+------------------------------------------------------------------------------------------+
| COMMAND DECK        ALL CLEAR (or: 3 NEED ATTENTION)     slots 7/14      12:04:51 UTC     |
| [repos] [classic boards]                                   refreshes every 10s            |
+------------------------------------------------------------------------------------------+
| STATION A   <STATION-A-LABEL>      STATION B   <STATION-B-LABEL>   STATION C <STATION-C-LABEL>|
| 5/6 busy  heard 20s ago            2/4 busy  heard 35s ago         1/4 busy heard 12s ago   |
| [tile] [tile] [tile]               [tile] [tile]                   [tile]                   |
| [tile] [tile]                                                                               |
+------------------------------------------------------------------------------------------+
| NEEDS YOU (3)                                                                              |
| x failed   C  repo-d   run-kilo   12m ago                                                   |
| ? awaiting B  repo-b   run-bravo   approval.requested                                       |
| ! collision A/B  repo-a  claim held by A, live on both                                      |
+------------------------------------------------------------------------------------------+
| RECENT OUTCOMES                                                                            |
| 12:01 succeeded repo-a A · 11:58 failed repo-d C · 11:40 interrupted repo-c B               |
+------------------------------------------------------------------------------------------+
| Other observers (2, hidden): <details>                                                     |
+------------------------------------------------------------------------------------------+
```

Stations share one grid row at this width. tiles wrap inside each station.

### Phone wireframe (390x844)

```text
+------------------------------------+
| COMMAND DECK   3 NEED ATTENTION    |
| slots 7/14 · 12:04:51 UTC · [repos]|
+------------------------------------+
| STATION A 5/6 busy · heard 20s     |
| | tile: repo-a  worker/claude      |
| | running 12m · claim: A·orch      |
| | tile: repo-c  worker/claude      |
| | queued 30s                       |
+------------------------------------+
| STATION B 2/4 busy · heard 35s     |
| | tile ...                         |
+------------------------------------+
| STATION C 1/4 busy · heard 12s     |
| | tile ...                         |
+------------------------------------+
| NEEDS YOU (3)                      |
| x failed C repo-d run-kilo 12m     |
| ...                                |
+------------------------------------+
| RECENT OUTCOMES (last 20)          |
| ...                                |
+------------------------------------+
| Other observers (2) <details>      |
+------------------------------------+
```

On phone, stations stack vertically and each tile is a full-width block. nothing requires side-by-side columns, so no horizontal scroll exists at either width.

## 5. Visual hierarchy

Ordered top to bottom. Every region is an HTML landmark or labelled section so keyboard order matches visual order.

1. Global status bar (highest weight). Verdict word derived from the worst signal present: `NEEDS ATTENTION (n)` when the rail is non-empty, otherwise `ALL CLEAR`. Beside it: total slots busy across configured stations (`7/14`), the UTC data-as-of stamp, refresh note, and the two navigation links. This bar carries the strongest background treatment on the page and is the first `<h1>`-adjacent element.
2. Three named host stations in fixed config order. Equal visual size. a station never reorders based on activity. Header per station: display name (large, human label), resolved identity detail (enrolled label or shortened node id, small monospace, title attribute carries the full node id), utilization `n/cap busy`, and last-heard age from `MAX(received_at)`.
3. Host health/utilization encoding: utilization renders as text plus a segmented bar of `cap` cells, filled = occupied. Over-capacity fills past the bar end and adds an `over` marker. Last-heard ages over 15 minutes get a quiet marker. there is deliberately no "offline" state (see section 6 freshness rules).
4. Active task tiles. One tile per live run under its station, sorted by the existing bucket rank (`failed` > `awaiting approval` > `stale` > `running` > `queued`. `interrupted` and `succeeded` never appear here because they are terminal). A suffix failure such as `run.dispatch.failed` remains a live failed tile until a later exact lifecycle terminal event arrives. Tile content: repo name (primary line), seat/harness chip, state badge reusing the existing bucket vocabulary and colors, elapsed time, run id in small monospace with full value in `title`. If an active claim row exists for the tile's repo target: second line with owner node name, conductor if set, and TTL remaining (`claim: A · orch · 11m left`). If the collision predicate holds: a `! collision` badge on the tile edge.
5. Attention rail. Flat list directly under the stations, entries sorted failed, then awaiting approval, then stale, then collisions. Each entry is a plain-text line naming host short name, repo, run id, state, and age. Sources: failed terminal runs within the lookback window (capped 10, newest first), live awaiting-approval runs, live stale runs, and every current collision. Empty state is the sentence `Nothing needs you.` not a blank region.
6. Claim collision visibility. Collisions appear in three places by design: the tile badge (section 5 item 4), a dedicated rail entry type, and the collision-first sort of `/deck/repos`. Visual marker is the existing failed-red border style. text always names both parties (`held by X, live on X and Y`).
7. Recent outcome timeline. One line per terminal run in the bounded window (default last 20 by event `ts` descending): stamp, outcome badge, repo, station short name, run id. Terminal means `run.completed`, `run.failed`, `run.interrupted` exactly as `TERMINAL_STATES` defines today (`src/brigade/fleet_hub.py:159`).
8. Other observers (lowest weight). A native `<details>` element titled `Other observers (n)` listing up to 8 observed-but-unconfigured node ids, most recently heard first, each with shortened id and last-heard age. Rendered after the timeline. excluded from every count.
9. Footer link row: `classic boards` linking to `/view/machines` and `/view/repos`. Keep it until the operator explicitly approves removal. root promotion alone does not remove it.

Secondary repo view (`/deck/repos`): a single table, collisions first then by target name. Columns: target, owner (station name plus conductor), TTL remaining, live now (station names plus states), flags (`! collision`). Rows cover the union of active claim targets and repos with live runs. empty state `No claims or live repos.` Expired claims are absent because the deck consumes active claims only.

## 6. Data semantics

### Enrolled host versus observed node

- Enrolled host: a row in the `nodes` table (`node_id`, `label`, unrevoked). Enrollment binds credentials and is necessary for station membership.
- Observed node: any distinct `node_id` present in `events`. This is what the legacy board wrongly treats as machines.
- Configured station: a `stations[]` entry in the deck config (section 7). Station membership requires BOTH a config entry AND an unrevoked enrollment matching its `node_id`. A config entry whose node is not enrolled renders greyed with `not enrolled` instead of disappearing, so config drift is visible.

### Host label resolution (first match wins)

1. Deck config `name` for the station.
2. `nodes.label` from enrollment.
3. Shortened node id (first 12 characters, same rule as `short_node()`), marked `unenrolled-label`.

Full node ids appear only in `title` attributes and the other-observers list, never as primary text.

### Capacity source

Capacities come exclusively from the deck config file read once at hub startup. There is no database change and no inference from run counts. Missing config entry means no station. missing `capacity` key is a validation error at startup (fail closed, same spirit as a missing token).

### Run state vocabulary and rules

The deck reuses `bucket_for()` semantics (`src/brigade/fleet_dashboard.py:147`) with these exact rules, evaluated per run from its latest event:

| Deck state | Rule |
| --- | --- |
| `failed` | exact `run.failed` is terminal. another suffix failure such as `run.dispatch.failed` remains a live failed tile and rail item until a later lifecycle terminal event |
| `awaiting approval` | latest state in {`run.paused`, `approval.requested`, `approval.held`}, regardless of age (age does not demote it to stale. preserves current behavior) |
| `stale` | non-terminal, none of the above, and event age > `stale_after_seconds` (default 1800) |
| `queued` | latest state is `run.created`, not stale yet |
| `running` | any other non-terminal state, not stale yet |
| terminal | latest state in `TERMINAL_STATES`. excluded from stations and tiles, shown in timeline (window-bounded) and, if `run.failed`, in the rail (lookback-bounded) |

Event age = `now - ts` of the latest event, computed identically to `build_rows()`.

### Freshness thresholds

- Page refresh: 10 seconds via meta refresh (unchanged).
- Staleness: 1800 s default, configurable per deployment via deck config `stale_after_seconds` (valid range 300..21600).
- Station last-heard: displayed from `MAX(received_at)` per configured node. Ages over 900 s render a `quiet` marker.
- No offline inference: nodes post events only on journal appends, so silence is indistinguishable from idle-with-no-runs. An idle station reads `0/N busy`. the spec forbids an `offline` badge until a heartbeat mechanism exists (open follow-up, out of scope).

### Claim ownership

A repo target's active claim is the single unexpired `claims` row keyed by target (`owner_node`, `owner_conductor`, `expires_at`). Rendering mirrors the default `list_claims()` behavior: display owner name, conductor, and TTL remaining. Expired claims are pruned or excluded rather than rendered. Holder tokens are never rendered.

Collision predicate (unchanged definition): two or more distinct nodes have live runs on one target, OR a live run exists on a target whose unexpired claim belongs to a different node. Computed against live runs only.

### Unknown and cloud nodes

Observed node ids matching no configured station go to Other observers, capped at 8 by recency. They are excluded from the station grid, from `slots busy`, from every headline count, and from the phrase "machine". If the hub runs with admin writes and a cloud job posts under an arbitrary id, the deck shows it as an observer line, which is the correct severity: visible, not alarming, never inflating the three-machine model.

## 7. Server and route shape

Zero-runtime-dependency constraint holds: stdlib `http.server` + `sqlite3` + `html` rendering, one process, one SQLite file, no framework, no static assets, no CDN. The deck follows the established pattern of pure render functions over query results so tests run without sockets (`render_page()` style, `src/brigade/fleet_dashboard.py:640`).

New module: `src/brigade/fleet_command_deck.py`. Contains: config loading/validation, capped query helpers, pure projection functions (stations, tiles, rail, timeline, repo entries), and `render_deck(view, config, conn_data..., nonce, now) -> str`. `fleet_hub.py` gains routing and the `--deck-config` flag. it must not grow rendering logic.

Routes, slice 1 (parallel route. root untouched):

- `GET /deck` : primary operating picture.
- `GET /deck/repos` : secondary repo view.

Dispatch order in `do_GET`: `/health`, then `/deck` prefix handled by the deck renderer, then existing `/` and `/view/*` handling unchanged. Legacy boards keep working verbatim. the transition footer links to them.

Root promotion (separate small change, gated on canary approval in section 10): `GET /` renders Command Deck. `/view/machines` and `/view/repos` continue serving the legacy boards indefinitely, and the classic-board footer remains until a later operator decision. Promotion touches only route dispatch plus tests and is reversible by ref swap. Until promotion, `/deck` is the canonical URL shared from the runbook.

Config file (operator-owned on the hub CT, e.g. `/etc/brigade/command-deck.json`, root-owned mode 0600):

```json
{
  "stations": [
    {"node_id": "<STATION_A_NODE_ID>", "name": "Station A", "capacity": 6},
    {"node_id": "<STATION_B_NODE_ID>", "name": "Station B", "capacity": 4},
    {"node_id": "<STATION_C_NODE_ID>", "name": "Station C", "capacity": 4}
  ],
  "stale_after_seconds": 1800,
  "outcome_window": 20,
  "failed_lookback_seconds": 86400
}
```

Loaded via `brigade fleet serve --deck-config PATH` (new optional flag on the existing parser in `src/brigade/cli/fleet.py`. also `BRIGADE_FLEET_DECK_CONFIG` env override). Validation at startup: JSON object. 1..16 stations. `node_id` matches the existing `CLAIM_ID_PATTERN` and is not `unknown`. `name` 1..64 chars after stripping C0/C1 control characters. `capacity` integer 1..64. unknown keys ignored. missing or invalid file raises `FleetHubError` at `make_server` time so a bad config refuses startup like a bad token does. Without the flag the deck routes answer with a valid page showing zero stations and a one-line hint to configure `--deck-config`, so deploy-time mistakes are visible but never a 500.

Progressive enhancement policy: identical information with JavaScript disabled. The single inline nonce'd script may tick elapsed timers only. collapsible sections use native `<details>`. no fetch, no localStorage, no client routing.

Bounded result sets (deck-local, leaving the legacy query-cost follow-up #1146 untouched):

- Active runs: compute the latest row per `(node_id, run_id)` first, then exclude exact terminal states, sort deterministically by attention rank, latest timestamp, node id, and run id, and return at most 200 rows. Never filter terminal events before latest-row selection, which would resurrect completed runs. This caps rendering and response size but does not make the existing whole-history latest-state projection constant-time. #1146 owns that storage/query change.
- Started-at: fetched only for the selected run keys (single aggregate query with an `IN` clause over those keys, chunked if needed), replacing the whole-table scan the legacy path does via `run_started_at()`.
- Outcome timeline: terminal runs ordered by `ts` DESC `LIMIT outcome_window` (default 20, max 100).
- Rail failure lookback: exact terminal `run.failed` rows with `ts` within `failed_lookback_seconds` (default 86400), `LIMIT 10`. suffix failures that are still live come from the active-run projection.
- Claims: reuse `list_claims()` (already a full-table read of a small table. acceptable and unchanged).
- Station last-heard: aggregate grouped query restricted to configured node ids.

## 8. Authentication, network, and output requirements

All inherited from the existing dashboard path unless stated:

- Auth: admin bearer token or the `brigade_fleet_view` cookie, exactly the check in `_serve_dashboard()` (`src/brigade/fleet_hub.py:1073`). Node tokens do not open deck pages (same as today's HTML routes). The `?token=` login flow, cookie lifetime, and scope stay byte-for-byte unchanged. the short-lived login code/cookie redesign remains a separate follow-up and must not ship here.
- Network: tailnet-only. `--host` stays required. no new bind behavior, no TLS termination changes, no public deployment of any kind.
- Read-only: deck routes accept GET only. No POST handler, no CSRF surface beyond what GET-plus-no-writes implies, no cache poisoning interest given `Cache-Control: no-store`.
- Response headers: every deck response goes through `_send_html()` unchanged, so CSP (`default-src 'none'` with per-response nonces for the one inline script/style, `form-action 'self'`, `frame-ancestors 'none'`), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`, and `Vary: Cookie` are identical to today (`src/brigade/fleet_hub.py:1052`). The deck introduces no new header and no server banner change.
- Output escaping: every interpolated string passes through `html.escape(..., quote=True)`. Before escaping, free-text fields that humans control (station `name`, `nodes.label`) have C0/C1 control characters stripped and are capped at their validated lengths. this implements the control-character hardening concern for deck surfaces without touching the legacy boards. Repo names, run ids, seat/harness values arrive from validated event fields and still escape unconditionally.
- Secrets and private identifiers: never render or reflect the admin token, node tokens, the cookie value, holder tokens, or the `Authorization` header in any deck response, including error pages. Placeholders in this spec (`<STATION_A_NODE_ID>`, `<FLEET_TOKEN>`, `/etc/brigade/command-deck.json`) stand in for private values. Real host names and node ids belong only in the CT-local config.
- Error pages for deck routes use plain-text bodies like the existing 401/404 paths, escaped and secret-free.

## 9. Test-first plan

New file `tests/test_fleet_command_deck.py`, mirroring the fixture style of `tests/test_fleet_dashboard.py` (stdlib `http.client` against `fleet_hub.make_server` on port 0, obviously fake constants like `NODE_A = "11111111-..."`, seeded synthetic events). The first failing tests, written before implementation in this order:

1. `test_deck_route_requires_bearer_or_cookie`: `/deck` and `/deck/repos` answer 401 without credentials, with a wrong bearer, and with a junk cookie. a valid bearer renders 200 HTML with the standard security headers. the cookie-only request renders 200 while `/status` with the same cookie stays 401.
2. `test_deck_renders_configured_stations_in_fixed_order`: seed events from three configured nodes plus one stranger. assert the three station names appear in config order regardless of activity, and the stranger appears only inside the other-observers details block.
3. `test_station_labels_resolve_from_enrollment_and_config`: enroll a node with a label, configure a different name, assert config name wins. delete config name, assert enrollment label. neither case renders a bare UUID as primary text (assert the 12-char form never appears as station title).
4. `test_capacity_and_utilization_per_station`: seed 5 live runs on the capacity-6 station and 1 on a capacity-4 station. assert `5/6 busy`, `1/4 busy`, slot-total `6/14`. seed 7 live runs on capacity 6 and assert the `over` marker renders.
5. `test_terminal_runs_stay_out_of_operating_picture`: seed completed, failed, and interrupted runs. default page omits them from tiles. timeline lists them newest first. failed ones appear in the rail. `succeeded` never appears in the rail.
6. `test_unknown_observers_do_not_inflate_counts`: with strangers seeded, the status bar slot total counts only configured stations and the summary never contains the word `machines`.
7. `test_recent_outcome_timeline_bounded`: insert more terminal runs than `outcome_window` via `/events` batching. assert the timeline renders exactly the configured cap and the helper applies its LIMIT (call the module's bounded-query function directly against the fixture DB and assert row count).
8. `test_repo_view_collision_first`: seed a claim by station A with live runs on both A and B. assert `/deck/repos` lists that target first with the collision flag and names both parties. assert an expired claim is absent.
9. `test_no_secrets_in_any_deck_page`: for every deck route and credential combination, assert the token, cookie value, and a seeded holder token are absent from the body.
10. `test_control_characters_and_labels_are_escaped`: enroll a label containing a control character and angle brackets. assert the rendered label is stripped of control characters and entity-escaped. assert a hostile query string cannot break the page.

Pure-function tests accompany them (config parsing/validation table test, state vocabulary table test, collision predicate test, utilization formatter). After the suite passes locally: `ruff check`, `ruff format --check`, `mypy`, and the focused pytest selection, then the full `./scripts/verify` gate before review. Route dispatch additions need one regression assertion in the hub tests that legacy `/`, `/view/machines`, `/view/repos` responses are unchanged.

Browser acceptance (manual, or optionally scripted later with the existing `research` Playwright extra. not a CI gate):

- Viewports 1440x900 and 390x844 against a seeded dev hub (temp dir DB, fake tokens, per AGENTS.md never the real operator workspace).
- Checks: no horizontal overflow (`document.scrollingElement.scrollWidth <= document.scrollingElement.clientWidth` at both widths, with the longest plausible repo/run strings seeded). station order and capacities legible without scrolling on desktop. attention rail reachable within one thumb-scroll on phone. the navigation links and native details disclosure retain visible focus outlines and logical tab order. the page is fully informative with JavaScript disabled (verify by blocking scripts).

## 10. Rollout and rollback on beta main

Rollout rides the documented ref-swap procedure in `docs/runbooks/fleet-hub-hogwarts.md` (backup, `pipx install --force` at a reviewed ref, restart, health check). Sequence:

1. Land slice 1 (section 12) on beta main with `/deck` parallel. Nothing about `/` changes, so a deploy is additive: worst case is an ugly unused route.
2. Operator adds `--deck-config` to the hub unit's `ExecStart` with the CT-local config file and restarts once. Verify `GET /health` then `GET /deck` from a tailnet device.
3. Canary window: operator uses `/deck` as the working view alongside the legacy boards for at least one normal working day covering all three hosts. Approval criteria: section 3 questions answerable, station identities and capacities correct against reality, no overflow on the operator's phone and desktop, no 500s in `journalctl -u brigade-fleet.service`.
4. On approval, land root promotion ("/" serves the deck) as its own small ref. legacy boards remain at `/view/*`. Update the runbook's dashboard URL text in the same change.
5. Never deploy any part of this to public infrastructure. No Vercel, no tunnels, no reverse proxy off the tailnet.

Rollback: reinstall the previous reviewed ref per the runbook's rollback sequence and restart. Slice 1 makes no schema change (`PRAGMA user_version` stays 4), writes nothing, and adds no table, so rollback is a pure ref swap with zero data migration in either direction. If only the config file is wrong, fixing or removing `--deck-config` and restarting suffices. the deck degrades to the zero-station page rather than failing requests.

## 11. Acceptance criteria and dependency map

Measurable checks, all verifiable from the test plan and a live canary:

- AC1: `/deck` returns 200 for authorized callers, 401 otherwise, with the exact legacy security-header set, on both deck routes.
- AC2: Page shows exactly the three CT-configured station names in config order with capacities 6, 4, 4 rendered as `n/cap busy`. no real host name is hardcoded in repository files.
- AC3: With seeded strangers, slot totals and station counts exclude them. strangers appear only under Other observers, capped at 8.
- AC4: Default page contains zero terminal-run tiles. timeline shows at most `outcome_window` entries. rail shows failures within lookback, capped at 10.
- AC5: A cross-node live-on-claimed-target scenario renders the collision marker on the tile, a rail entry, and the first row of `/deck/repos` naming both parties.
- AC6: No response body contains the admin token, any node token, any cookie value, or any holder token (automated assertion).
- AC7: Labels with control characters and markup render stripped and escaped (automated assertion).
- AC8: Deck queries apply their stated limits (automated assertions against the helpers with oversized seeds).
- AC9: Browser acceptance passes at 1440x900 and 390x844 including the no-horizontal-overflow check and the JavaScript-disabled check.
- AC10: Keyboard-only operation reaches every link and disclosure in DOM order with visible focus. no JS-required interaction exists.
- AC11: `./scripts/verify` green on the implementation branch. legacy board tests pass unchanged.
- AC12: Rollback drill reasoning documented in the PR: ref swap restores prior behavior with no migration steps.

Dependency and issue map:

| Concern | Files | Related tracking |
| --- | --- | --- |
| Deck module, routes, config flag | `src/brigade/fleet_command_deck.py` (new), `src/brigade/fleet_hub.py`, `src/brigade/cli/fleet.py` | `<DECK-ISSUE>` (file at approval) |
| Deck tests | `tests/test_fleet_command_deck.py` (new), regression guard in existing fleet tests | `<DECK-ISSUE>` |
| Identity/claims data | `src/brigade/fleet_hub.py` tables (schema v4, unchanged) | #1125, #1141, #1150 |
| Request/connection constraints honored | `open_db` per-request connections, startup-only DDL | #1161 |
| Legacy boards (untouched, linked) | `src/brigade/fleet_dashboard.py`, `tests/test_fleet_dashboard.py` | #1124, PR #1145 |
| Bounded dashboard query cost (legacy and deck projection) | response caps land here. constant-time storage/query work stays separate | #1146 |
| Short-lived login codes/cookies | out of scope. interface constraint: reuse existing cookie checks | #1153 |
| Quotas/result bounds (hub-wide) | deck implements response caps only | #1151 |
| Control-character/server-banner hardening | deck implements label sanitization. banner unchanged | #1155 |
| Hub lock behavior | out of scope | #1159 |
| Campaigns | out of scope. deck consumes claims rows generically | #1128 |

## 12. Risks, unresolved decisions, first slice

Risks:

- Config drift: a re-enrolled node gets a new node_id only if revoked and re-added under a different id. if the operator rotates tokens keeping the same node_id, config survives. If a host is re-enrolled under a new id, its station greys to `not enrolled` until config updates. Mitigated by the greyed-station rule, chosen over hiding.
- Clock skew across hosts distorts elapsed and TTL displays the same way the current dashboard's do. no new skew handling in slice 1.
- Label trust: station names come from an operator-owned root-only file and admin-set labels. both are trusted-input surfaces by the existing trust model, sanitized for control characters and escaping regardless.
- Spool replay can make old events arrive late, briefly showing a run as fresher than truth. identical exposure exists today and self-corrects on the next real event.
- Capacity numbers are convention, not enforcement. the deck reports over-capacity occupancy rather than preventing it.

Defaults to approve with this specification:

- Use the configured full station name in rail and timeline lines. Do not add another abbreviation setting.
- Keep both legacy boards indefinitely after root promotion. Removal requires a later operator decision.
- Keep `outcome_window` bounded without pagination. Paginated history is a separate future feature if the operator asks for it.

Recommended first implementation slice (the whole of this feature before promotion):

1. `tests/test_fleet_command_deck.py` with tests 1 through 4 red.
2. `src/brigade/fleet_command_deck.py`: config loader/validator, capped query results, station/tile/rail/timeline/repo projections, `render_deck`.
3. Routing for `/deck` and `/deck/repos` plus the `--deck-config` flag and env override. tests 5 through 10 and pure-function tests green. legacy regressions green.
4. Lint, format, type, version-sync, coverage gates via `./scripts/verify`.
5. Deploy to the hub CT per section 10, run the canary window, then schedule root promotion as the follow-up ref.

No code, tests, or configuration are changed by this document. Nothing in it has been executed yet.
