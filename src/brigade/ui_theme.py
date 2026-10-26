"""Shared visual system for every Brigade HTML surface (#1495).

The Command Deck, the fleet boards, and Center all render through
``document()`` and embed ``TOKENS`` plus ``BASE_CSS``. Page-specific rules
travel in ``extra_css``. The scale is a 4px base: panels pad 16px, labels sit
6px above controls, helper text sits 8px below, buttons are 40px tall.
"""

from __future__ import annotations

import html

# Pixel steps used below. Label/control (6px) and table row (10px) spacing
# are optical half-steps. Panels/gaps use 16, headings/fields/actions use 12,
# helpers/captions use 8, and buttons use 40. Controls have a 36px minimum.
SPACING = (4, 8, 12, 16, 20, 24, 32, 40)

_DARK_COLORS = """  --canvas: #0a0a0a;
  --surface: #111111;
  --surface-raised: #171717;
  --line: #262626;
  --line-quiet: #1c1c1c;
  --ink: #ededed;
  --muted: #a1a1a1;
  --faint: #8a8a8a;
  --signal: #f5a524;
  --signal-quiet: #2a2010;
  --ok: #22c55e;
  --bad: #f0706a;
  --working: #3b82f6;
  --idle: #22c55e;
  --offline: #6b6b6b;
  --warn-surface: #1f1a10;
  --warn-line: #5c4520;
  --bar-track: #262626;
  --surface-inset: #0d0d0d;
  --error-line: #b0553a;
  --on-accent: #ffffff;
  --on-signal: #0a0a0a;
  --control-line: #6b6b6b;
"""

_LIGHT_COLORS = """    --canvas: #f7f7f7;
    --surface: #fcfcfc;
    --surface-raised: #f2f2f2;
    --line: #e5e5e5;
    --line-quiet: #ececec;
    --ink: #171717;
    --muted: #5c5c5c;
    --faint: #6e6e6e;
    --signal: #a15c07;
    --signal-quiet: #fbf3e6;
    --ok: #15803d;
    --bad: #c8322a;
    --working: #2563eb;
    --idle: #16a34a;
    --offline: #8a8a8a;
    --warn-surface: #fbf3e6;
    --warn-line: #e8cfa4;
    --bar-track: #e5e5e5;
    --surface-inset: #f4f4f4;
    --error-line: #c0472a;
    --on-accent: #ffffff;
    --on-signal: #ffffff;
    --control-line: #808080;
"""

_SPACE_TOKENS = "".join(f"  --space-{index}: {pixels}px;\n" for index, pixels in enumerate(SPACING, 1))

_SHAPE_TOKENS = """  --radius-card: 8px;
  --radius-control: 6px;
  --font-sans: "Geist", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  --font-mono: "Geist Mono", ui-monospace, "SF Mono", Menlo, monospace;
"""

# The default sheet follows the system: dark values on :root, light values
# under prefers-color-scheme. DARK_TOKENS pins a page to dark.
TOKENS = (
    ":root {\n  color-scheme: light dark;\n"
    + _DARK_COLORS
    + _SPACE_TOKENS
    + _SHAPE_TOKENS
    + "}\n@media (prefers-color-scheme: light) {\n  :root {\n"
    + _LIGHT_COLORS
    + "  }\n}\n"
)
DARK_TOKENS = ":root {\n  color-scheme: dark;\n" + _DARK_COLORS + _SPACE_TOKENS + _SHAPE_TOKENS + "}\n"

