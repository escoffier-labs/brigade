# Command Deck overview redesign (slice 1)

Date: 2026-10-18. Status: design approved in chat (decisions below), spec pending review.

## Goal

Make `/deck` answer "what is the fleet doing, and does anything need me" in one screen, in the style of the reference runner dashboard the owner shared: a left sidebar with a machine tree, four summary cards, an alerts strip, a working/queued pair, and a machines table with bars. Today the page is one ~3,000px column where the answer is spread across eight sections.

## Decisions already made

1. **Layout now, host metrics next.** Slice 1 uses only data the hub already stores. CPU, memory, disk free and uptime need new optional telemetry fields plus a Linux and a Windows collector; that is slice 2 with its own spec.
2. **Theme follows the system.** Add a light token set next to the dark one and switch on `prefers-color-scheme`. Every page that renders through `ui_theme.document()` gets both.
3. **#1749 lands first.** The redesign builds on its `--space-1`..`--space-8` scale and starts from main after #1749 merges.

## What the hub already knows (slice 1 inputs)

All from `DeckView` (`fleet_command_deck.py`) with no new queries except one:

| Need | Source |
|---|---|
| Machines, capacity, busy slots, active runs | `StationView` (`busy`, `station.capacity`, `tiles`) |
| Load 0..1, state, why idle, claims, free slots, observed time, OS | `ControlPlane.machines` (`MachineState`) |
| Ready queue | `ControlPlane.queue` (`QueueState`) |
| Things that need a person | `attention_items(view)`, `coverage_gaps(...)` |
| Cloud providers and leases | `CloudWorker` |
| Interactive sessions, recent outcomes | `InteractiveSession`, `outcomes` |
| Last run event per node | `fetch_last_heard` (events table) |

Telemetry age already reaches the deck as `MachineState.last_observed_at`.

One join gap needs fixing. Stations are keyed by `node_id`, while `MachineState.id` is the policy machine name. The routing snapshot builder (`fleet_hub_routing.py`, the `machines.append({...})` block) already holds `record["node_id"]` but does not emit it. Slice 1 adds `node_id` to that dict and an optional `node_id` field on `MachineState` (default `unknown`). This is an additive snapshot key, not a database schema change. The deck joins station to machine on `node_id`. Without one, the station's Load, Claims, State and Telemetry show `unknown`; there is no fallback match by label.

Staleness comes from the hub, not a second threshold in the deck. A machine is stale when its `unused_reason` is `stale-telemetry`; the hub decides that against the policy's `routing.telemetry_ttl_seconds`.

## Page structure

```
+----------------+---------------------------------------------------------+
| Brigade        | Fleet > Fleet overview            Updated 02:19 UTC  (R) |
| Fleet Hub      +---------------------------------------------------------+
|                | Fleet overview                                          |
| > Overview  3  | 3 machines · 22 slots · 5 cloud providers               |
|   Repos        |                                                         |
|   Work         | [Jobs 0 of 22] [Idle 22] [Offline 0] [Ready to run 0]    |
|   Roster       |                                                         |
|   Policy       | Active alerts (3)                                       |
|   Boards       |  ! FAILED skillet on linux-a   · external.failed  2h    |
| [filter.....]  |  ! UNKNOWN canary-repo on linux-b                 1d    |
|                |                                                         |
| MACHINES     3 | Activity                   | Ready queue                |
| v * Linux-a    |  Managed jobs / sessions   |  0 ready to run            |
|     0/10 idle  |                                                         |
| v * Linux-b    | Machines (3)                                            |
| v * Win-a      | Machine   Seats      Load  Claims  State     Telemetry  |
|                | linux-a   [====    ] 44%   0       idle      11s        |
| CLOUD        5 | linux-b   [        ] -     0       idle      1m         |
|   claude 0/4   | win-a     [    ]     0%    0       idle      1h 13m     |
|   ...          |                                                         |
|                | Cloud workers · Recent outcomes                         |
| hub heard 11s  | Control plane details (collapsed)                       |
+----------------+---------------------------------------------------------+
```

### Sidebar (`<aside>`, 248px, sticky; collapses to a top bar under 900px)

- Brand line: "Brigade Fleet Hub", sub-line "self-hosted".
- Page nav: Overview, Repos, Work (only when Worklore is enabled), Roster, Policy, Boards (`/view/machines`). Overview shows the machine count. The active page gets `aria-current="page"`.
- Filter input: hides tree rows that do not match. It needs a nonce'd inline script. Without JavaScript the input is not rendered (it is emitted by the script), so nothing is broken.
- MACHINES: one row per station: status dot, label, and a right-aligned summary (`3 busy`, `idle`, `stale`). Children: one row per active run (`repo · seat`, elapsed time), or nothing when idle.
- CLOUD: one row per provider with `leases/capacity` and its circuit state when not closed.
- Footer: "Hub telemetry 11s ago", the age of the newest telemetry row across machines.

