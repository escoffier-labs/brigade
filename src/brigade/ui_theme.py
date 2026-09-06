"""Shared visual system for every Brigade HTML surface (#1495).

The Command Deck, the fleet boards, and Center all render through
``document()`` and embed ``TOKENS`` plus ``BASE_CSS``. Page-specific rules
travel in ``extra_css``. The scale is a 4px base: panels pad 16px, labels sit
6px above controls, helper text sits 8px below, buttons are 40px tall.
"""

from __future__ import annotations

import html

TOKENS = """:root {
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
}
"""

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
.deck-shell { width: min(1480px, 100%); margin: 0 auto; padding: 20px; }
.masthead {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 16px;
  padding: 0 0 16px;
  border-bottom: 1px solid var(--line);
}
.eyebrow { margin: 0 0 4px; color: var(--faint); font-size: 11px; font-weight: 700; letter-spacing: .11em; text-transform: uppercase; }
h1, h2, h3, p { margin-top: 0; }
h1 { margin-bottom: 4px; font-size: 22px; line-height: 1.1; letter-spacing: -.02em; }
h2 { margin-bottom: 0; font-size: 15px; line-height: 1.25; }
h3 { margin-bottom: 0; font-size: 13px; }
.header-meta { margin: 0; color: var(--muted); font-variant-numeric: tabular-nums; text-align: right; }
.station-meta { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
.verdict { color: var(--signal); font-size: 12px; font-weight: 800; letter-spacing: .08em; }
nav { display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0; }
a { color: var(--ink); text-underline-offset: 3px; }
nav a { border: 1px solid var(--line); padding: 6px 10px; color: var(--muted); text-decoration: none; }
nav a:hover, nav a:focus-visible { border-color: var(--signal); color: var(--ink); outline: none; }
nav a[aria-current="page"] { border-color: var(--signal); color: var(--ink); }
.panel, .repo-panel, details {
  min-width: 0;
  border: 1px solid var(--line-quiet);
  background: var(--surface);
}
.panel, .repo-panel { padding: 16px; }
.panel > header, .repo-panel > header { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.panel-count { margin: 0; color: var(--faint); font-size: 12px; font-variant-numeric: tabular-nums; }
.panel + .panel { margin-top: 16px; }
.empty { margin: 0; padding: 12px; color: var(--muted); background: #141b1c; font-size: 12px; }
.callout { margin: 16px 0 0; padding: 12px 16px; border: 1px solid var(--signal); color: var(--ink); font-size: 12px; }
.callout--error { border-color: #b0553a; }
.banner { margin: 16px 0 0; padding: 12px 16px; border: 1px solid var(--signal); color: var(--ink); font-size: 12px; }
.banner--error { border-color: #b0553a; }
.tile { min-width: 0; padding: 12px; border: 1px solid var(--line); border-left: 3px solid var(--signal); background: var(--surface-raised); }
.tile--failed, .tile--awaiting-approval, .tile--stale { border-color: var(--signal); background: var(--signal-quiet); }
.tile-head { display: flex; align-items: start; justify-content: space-between; gap: 8px; }
.state { margin: 0; color: var(--signal); font-size: 11px; font-weight: 800; letter-spacing: .06em; text-align: right; text-transform: uppercase; }
.chip, .chip-ok, .chip-bad, .chip-warn, .chip-muted { display: inline-block; padding: 2px 8px; border: 1px solid var(--line); border-radius: 999px; font-size: 11px; font-weight: 700; letter-spacing: .04em; white-space: nowrap; }
.chip-ok { border-color: var(--ok); color: var(--ok); }
.chip-bad { border-color: var(--bad); color: var(--bad); }
.chip-warn { border-color: var(--signal); color: var(--signal); }
.chip-muted { color: var(--muted); }
details { margin-top: 16px; padding: 12px 16px; color: var(--muted); }
details:target { border-color: var(--signal); }
summary { cursor: pointer; color: var(--ink); font-weight: 700; }
.table-wrap { overflow: hidden; }
table { width: 100%; table-layout: fixed; border-collapse: collapse; }
th, td { padding: 10px 12px; border-bottom: 1px solid var(--line-quiet); overflow-wrap: anywhere; text-align: left; vertical-align: top; }
th { color: var(--faint); font-size: 11px; letter-spacing: .06em; text-transform: uppercase; }
td { color: var(--muted); font-size: 12px; }
.flag { color: var(--signal); font-weight: 800; }
footer { margin-top: 20px; color: var(--faint); font-size: 12px; }
footer a { margin-right: 12px; color: var(--muted); }
label { color: var(--muted); font-size: 12px; }
.roster-form label { display: grid; gap: 6px; color: var(--muted); font-size: 12px; }
.roster-form select, .roster-form textarea, .roster-form input[type="text"], .roster-form input[type="search"], .roster-form input[type="number"] { width: 100%; min-height: 36px; padding: 8px 10px; border: 1px solid var(--line); background: var(--surface-raised); color: var(--ink); font: inherit; }
.roster-form textarea { margin-top: 12px; min-height: 72px; }
.roster-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px 16px; }
.roster-grid + .roster-grid, .roster-grid + p, .roster-form > p { margin-top: 12px; }
.hint { display: block; margin-top: 8px; color: var(--faint); font-size: 11px; line-height: 1.4; }
.roster-actions { display: grid; gap: 8px; margin: 16px 0 0; }
.roster-actions label { display: grid; gap: 6px; }
.roster-actions button { min-height: 40px; padding: 0 18px; border: 1px solid var(--signal); background: var(--signal-quiet); color: var(--ink); font: inherit; font-weight: 700; cursor: pointer; justify-self: start; }
.roster-actions .hint { margin-top: 0; }
.confirm-save { margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--line); }
@media (max-width: 700px) {
  .deck-shell { padding: 12px; }
  .masthead { align-items: start; flex-direction: column; }
  .header-meta { text-align: left; }
  th, td { padding: 8px 6px; font-size: 11px; }
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