BASE_CSS = """* { box-sizing: border-box; }
body.deck { margin: 0; min-width: 0; background: var(--canvas); color: var(--ink); font-family: var(--font-sans); font-size: 14px; line-height: 1.45; }
.deck { max-width: 100%; overflow-wrap: anywhere; }
.deck-shell { width: min(1480px, 100%); margin: 0 auto; padding: var(--space-5); }
.masthead { display: flex; align-items: end; justify-content: space-between; gap: var(--space-4); padding: 0 0 var(--space-4); border-bottom: 1px solid var(--line); }
.eyebrow, .page-kicker { margin: 0 0 var(--space-1); color: var(--muted); font-size: 11px; font-weight: 700; letter-spacing: .11em; text-transform: uppercase; }
h1, h2, h3, p { margin-top: 0; }
h1, .page-title { margin-bottom: var(--space-1); font-size: 22px; line-height: 1.1; letter-spacing: -.02em; }
h2 { margin-bottom: 0; font-size: 15px; line-height: 1.25; }
h3 { margin-bottom: 0; font-size: 13px; }
.header-meta, .page-meta { margin: 0; color: var(--muted); font-variant-numeric: tabular-nums; text-align: right; }
.station-meta { margin: var(--space-1) 0 0; color: var(--muted); font-size: 12px; }
.verdict { color: var(--signal); font-size: 12px; font-weight: 800; letter-spacing: .08em; }
nav, .deck-nav { display: flex; flex-wrap: wrap; gap: 0; margin: var(--space-4) 0; border-bottom: 1px solid var(--line); }
a { color: var(--ink); text-underline-offset: 3px; }
nav a, .deck-nav a { min-height: 36px; display: inline-flex; align-items: center; border: 0; border-bottom: 2px solid transparent; border-radius: 0; padding: 6px 2px; margin-right: var(--space-3); color: var(--muted); text-decoration: none; }
nav a:hover, .deck-nav a:hover { border-color: var(--signal); color: var(--ink); }
:focus-visible { outline: 2px solid var(--signal); outline-offset: 3px; }
nav a[aria-current="page"], .deck-nav a[aria-current="page"] { border-bottom-color: var(--ink); color: var(--ink); }
.panel, .repo-panel, details { min-width: 0; border: 1px solid var(--line); border-radius: var(--radius-card); background: var(--surface); }
.panel, .repo-panel { padding: var(--space-4); }
.panel > header, .repo-panel > header { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: var(--space-3); margin-bottom: var(--space-3); }
.panel-count { margin: 0; color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.panel + .panel { margin-top: var(--space-4); }
.panel + .repo-panel { margin-top: var(--space-4); }
.panel > h2, .repo-panel > h2, details > h3 { margin-bottom: var(--space-3); }
.empty { margin: 0; padding: var(--space-6) var(--space-3); color: var(--muted); text-align: center; font-size: 13px; }
.callout { margin: var(--space-4) 0 0; padding: var(--space-3) var(--space-4); border: 1px solid var(--signal); color: var(--ink); font-size: 12px; }
.callout--error { border-color: var(--error-line); }
.banner { margin: var(--space-4) 0 0; padding: var(--space-3) var(--space-4); border: 1px solid var(--signal); color: var(--ink); font-size: 12px; }
.banner--error { border-color: var(--error-line); }
.tile { min-width: 0; padding: var(--space-3); border: 1px solid var(--line); border-radius: var(--radius-card); background: var(--surface-raised); }
.tile--failed, .tile--awaiting-approval, .tile--stale { border-color: var(--signal); background: var(--signal-quiet); }
.tile-head { display: flex; align-items: start; justify-content: space-between; gap: var(--space-2); }
.state { margin: 0; color: var(--signal); font-size: 11px; font-weight: 800; letter-spacing: .06em; text-align: right; text-transform: uppercase; }
.chip, .chip-ok, .chip-bad, .chip-warn, .chip-muted { display: inline-block; padding: 2px var(--space-2); border: 1px solid var(--line); border-radius: 999px; font-size: 12px; font-weight: 500; letter-spacing: 0; white-space: nowrap; }
.chip-ok { border-color: var(--ok); color: var(--ok); }
.chip-bad { border-color: var(--bad); color: var(--bad); }
.chip-warn { border-color: var(--signal); color: var(--signal); }
.chip-muted { color: var(--muted); }
details { margin-top: var(--space-4); padding: var(--space-3) var(--space-4); color: var(--muted); }
details:target { border-color: var(--signal); }
summary { cursor: pointer; color: var(--ink); font-weight: 700; }
.table-wrap { position: relative; max-width: 100%; overflow-x: auto; }
table { width: 100%; table-layout: fixed; border-collapse: collapse; }
th, td { padding: 10px var(--space-3); border-bottom: 1px solid var(--line-quiet); overflow-wrap: anywhere; text-align: left; vertical-align: top; }
th { background: var(--surface-raised); color: var(--muted); font-size: 12px; font-weight: 500; letter-spacing: 0; text-transform: none; }
td { color: var(--ink); font-size: 13px; }
.flag { color: var(--signal); font-weight: 800; }
footer { margin-top: var(--space-5); color: var(--faint); font-size: 12px; }
footer a { margin-right: var(--space-3); color: var(--muted); }
label { color: var(--muted); font-size: 12px; }
input::placeholder, textarea::placeholder { color: var(--muted); opacity: 1; }
.roster-form label { display: grid; gap: 6px; min-width: 0; color: var(--ink); font-size: 13px; font-weight: 500; }
.roster-form label > .hint { grid-row: 3; margin-top: 2px; }
.roster-form input, .roster-form select, .roster-form textarea { min-width: 0; text-transform: none; }
.roster-form select, .roster-form textarea, .roster-form input[type="text"], .roster-form input[type="search"], .roster-form input[type="number"] { width: 100%; min-height: 36px; padding: var(--space-2) 10px; border: 1px solid var(--control-line); border-radius: var(--radius-control); background: var(--surface-raised); color: var(--ink); font: inherit; }
.roster-form textarea { min-height: 72px; }
.roster-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: var(--space-3) var(--space-4); }
.roster-grid + .roster-grid, .roster-grid + p, .roster-form > p { margin-top: var(--space-3); }
.hint { display: block; margin-top: var(--space-2); color: var(--muted); font-size: 11px; line-height: 1.4; text-transform: none; }
.roster-actions { display: grid; gap: var(--space-2); margin: var(--space-3) 0 0; }
.roster-actions label + button { margin-top: var(--space-1); }
.roster-actions label { display: grid; gap: 6px; }
.roster-actions button { min-height: var(--space-8); padding: 0 18px; border-radius: var(--radius-control); font: inherit; cursor: pointer; justify-self: start; }
.roster-actions .hint { margin-top: 0; }
.confirm-save { margin-top: var(--space-4); padding-top: var(--space-3); border-top: 1px solid var(--line); }
.shell { display: grid; grid-template-columns: 248px minmax(0, 1fr); min-height: 100vh; }
.shell-main { min-width: 0; }
.shell-sidebar { display: flex; flex-direction: column; position: sticky; top: 0; align-self: start; height: 100vh; overflow-y: auto; padding: var(--space-4); border-right: 1px solid var(--line-quiet); background: var(--surface); }
.brand { padding: var(--space-1) 0; }
.brand-name { margin: 0; font-size: 14px; font-weight: 600; }
.brand-sub { margin: 0; color: var(--muted); font-size: 12px; }
.shell-sidebar .deck-nav { flex-direction: column; flex-wrap: nowrap; gap: var(--space-1); margin: var(--space-4) 0; border-bottom: 0; }
.shell-sidebar .deck-nav a { display: flex; align-items: center; gap: var(--space-2); margin: 0; padding: 6px 10px; border: 0; border-radius: var(--radius-control); color: var(--muted); text-transform: capitalize; }
.shell-sidebar .deck-nav a[aria-current="page"] { background: var(--surface-raised); color: var(--ink); font-weight: 500; }
.nav-count { margin-left: auto; color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.deck-nav a[href="/deck/alerts"] .nav-count { color: var(--signal); }
.tree-filter input { width: 100%; min-height: 36px; padding: var(--space-2) 10px; border: 1px solid var(--control-line); border-radius: var(--radius-control); background: var(--surface-raised); color: var(--ink); font: inherit; }
.tree-heading { display: flex; justify-content: space-between; margin: var(--space-4) 0 var(--space-2); color: var(--muted); font-size: 11px; }
.tree-row[data-status="stale"] .tree-meta { color: var(--signal); }
.tree-row[data-status="offline"] .tree-meta { color: var(--muted); }
.tree-row[data-status="unknown"] .tree-meta { color: var(--muted); }
.tree-list, .tree-children { margin: 0; padding: 0; list-style: none; }
.tree-row[hidden] { display: none; }
.page-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-4); }
.page-head h1 { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-3); margin: 0; }
.page-sub { margin: var(--space-1) 0 0; color: var(--muted); }
.visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
.tree-line { display: flex; align-items: center; gap: var(--space-2); min-height: 28px; }
.tree-label { flex: 1; min-width: 0; font-weight: 650; }
.tree-meta { color: var(--muted); font-size: 12px; white-space: nowrap; }
.tree-children { margin: 0 0 var(--space-1) var(--space-1); padding-left: var(--space-3); border-left: 1px solid var(--line-quiet); }
.tree-run { display: flex; justify-content: space-between; gap: var(--space-2); color: var(--muted); font-size: 12px; }
.tree-run-name { min-width: 0; }
.tree-time { white-space: nowrap; font-variant-numeric: tabular-nums; }
.tree-empty { margin: 0; color: var(--faint); font-size: 12px; }
.mono { font-family: var(--font-mono); font-size: 12px; font-variant-numeric: tabular-nums; }
.icon { flex: 0 0 auto; vertical-align: middle; }
tbody tr:hover td { background: var(--surface-raised); }
.tag { display: inline-block; padding: 2px var(--space-2); border-radius: 999px; font-size: 12px; font-weight: 500; letter-spacing: 0; color: var(--muted); border: 1px solid var(--line); white-space: nowrap; }
.tag--warn { color: var(--signal); border-color: var(--warn-line); background: var(--signal-quiet); }
.shell-sidebar .deck-nav a:hover { background: var(--surface-raised); color: var(--ink); }
.resources { margin-top: auto; padding-top: var(--space-3); border-top: 1px solid var(--line); }
.resources a { display: flex; align-items: center; gap: var(--space-2); padding: 5px 10px; border-radius: var(--radius-control); color: var(--muted); text-decoration: none; }
.resources a > .icon:last-child { margin-left: auto; }
.resources a:hover { background: var(--surface-raised); color: var(--ink); }
.sidebar-meta { display: flex; white-space: nowrap; gap: var(--space-1); justify-content: space-between; margin: var(--space-2) 10px 0; color: var(--faint); font-size: 11px; }
.sidebar-version { overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.sidebar-telemetry { flex: none; }
.sidebar-meta--stale .sidebar-telemetry { color: var(--signal); }
.shell-main button[type="submit"], .fleet-controls button { background: var(--ink); color: var(--canvas); border: 1px solid var(--ink); border-radius: var(--radius-control); font-weight: 500; cursor: pointer; }
.shell-main button[value="preview"] { background: var(--surface); color: var(--ink); border-color: var(--line); }
.card-link { display: inline-flex; align-items: center; gap: var(--space-1); color: var(--muted); font-size: 12px; text-decoration: none; }
.card-link:hover { color: var(--ink); }
.card-link::after { content: "\\2192" / ""; margin-left: var(--space-1); }
.section-tabs { display: flex; flex-wrap: nowrap; overflow-x: auto; max-width: 100%; margin: 0; padding: var(--space-4) var(--space-4) 0; border-bottom: 1px solid var(--line); }
.section-tabs a { flex: 0 0 auto; white-space: nowrap; min-height: 40px; margin: 0 0 -1px; padding: var(--space-2) var(--space-3); border: 1px solid transparent; border-bottom: 1px solid var(--line); border-radius: var(--radius-control); border-bottom-left-radius: 0; border-bottom-right-radius: 0; }
.section-tabs a[aria-current="page"] { border-color: var(--line); border-bottom-color: var(--canvas); background: var(--canvas); color: var(--ink); }
.repository-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 1fr)); gap: var(--space-4); }
.repository-card { min-width: 0; padding: var(--space-4); border: 1px solid var(--line); border-radius: var(--radius-card); background: var(--surface); overflow-wrap: anywhere; }
.repository-card header { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: var(--space-2); padding-bottom: var(--space-3); margin-bottom: var(--space-3); border-bottom: 1px solid var(--line-quiet); }
.repository-card p { margin-bottom: 0; }

.attention-shortcut { display: flex; padding: var(--space-2) 10px; color: var(--signal); }
.alert-rows { padding: 0; margin: 0; list-style: none; }
.alert-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: var(--space-2) var(--space-3); padding: var(--space-3) 0; border-top: 1px solid var(--line-quiet); }
.alert-row h3 { margin: var(--space-1) 0; }
.alert-age, .alert-detail { margin: 0; color: var(--muted); font-size: 12px; }
.alert-summary { margin: var(--space-1) 0; font-size: 13px; }
.alert-guidance { margin: var(--space-2) 0; max-width: 75ch; font-size: 13px; }
.alert-action { align-self: center; max-width: 9rem; }
.alert-action a { display: inline-flex; align-items: center; min-height: 40px; }
.alert-action-note { display: block; color: var(--muted); font-size: 11px; }
.alert-evidence { grid-column: 1 / -1; margin: 0; padding: var(--space-2); }
.pager { display: flex; flex-wrap: wrap; gap: var(--space-2); margin: var(--space-4) 0; }
.pager a { display: inline-flex; align-items: center; min-height: 32px; padding: 0 var(--space-3); border: 1px solid var(--line); border-radius: var(--radius-control); background: var(--surface); color: var(--ink); text-decoration: none; }
.pager a:hover { background: var(--surface-raised); }
@media (max-width: 900px) {
  .shell { grid-template-columns: 1fr; }
  .shell-sidebar { position: static; height: auto; border-right: 0; border-bottom: 1px solid var(--line-quiet); }
  .shell-sidebar { padding: var(--space-2) var(--space-3); }
  .shell-sidebar .brand-sub { display: none; }
  .shell-sidebar .deck-nav a { padding: 6px; gap: 4px; font-size: 12px; }
  .shell-sidebar .deck-nav .icon { display: none; }
  .shell-sidebar .deck-nav { flex-direction: row; flex-wrap: wrap; margin: var(--space-3) 0 0; }
  .shell-sidebar .tree, .tree-filter, .resources, .sidebar-meta { display: none; }
}
@media (max-width: 700px) {
  .deck-shell { padding: var(--space-3); }
  .masthead { align-items: start; flex-direction: column; }
  .header-meta, .page-meta { text-align: left; }
  th, td { padding: var(--space-2) 6px; font-size: 11px; }
}
"""