### Main column

1. **Header bar:** breadcrumb, "Updated HH:MM UTC", and the existing auto-refresh. The verdict (`NEEDS ATTENTION (5)`, `COVERAGE UNKNOWN`, `ALL CLEAR`) becomes a pill next to the title.
2. **Summary cards (4):**
   - Working: busy slots, with a bar for busy/total and "of N slots".
   - Idle: free slots on machines whose telemetry is fresh.
   - Offline: machines that are not enrolled, report state `offline`, or have `unused_reason` `stale-telemetry` or `unknown-telemetry`. Subtitle: "stale or offline".
   - Ready to run: `QueueState` eligible queued count. Shows `unknown` when the queue section is absent, never `0`.
3. **Active alerts:** one card per item, warning-tinted. Sources, in order:
   - attention items (failed, unknown, stale runs)
   - actionable coordination warnings, excluding matching repo labels alone
   - machines with stale telemetry
   - coverage gaps ("no telemetry for: integrations")

   Each card: title, one line of detail, and "first observed <age> ago". When there are no alerts the section reads "No active alerts" and the verdict pill says why (all clear, or coverage unknown).
4. **Activity / Ready queue:** two columns.
   - Managed job rows: a blue dot, the repo, `harness badge · seat · machine`, elapsed time, and a link to the existing board. Parallel activity describes matching nonempty repo labels across machines. Shared modified paths warn separately when reported repository identity and dirty paths match.
   - Reported agent sessions group by node, harness, and session id. Show eight groups and fold the rest in native details, retaining every checkout membership. Presence may be idle or overlap managed jobs and never increases busy counts. At the 500-membership fetch limit, disclose that the list may be capped and the session total is unknown.
   - Ready queue counts come from `QueueState`. Equal ready and queued counts appear once. Unknown values stay unknown. Routine eligibility exclusions fold into readable reasons, while actionable blockers remain alerts.
5. **Machines table:** one row per station, joined to its `MachineState` by node id.

   | Column | Content |
   |---|---|
   | Machine | dot, label, OS |
   | Seats | segmented bar (busy blue, free green, one segment per slot up to 16, then a proportional bar), plus "2 busy, 8 idle" |
   | Load | percentage, with a thin bar |
   | Claims | count |
   | State | `state` and `unused_reason` |
   | Telemetry | age of the last telemetry, with the last run age as secondary text |

   The rows do not link anywhere in slice 1.
6. **Below the fold, compacted:**
   - Cloud workers: a grid of small cards.
   - Recent outcomes: consecutive identical `(state, repo, node)` rows collapse into one row with `×N`.
   - Control plane detail: the existing `render_control_plane` output inside a closed `<details>`.
   - Observed work groups: also inside a closed `<details>`, since they describe operator-configured metadata and not live activity.

### Fixes that ride along

- **Human ages.** `heard_age` returns `11s`, `4m`, `1h 13m` or `40d 2h` instead of raw seconds.
- **Telemetry versus run age.** The station "heard" line splits into telemetry age (`MachineState.last_observed_at`) and last run (`fetch_last_heard`). A host with fresh telemetry and no recent runs then shows "telemetry 1m · last run 40d" instead of "heard 3442035s ago".

## Theme

- `ui_theme.TOKENS` gains a light set inside `@media (prefers-color-scheme: light)`, and `color-scheme: light dark`.
- New semantic tokens used by the redesign:
  - `--working` (blue)
  - `--idle` (green, reuses `--ok`)
  - `--offline` (neutral grey)
  - `--warn-surface` and `--warn-line` (alert cards)
  - `--bar-track`
- Audit every page's `extra_css` for literal colors. Any literal color in a rendered page moves to a token, so the light scheme cannot leave dark-only text on a light surface. A test enforces this (see Testing).
- Light palette, close to the reference:
  - canvas `#f6f7f8`, surface `#ffffff`, line `#e3e6e8`
  - ink `#1d2327`, muted `#5f6b72`
  - working `#3b6fd8`, idle `#3f9a5d`, warn surface `#fbf3e6` with line `#e8cfa4`

## Code layout

