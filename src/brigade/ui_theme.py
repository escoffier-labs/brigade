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

_DARK_COLORS = """  --canvas: #111617;
  --surface: #182022;
  --surface-raised: #202a2c;
  --line: #3a4849;
  --line-quiet: #293536;
  --ink: #e5ece8;
  --muted: #a4b1ad;
  --faint: #75837f;
  --signal: #d5ab69;
  --signal-quiet: #382f22;
  --ok: #6fbf8e;
  --bad: #d67c6a;
  --working: #6a9bf0;
  --idle: var(--ok);
  --offline: #75837f;
  --warn-surface: #2a2419;
  --warn-line: #6d5635;
  --bar-track: #293536;
  --surface-inset: #141b1c;
  --error-line: #b0553a;
  --on-accent: #ffffff;
  --on-signal: #111617;
  --control-line: #687879;
"""

_LIGHT_COLORS = """    --canvas: #f6f7f8;
    --surface: #ffffff;
    --surface-raised: #f0f2f3;
    --line: #e3e6e8;
    --line-quiet: #eceef0;
    --ink: #1d2327;
    --muted: #5f6b72;
    --faint: #636e74;
    --signal: #8a5d14;
    --signal-quiet: #fbf3e6;
    --ok: #2f7d4a;
    --bad: #b3402c;
    --working: #3b6fd8;
    --idle: #3f9a5d;
    --offline: #8a959b;
    --warn-surface: #fbf3e6;
    --warn-line: #e8cfa4;
    --bar-track: #e3e6e8;
    --surface-inset: #eef1f2;
    --error-line: #c0472a;
    --on-accent: #ffffff;
    --on-signal: #ffffff;
    --control-line: #7d878d;
"""

_SPACE_TOKENS = "".join(f"  --space-{index}: {pixels}px;\n" for index, pixels in enumerate(SPACING, 1))

# The default sheet follows the system: dark values on :root, light values
# under prefers-color-scheme. DARK_TOKENS pins a page to dark.
TOKENS = (
    ":root {\n  color-scheme: light dark;\n"
    + _DARK_COLORS
    + _SPACE_TOKENS
    + "}\n@media (prefers-color-scheme: light) {\n  :root {\n"
    + _LIGHT_COLORS
    + "  }\n}\n"
)
DARK_TOKENS = ":root {\n  color-scheme: dark;\n" + _DARK_COLORS + _SPACE_TOKENS + "}\n"