# Main-column rules for the Alerts page (/deck/alerts) only.
ALERTS_CSS = """
.alerts-page .panel { margin-top: var(--space-4); }
@media (max-width: 600px) {
  .alerts-page .alert-row { grid-template-columns: minmax(0, 1fr); }
  .alerts-page .alert-action { max-width: none; display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2) var(--space-4); }
  .alerts-page .alert-action-note { flex-basis: 100%; }
}
"""

OVERVIEW_CSS = """
.overview > * + * { margin-top: var(--space-4); }
.overview-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); align-items: start; gap: var(--space-4); }
.overview-grid > .panel { min-width: 0; margin: 0; }
.overview-grid > .panel > header { padding-bottom: var(--space-3); border-bottom: 1px solid var(--line-quiet); }
.overview-grid > .panel:first-child, .overview-grid > .panel:nth-child(2) { grid-column: 1 / -1; }
.overview-grid > #machines-table { grid-column: 1 / -1; }
.overview-grid .activity-cards { grid-template-columns: repeat(auto-fit, minmax(min(100%, 260px), 1fr)); gap: var(--space-2); }
.overview-grid .activity-card { min-height: 0; padding: var(--space-2) var(--space-3); }
.overview-grid .activity-card .row-title { font-size: 14px; }
.overview-grid .panel h3 { margin-top: var(--space-3); }
@media (max-width: 900px) { .overview-grid { grid-template-columns: minmax(0, 1fr); } }
.overview .alert-row { padding: var(--space-2) 0; }
.overview .alert-row h3 { font-size: 13px; }
.overview .alert-age { display: inline; margin-right: var(--space-2); }
.overview .alert-evidence-inline, .overview .alert-evidence-inline summary { display: inline; font-size: 12px; }
.overview .alert-evidence-inline { margin: 0; padding: 0; border: 0; border-radius: 0; background: transparent; }
.overview .alert-evidence-inline summary { cursor: pointer; color: var(--muted); font-weight: 400; text-decoration: underline; text-underline-offset: 2px; }
.verdict-status { font-size: 11px; font-weight: 800; }
.verdict-status--attention { color: var(--signal); }
.verdict-status--unknown { color: var(--muted); }
.verdict-status--clear { color: var(--ok); }
.refresh-link { margin-left: var(--space-2); color: var(--muted); }
.cards { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0; border: 1px solid var(--line); border-radius: var(--radius-card); background: var(--surface); overflow: hidden; }
.card { min-width: 0; padding: var(--space-4); border: 0; border-radius: 0; border-left: 1px solid var(--line); background: var(--surface); }
.card:first-child { border-left: 0; }
.card-title { margin: 0; color: var(--muted); font-size: 12px; font-weight: 500; }
.card-value { color: var(--ink); margin: var(--space-1) 0 0; font-size: 24px; font-weight: 600; font-variant-numeric: tabular-nums; }
.card-sub { margin: 0 0 var(--space-2); color: var(--muted); font-size: 12px; }
.reported-counts { display: flex; flex-wrap: wrap; gap: var(--space-4); }
.reported-counts .card { flex: 1 1 160px; padding: 0; border: 0; }
.reported-counts .card-title, .reported-counts .card-value { font-size: 13px; }
progress { appearance: none; -webkit-appearance: none; display: block; width: 100%; height: 6px; border: 0; border-radius: 999px; overflow: hidden; background: var(--bar-track); color: var(--working); }
progress::-webkit-progress-bar { background: var(--bar-track); }
progress::-webkit-progress-value { background: var(--working); }
progress::-moz-progress-bar { background: var(--working); }
.bar { margin-top: var(--space-2); }
.bar--thin { height: 4px; margin-top: var(--space-1); }
.alert-line { color: var(--muted); }
.panel-help { margin-top: var(--space-1); color: var(--muted); font-size: 12px; }
.panel-help summary { cursor: pointer; }
.panel-help p { margin: var(--space-1) 0 0; max-width: 60ch; }
.alert-line strong { color: var(--signal); }
.split { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-4); }
.split > .panel + .panel { margin-top: 0; }
.rows { display: grid; margin: 0; padding: 0; list-style: none; }
.row { display: flex; align-items: flex-start; gap: var(--space-3); padding: var(--space-3) 0; border-top: 1px solid var(--line-quiet); }
.row:first-child { padding-top: 0; border-top: 0; }
.row-body { flex: 1; min-width: 0; }
.row-title, .row-meta { margin: 0; }
.row-meta { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); color: var(--muted); font-size: 12px; }
.row-time { color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.activity-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 1fr)); gap: var(--space-3); margin: var(--space-3) 0 var(--space-4); padding: 0; list-style: none; }
.activity-cards .activity-card { display: grid; grid-template-rows: auto auto auto 1fr auto; align-content: start; gap: 6px; min-width: 0; min-height: 180px; padding: var(--space-3); border: 1px solid var(--line); border-radius: var(--radius-card); background: var(--surface-raised); }
.activity-cards [data-activity-kind="reported"] { border-style: dashed; background: var(--surface); }
.activity-card .activity-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: var(--space-2); }
.activity-card .activity-kind { color: var(--muted); font-size: 11px; }
.activity-card .activity-state { display: inline-flex; align-items: baseline; gap: 6px; max-width: 100%; border: 1px solid currentColor; border-radius: 999px; padding: 2px var(--space-2); color: var(--signal); font-size: 11px; font-weight: 600; }
.activity-card .activity-state::before { content: ""; flex: 0 0 6px; width: 6px; height: 6px; border-radius: 999px; background: currentColor; }
.activity-card .activity-state[data-state="running"] { color: var(--working); }
.activity-card .activity-state[data-state="completed"] { color: var(--ok); }
.activity-card .activity-state[data-state="failed"] { color: var(--bad); }
.activity-card .activity-state[data-state="queued"], .activity-card .activity-state[data-state="cancelled"] { color: var(--muted); }
.activity-card .row-title { font-size: 15px; font-weight: 650; line-height: 1.3; }
.activity-card .row-title a { text-decoration-thickness: 1px; text-decoration-color: var(--line); }
.activity-card .row-title a:hover { text-decoration-color: currentColor; }
.activity-card .activity-fact { margin: 0; font-size: 13px; }
.activity-card .activity-footer { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: var(--space-2); min-width: 0; }
.activity-card .activity-age { color: var(--faint); font-size: 11px; font-variant-numeric: tabular-nums; }
.activity-card .activity-footer > details { min-width: 0; margin: 0; padding: 0; border: 0; background: transparent; font-size: 12px; }
.activity-card .activity-footer > details[open] { flex-basis: 100%; }
.activity-card .activity-footer summary { min-height: 28px; padding: 4px 0; color: var(--muted); font-weight: 500; }
.activity-card details p { margin: var(--space-2) 0 0; }
.activity-history > .station-meta { margin-top: var(--space-2); }
@media (max-width: 480px) {
  .activity-cards { grid-template-columns: minmax(0, 1fr); }
}
.machine-table { min-width: 640px; }
.session-table { min-width: 480px; }
.machine-table th:nth-child(1) { width: 24%; }
.machine-table th:nth-child(2) { width: 28%; }
.machine-table th:nth-child(3) { width: 18%; }
.machine-table th:nth-child(4) { width: 16%; }
.machine-table th:nth-child(5) { width: 14%; text-align: right; }
.machine-table--no-notes th:nth-child(1) { width: 30%; }
.machine-table--no-notes th:nth-child(2) { width: 34%; }
.machine-table--no-notes th:nth-child(3) { width: 20%; }
.machine-table--no-notes th:nth-child(4) { width: 16%; text-align: right; }
.session-table th:nth-child(n) { width: auto; }
.machine-name { color: var(--ink); font-weight: 650; }
.sub-inline { margin-left: var(--space-2); color: var(--faint); font-size: 12px; }
.seen { text-align: right; white-space: nowrap; }
.seen--stale { color: var(--signal); }
.seat-legend { margin: 0; color: var(--muted); font-size: 12px; }
.legend-swatch { display: inline-block; width: 6px; height: 10px; margin: 0 var(--space-1) 0 var(--space-3); border-radius: 2px; vertical-align: -1px; }
.legend-swatch:first-child { margin-left: 0; }
.legend-swatch--busy { background: var(--working); }
.legend-swatch--free { background: var(--idle); }
.legend-swatch--off { box-shadow: inset 0 0 0 1px var(--offline); }
.seats { display: inline-flex; gap: 2px; margin-right: var(--space-2); vertical-align: middle; }
.seg { width: 8px; height: 12px; border-radius: 2px; }
.seg--busy { background: var(--working); }
.seg--free { background: var(--idle); }
.seg--off { background: transparent; box-shadow: inset 0 0 0 1px var(--offline); }
.seats--scaled { gap: 1px; }
.seat-text { color: var(--muted); }
@media (max-width: 900px) {
  .page-head { flex-direction: column; }
  .cards { grid-template-columns: repeat(3, minmax(0, 1fr)); }


  .split { grid-template-columns: 1fr; }
}
@media (max-width: 480px) {
  .cards { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .cards .card { padding: var(--space-2); }
  .cards .card-sub { font-size: 11px; }
}
"""