- **New `src/brigade/fleet_deck_overview.py`** holds the page renderers: `render_sidebar(view, active, now)`, `render_summary(view)`, `render_alerts(view, now)`, `render_work_lists(view)`, `render_machine_table(view, now)`, `collapse_outcomes(outcomes)` and `format_age(seconds)`. It is pure functions over `DeckView` with no database access. Target under 750 lines.
- **New `src/brigade/fleet_deck_shell.py`** holds `deck_page(title, active, sidebar_html, body_html, nonce)`: the two-column shell for `/deck`, `/deck/repos`, `/deck/work`, `/deck/roster` and `/deck/policy`. Those pages keep their current bodies inside the new shell. Target under 250 lines.
- **`fleet_command_deck.render_deck`** shrinks to assembling the new sections. The old station cards and inline sections move out, which lowers the file below its current 1,953 lines; the ratchet only allows shrinking.
- **`ui_theme.py`** gets the light tokens and the shared shell CSS.
- **`fleet_hub_routing.py`** emits `node_id` on each machine snapshot record, and `MachineState` gains the optional field. This is the only change outside the renderers.
- The `/view/*` fleet boards and Center keep their current layouts and only gain the light theme.

## Failure handling

- **No control plane:** cards show `unknown` for Idle, Offline and Queued, Working still counts from stations, and an alert says the control-plane snapshot is missing.
- **Station with no `MachineState` for its `node_id`:** Load, Claims, State and Telemetry show `unknown`, and the machine counts as Offline only if it is not enrolled.
- **No stations:** the existing empty state, inside the new shell.
- **All text** goes through `_esc`. No user text reaches attributes without escaping, and the filter script reads `textContent` only.

## Testing

Renderer tests in `tests/test_fleet_deck_overview.py`, using synthetic `DeckView` objects:

- Card counts for busy, idle, stale, offline and not-enrolled mixes. Queued renders `unknown` when the queue section is absent.
- Alert ordering and sources: attention, collision, stale telemetry, coverage gap. Plus the empty state.
- The machines table joins stations to `MachineState` on `node_id`. A missing `node_id` gives `unknown` cells, not a label match. The seat bar's text equals its segments.
- The routing snapshot emits `node_id` per machine. Add a `fleet_hub_routing` test, and check that older snapshots without the key still parse.
- `format_age` boundaries: 0s, 59s, 60s, 3599s, 1 day and 40 days.
- `collapse_outcomes` merges only adjacent identical rows.
- Escaping: a repo named `<script>` renders inert in the sidebar, table and alerts.

Existing tests:

- `test_ui_theme_integration.py` (no unresolved custom properties) covers the new pages.
- `test_fleet_command_deck.py` assertions that pin old markup are updated deliberately, and each change is listed in the PR.

New theme test: no literal hex or rgb color appears in any page's CSS outside `TOKENS`.

Screenshots for the PR come from a local fixture server with synthetic data:

- `/deck` at 1440px and 390px, in both color schemes
- `/deck/repos` in light

Verification: the focused gate from #1749 plus the new test file, then `./scripts/verify` once through `brigade work verify run`.

## Reference app and what we take from it

The screenshot is the Runner Dashboard app from `aarondfrancis/fleet`, a Rust/Tauri macOS app that manages GitHub Actions runners over a tailnet. The repository has no license, so it is all rights reserved. We take layout and behavior ideas only. No code, CSS, scripts or assets are copied, and the PR says so.

Ideas adopted:

- **Alerts carry a `since` time, and missing data never clears one.**
  - Slice 1 derives `since` without new storage. Stale telemetry: `last_observed_at` plus the policy TTL. Run attention items: the event's `received_at`. Coverage gaps: shown as "ongoing".
  - A stale snapshot never turns an alert green.
- **Offline needs a grace period.** Their app waits 3 minutes before an offline alert. The hub's telemetry TTL already acts as that grace, so the deck adds no second timer.
- **Slice 2 notes, for its own spec:**
  - Collect boot id (reboot detection), cores, memory total and available, 1/5/15 load, CPU percent sampled over one second, uptime, and disk total/free for `/` and the home directory. A metric we cannot read is sent as absent, never as zero.
  - Low-disk alerts use hysteresis: raise below a free-percent threshold, clear only 2 points above it, so readings near the line don't flap.
  - Allow a one-hour per-machine maintenance mute, and keep a short per-machine event history.
  - Keep our push model: hosts post telemetry to the hub. The reference app pulls over SSH from one control machine, which does not fit a hub in a container.

## Out of scope

- Slice 2: host metrics (CPU, memory, disk free, uptime), the threshold alerts the reference shows, and the Windows collector.
- Machine detail pages. Rows are not clickable in slice 1.
- Changes to `/view/machines`, `/view/repos` and Center beyond the theme.
- Unread agent message counts (#1723/#1715).