BASE_CSS = """* { box-sizing: border-box; }
body.deck {
  margin: 0;
  min-width: 0;
  background: var(--canvas);
  color: var(--ink);
  font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 14px;
  line-height: 1.45;
}
.deck { max-width: 100%; overflow-wrap: anywhere; }
.deck-shell { width: min(1480px, 100%); margin: 0 auto; padding: var(--space-5); }
.masthead {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: var(--space-4);
  padding: 0 0 var(--space-4);
  border-bottom: 1px solid var(--line);
}
.eyebrow, .page-kicker { margin: 0 0 var(--space-1); color: var(--muted); font-size: 11px; font-weight: 700; letter-spacing: .11em; text-transform: uppercase; }
h1, h2, h3, p { margin-top: 0; }
h1, .page-title { margin-bottom: var(--space-1); font-size: 22px; line-height: 1.1; letter-spacing: -.02em; }
h2 { margin-bottom: 0; font-size: 15px; line-height: 1.25; }
h3 { margin-bottom: 0; font-size: 13px; }
.header-meta, .page-meta { margin: 0; color: var(--muted); font-variant-numeric: tabular-nums; text-align: right; }
.station-meta { margin: var(--space-1) 0 0; color: var(--muted); font-size: 12px; }
.verdict { color: var(--signal); font-size: 12px; font-weight: 800; letter-spacing: .08em; }
nav, .deck-nav { display: flex; flex-wrap: wrap; gap: var(--space-2); margin: var(--space-4) 0; }
a { color: var(--ink); text-underline-offset: 3px; }
nav a, .deck-nav a { min-height: 36px; display: inline-flex; align-items: center; border: 1px solid var(--line); padding: 6px 10px; color: var(--muted); text-decoration: none; }
nav a:hover, .deck-nav a:hover { border-color: var(--signal); color: var(--ink); }
:focus-visible { outline: 2px solid var(--signal); outline-offset: 3px; }
nav a[aria-current="page"], .deck-nav a[aria-current="page"] { border-color: var(--signal); color: var(--ink); }
.panel, .repo-panel, details {
  min-width: 0;
  border: 1px solid var(--line-quiet);
  background: var(--surface);
}
.panel, .repo-panel { padding: var(--space-4); }
.panel > header, .repo-panel > header { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-3); margin-bottom: var(--space-3); }
.panel-count { margin: 0; color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.panel + .panel { margin-top: var(--space-4); }
.panel + .repo-panel { margin-top: var(--space-4); }
.panel > h2, .repo-panel > h2, details > h3 { margin-bottom: var(--space-3); }
.empty { margin: 0; padding: var(--space-3); color: var(--muted); background: var(--surface-inset); font-size: 12px; }
.callout { margin: var(--space-4) 0 0; padding: var(--space-3) var(--space-4); border: 1px solid var(--signal); color: var(--ink); font-size: 12px; }
.callout--error { border-color: var(--error-line); }
.banner { margin: var(--space-4) 0 0; padding: var(--space-3) var(--space-4); border: 1px solid var(--signal); color: var(--ink); font-size: 12px; }
.banner--error { border-color: var(--error-line); }
.tile { min-width: 0; padding: var(--space-3); border: 1px solid var(--line); border-left: 3px solid var(--signal); background: var(--surface-raised); }
.tile--failed, .tile--awaiting-approval, .tile--stale { border-color: var(--signal); background: var(--signal-quiet); }
.tile-head { display: flex; align-items: start; justify-content: space-between; gap: var(--space-2); }
.state { margin: 0; color: var(--signal); font-size: 11px; font-weight: 800; letter-spacing: .06em; text-align: right; text-transform: uppercase; }
.chip, .chip-ok, .chip-bad, .chip-warn, .chip-muted { display: inline-block; padding: 2px var(--space-2); border: 1px solid var(--line); border-radius: 999px; font-size: 11px; font-weight: 700; letter-spacing: .04em; white-space: nowrap; }
.chip-ok { border-color: var(--ok); color: var(--ok); }
.chip-bad { border-color: var(--bad); color: var(--bad); }
.chip-warn { border-color: var(--signal); color: var(--signal); }
.chip-muted { color: var(--muted); }
details { margin-top: var(--space-4); padding: var(--space-3) var(--space-4); color: var(--muted); }
details:target { border-color: var(--signal); }
summary { cursor: pointer; color: var(--ink); font-weight: 700; }
.table-wrap { max-width: 100%; overflow-x: auto; }
table { width: 100%; table-layout: fixed; border-collapse: collapse; }
th, td { padding: 10px var(--space-3); border-bottom: 1px solid var(--line-quiet); overflow-wrap: anywhere; text-align: left; vertical-align: top; }
th { color: var(--muted); font-size: 11px; letter-spacing: .06em; text-transform: uppercase; }
td { color: var(--muted); font-size: 12px; }
.flag { color: var(--signal); font-weight: 800; }
footer { margin-top: var(--space-5); color: var(--faint); font-size: 12px; }
footer a { margin-right: var(--space-3); color: var(--muted); }
label { color: var(--muted); font-size: 12px; }
input::placeholder, textarea::placeholder { color: var(--muted); opacity: 1; }
.roster-form label { display: grid; gap: 6px; min-width: 0; color: var(--muted); font-size: 12px; text-transform: uppercase; }
.roster-form label > .hint { grid-row: 3; margin-top: 2px; }
.roster-form input, .roster-form select, .roster-form textarea { min-width: 0; text-transform: none; }
.roster-form select, .roster-form textarea, .roster-form input[type="text"], .roster-form input[type="search"], .roster-form input[type="number"] { width: 100%; min-height: 36px; padding: var(--space-2) 10px; border: 1px solid var(--control-line); background: var(--surface-raised); color: var(--ink); font: inherit; }
.roster-form textarea { min-height: 72px; }
.roster-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: var(--space-3) var(--space-4); }
.roster-grid + .roster-grid, .roster-grid + p, .roster-form > p { margin-top: var(--space-3); }
.hint { display: block; margin-top: var(--space-2); color: var(--muted); font-size: 11px; line-height: 1.4; text-transform: none; }
.roster-actions { display: grid; gap: var(--space-2); margin: var(--space-3) 0 0; }
.roster-actions label + button { margin-top: var(--space-1); }
.roster-actions label { display: grid; gap: 6px; }
.roster-actions button { min-height: var(--space-8); padding: 0 18px; border: 1px solid var(--signal); background: var(--signal-quiet); color: var(--ink); font: inherit; font-weight: 700; cursor: pointer; justify-self: start; }
.roster-actions .hint { margin-top: 0; }
.confirm-save { margin-top: var(--space-4); padding-top: var(--space-3); border-top: 1px solid var(--line); }
.shell { display: grid; grid-template-columns: 248px minmax(0, 1fr); min-height: 100vh; }
.shell-main { min-width: 0; }
.shell-sidebar { position: sticky; top: 0; align-self: start; height: 100vh; overflow-y: auto; padding: var(--space-4); border-right: 1px solid var(--line-quiet); background: var(--surface); }
.brand { padding: var(--space-1) 0; }
.brand-name { margin: 0; font-size: 15px; font-weight: 750; }
.brand-sub { margin: 0; color: var(--muted); font-size: 12px; }
.shell-sidebar .deck-nav { flex-direction: column; flex-wrap: nowrap; gap: var(--space-1); margin: var(--space-4) 0; }
.shell-sidebar .deck-nav a { justify-content: space-between; border-color: transparent; text-transform: capitalize; }
.shell-sidebar .deck-nav a[aria-current="page"] { border-color: var(--line); background: var(--surface-raised); color: var(--ink); }
.nav-count { margin-left: var(--space-2); color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.tree-filter input { width: 100%; min-height: 36px; padding: var(--space-2) 10px; border: 1px solid var(--control-line); background: var(--surface-raised); color: var(--ink); font: inherit; }
.tree-heading { display: flex; justify-content: space-between; margin: var(--space-4) 0 var(--space-2); color: var(--muted); font-size: 11px; letter-spacing: .08em; text-transform: uppercase; }
.tree-list, .tree-children { margin: 0; padding: 0; list-style: none; }
.tree-row[hidden] { display: none; }
.tree-line { display: flex; align-items: center; gap: var(--space-2); min-height: 28px; }
.tree-label { flex: 1; min-width: 0; font-weight: 650; }
.tree-meta { color: var(--muted); font-size: 12px; white-space: nowrap; }
.tree-children { margin: 0 0 var(--space-1) var(--space-1); padding-left: var(--space-3); border-left: 1px solid var(--line-quiet); }
.tree-run { display: flex; justify-content: space-between; gap: var(--space-2); color: var(--muted); font-size: 12px; }
.tree-run-name { min-width: 0; }
.tree-time { white-space: nowrap; font-variant-numeric: tabular-nums; }
.tree-empty { margin: 0; color: var(--faint); font-size: 12px; }
.sidebar-foot { margin: var(--space-4) 0 0; color: var(--faint); font-size: 12px; }
.dot { display: inline-block; flex: 0 0 auto; width: 8px; height: 8px; border-radius: 999px; background: var(--offline); }
.dot--working { background: var(--working); }
.dot--idle { background: var(--idle); }
.dot--stale { background: var(--signal); }
.dot--offline { background: var(--offline); }
.dot--limited { background: var(--offline); }
.dot--unknown { background: var(--faint); }
@media (max-width: 900px) {
  .shell { grid-template-columns: 1fr; }
  .shell-sidebar { position: static; height: auto; border-right: 0; border-bottom: 1px solid var(--line-quiet); }
  .shell-sidebar .deck-nav { flex-direction: row; flex-wrap: wrap; margin: var(--space-3) 0 0; }
  .shell-sidebar .tree, .tree-filter, .sidebar-foot { display: none; }
}
@media (max-width: 700px) {
  .deck-shell { padding: var(--space-3); }
  .masthead { align-items: start; flex-direction: column; }
  .header-meta, .page-meta { text-align: left; }
  th, td { padding: var(--space-2) 6px; font-size: 11px; }
}
"""

