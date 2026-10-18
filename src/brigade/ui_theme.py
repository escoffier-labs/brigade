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

TOKENS = (
    """:root {
  color-scheme: dark;
  --canvas: #111617;
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
"""
    + "".join(f"  --space-{index}: {pixels}px;\n" for index, pixels in enumerate(SPACING, 1))
    + "}\n"
)

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
.empty { margin: 0; padding: var(--space-3); color: var(--muted); background: #141b1c; font-size: 12px; }
.callout { margin: var(--space-4) 0 0; padding: var(--space-3) var(--space-4); border: 1px solid var(--signal); color: var(--ink); font-size: 12px; }
.callout--error { border-color: #b0553a; }
.banner { margin: var(--space-4) 0 0; padding: var(--space-3) var(--space-4); border: 1px solid var(--signal); color: var(--ink); font-size: 12px; }
.banner--error { border-color: #b0553a; }
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
.roster-form select, .roster-form textarea, .roster-form input[type="text"], .roster-form input[type="search"], .roster-form input[type="number"] { width: 100%; min-height: 36px; padding: var(--space-2) 10px; border: 1px solid var(--line); background: var(--surface-raised); color: var(--ink); font: inherit; }
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
@media (max-width: 700px) {
  .deck-shell { padding: var(--space-3); }
  .masthead { align-items: start; flex-direction: column; }
  .header-meta, .page-meta { text-align: left; }
  th, td { padding: var(--space-2) 6px; font-size: 11px; }
}
"""

_HEAD_LINKS = (
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<meta name="theme-color" content="#111617">'
    '<meta name="application-name" content="Fleet Hub">'
    '<meta name="apple-mobile-web-app-title" content="Fleet Hub">'
    '<link rel="icon" type="image/x-icon" href="/favicon.ico">'
    '<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32x32.png">'
    '<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">'
    '<link rel="manifest" href="/site.webmanifest">'
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
) -> str:
    """Wrap an already-escaped *body* in the shared HTML shell.

    *title* and *as_of* are escaped here. *nonce* is stamped on the style and
    script tags so hub CSP headers keep working. *refresh_seconds* adds the
    meta refresh the Deck and the boards rely on; ``None`` omits it.
    """
    refresh_tag = f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">' if refresh_seconds else ""
    script_tag = f'<script nonce="{_esc(nonce)}">{script}</script>' if script else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"{refresh_tag}{_HEAD_LINKS}"
        f"<title>{_esc(title)}</title>"
        f'<style nonce="{_esc(nonce)}">{TOKENS}{BASE_CSS}{extra_css}</style>'
        f"{script_tag}"
        f'</head><body class="deck" data-as-of="{_esc(as_of)}">{body}</body></html>'
    )