_HEAD_LINKS = (
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<meta name="application-name" content="Fleet Hub">'
    '<meta name="apple-mobile-web-app-title" content="Fleet Hub">'
    '<link rel="icon" type="image/x-icon" href="/favicon.ico">'
    '<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32x32.png">'
    '<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">'
    '<link rel="manifest" href="/site.webmanifest">'
)


_THEME_COLOR_DARK = '<meta name="theme-color" content="#0a0a0a">'
_THEME_COLOR_AUTO = (
    '<meta name="theme-color" content="#0a0a0a" media="(prefers-color-scheme: dark)">'
    '<meta name="theme-color" content="#f7f7f7" media="(prefers-color-scheme: light)">'
)


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def document(
    title: str,
    nonce: str,
    body: str,
    *,
    as_of: str,
    refresh_seconds: int | None = None,
    extra_css: str = "",
    script: str = "",
    scheme: str = "auto",
) -> str:
    """Wrap an already-escaped *body* in the shared HTML shell.

    *title* and *as_of* are escaped here. *nonce* is stamped on the style and
    script tags so hub CSP headers keep working. *refresh_seconds* adds the
    meta refresh the Deck and the boards rely on; ``None`` omits it. *scheme*
    is ``"auto"`` (dark, with light under ``prefers-color-scheme: light``) or
    ``"dark"`` (dark only, for pages whose own CSS still carries literal colors).
    """
    if scheme not in ("auto", "dark"):
        raise ValueError(f"unknown color scheme {scheme!r}: use 'auto' or 'dark'")
    tokens = DARK_TOKENS if scheme == "dark" else TOKENS
    theme_color = _THEME_COLOR_DARK if scheme == "dark" else _THEME_COLOR_AUTO
    refresh_tag = f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">' if refresh_seconds else ""
    script_tag = f'<script nonce="{_esc(nonce)}">{script}</script>' if script else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"{refresh_tag}{theme_color}{_HEAD_LINKS}"
        f"<title>{_esc(title)}</title>"
        f'<style nonce="{_esc(nonce)}">{tokens}{BASE_CSS}{extra_css}</style>'
        f"{script_tag}"
        f'</head><body class="deck" data-as-of="{_esc(as_of)}">{body}</body></html>'
    )