# Main-column rules for the Fleet overview page (/deck) only.
OVERVIEW_CSS = """
.overview > * + * { margin-top: var(--space-4); }
.page-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-4); }
.crumbs { margin: 0 0 var(--space-1); color: var(--muted); font-size: 12px; }
.page-head h1 { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-3); margin: 0; }
.page-sub { margin: var(--space-1) 0 0; color: var(--muted); }
.verdict-pill { padding: 2px var(--space-3); border: 1px solid var(--line); border-radius: 999px; font-size: 11px; font-weight: 800; letter-spacing: .06em; }
.verdict-pill--attention { border-color: var(--warn-line); background: var(--warn-surface); color: var(--signal); }
.verdict-pill--unknown { color: var(--muted); }
.verdict-pill--clear { border-color: var(--ok); color: var(--ok); }
.refresh-link { margin-left: var(--space-2); color: var(--muted); }
.cards { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--space-4); }
.card { min-width: 0; padding: var(--space-4); border: 1px solid var(--line-quiet); background: var(--surface); }
.card-title { margin: 0; color: var(--muted); font-size: 12px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }
.card-value { margin: var(--space-1) 0 0; font-size: 28px; font-weight: 750; font-variant-numeric: tabular-nums; }
.card-sub { margin: 0 0 var(--space-2); color: var(--muted); font-size: 12px; }
.card--working .card-value { color: var(--working); }
.card--idle .card-value { color: var(--idle); }
.card--offline .card-value { color: var(--offline); }
progress { appearance: none; -webkit-appearance: none; display: block; width: 100%; height: 6px; border: 0; border-radius: 999px; overflow: hidden; background: var(--bar-track); color: var(--working); }
progress::-webkit-progress-bar { background: var(--bar-track); }
progress::-webkit-progress-value { background: var(--working); }
progress::-moz-progress-bar { background: var(--working); }
.bar { margin-top: var(--space-2); }
.bar--thin { height: 4px; margin-top: var(--space-1); }
.alerts { display: grid; gap: var(--space-2); margin: 0; padding: 0; list-style: none; }
.alert { display: flex; gap: var(--space-3); padding: var(--space-3); border: 1px solid var(--warn-line); background: var(--warn-surface); }
.alert-icon { flex: 0 0 auto; width: 20px; height: 20px; border-radius: 999px; background: var(--signal); color: var(--on-signal); font-weight: 800; line-height: 20px; text-align: center; }
.alert-body { min-width: 0; }
.alert-title, .alert-detail, .alert-since { margin: 0; }
.alert-kind { color: var(--signal); font-size: 11px; font-weight: 800; letter-spacing: .06em; text-transform: uppercase; }
.alert-detail { color: var(--muted); font-size: 12px; }
.alert-since { color: var(--faint); font-size: 12px; }
.split { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-4); }
.split > .panel + .panel { margin-top: 0; }
.rows { display: grid; margin: 0; padding: 0; list-style: none; }
.row { display: flex; align-items: flex-start; gap: var(--space-3); padding: var(--space-3) 0; border-top: 1px solid var(--line-quiet); }
.row:first-child { padding-top: 0; border-top: 0; }
.row .dot { margin-top: 6px; }
.row-body { flex: 1; min-width: 0; }
.row-title, .row-meta { margin: 0; }
.row-meta { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); color: var(--muted); font-size: 12px; }
.row-time { color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.machine-table { min-width: 640px; }
.session-table { min-width: 480px; }
.machine-table th:nth-child(1) { width: 24%; }
.machine-table th:nth-child(2) { width: 26%; }
.machine-table th:nth-child(3) { width: 12%; }
.machine-table th:nth-child(4) { width: 8%; }
.machine-table th:nth-child(5) { width: 15%; }
.machine-table th:nth-child(6) { width: 15%; }
.session-table th:nth-child(n) { width: auto; }
.machine-name { color: var(--ink); font-weight: 650; }
.sub { display: block; color: var(--faint); font-size: 11px; }
.seats { display: inline-flex; gap: 2px; margin-right: var(--space-2); vertical-align: middle; }
.seg { width: 8px; height: 12px; border-radius: 2px; }
.seg--busy { background: var(--working); }
.seg--free { background: var(--idle); }
.seg--off { background: transparent; box-shadow: inset 0 0 0 1px var(--offline); }
.seats--scaled { gap: 1px; }
.seat-text { color: var(--muted); }
@media (max-width: 900px) {
  .page-head { flex-direction: column; }
  .cards { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .split { grid-template-columns: 1fr; }
}
@media (max-width: 480px) {
  .cards { grid-template-columns: 1fr; }
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


_THEME_COLOR_DARK = '<meta name="theme-color" content="#111617">'
_THEME_COLOR_AUTO = (
    '<meta name="theme-color" content="#111617" media="(prefers-color-scheme: dark)">'
    '<meta name="theme-color" content="#f6f7f8" media="(prefers-color-scheme: light)">'
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
