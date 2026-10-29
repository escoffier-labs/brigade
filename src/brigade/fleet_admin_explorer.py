"""Small presentation-only list/detail surface with a useful no-script view."""

from __future__ import annotations

import hashlib
import html
from dataclasses import dataclass
from typing import Mapping, Sequence
from urllib.parse import parse_qs, urlencode

from .ui_theme import empty_state


@dataclass(frozen=True)
class Entry:
    key: str
    title: str
    status: str
    detail: str  # Escaped HTML from the owning renderer, never raw stored text.
    summary_html: str = ""  # Optional trusted resource bars from the owning renderer.


def anchor(key: str) -> str:
    return "record-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def identifiers(keys: Sequence[str]) -> tuple[str, ...]:
    occurrences: dict[str, int] = {}
    result = []
    for key in keys:
        occurrence = occurrences.get(key, 0)
        occurrences[key] = occurrence + 1
        result.append(anchor(key if occurrence == 0 else f"{key}:duplicate:{occurrence}"))
    return tuple(result)


_WARN_WORDS = (
    "stale",
    "blocked",
    "failed",
    "failure",
    "unknown",
    "error",
    "offline",
    "unavailable",
    "interrupted",
    "expired",
    "orphaned",
    "timed",
    "waiting",
    "open",
    "coverage",
    "collision",
    "not enrolled",
    "not assigned",
)
_OK_WORDS = ("running", "active", "completed", "available", "ready", "enrolled", "healthy", "closed", "ok")


def tone(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in _WARN_WORDS):
        return "warn"
    if any(word in lowered for word in _OK_WORDS):
        return "ok"
    return "muted"


def pill(text: str, kind: str = "") -> str:
    """A small status pill. Colour follows the status word unless *kind* (warn/ok/muted) is given."""
    return (
        f'<span class="pill pill--{kind if kind in {"warn", "ok", "muted"} else tone(text)}">{html.escape(text)}</span>'
    )


def hero(title: str, status: str = "", kind: str = "") -> str:
    """Detail heading: title plus an optional status pill."""
    return f'<div class="detail-hero"><h2>{html.escape(title)}</h2>{pill(status, kind) if status else ""}</div>'


def agent(content: str, tag: str = "div") -> str:
    """Mark *content* (already escaped) as information for agents, not human admins."""
    return f'<{tag} data-audience="agent">{content}</{tag}>' if content else ""


def facts(values: Mapping[str, object], *, audience: str = "") -> str:
    """Executor-style bordered field rows. Every label and value is escaped."""
    esc = html.escape
    rows = "".join(
        f"<div><dt>{esc(str(label))}</dt><dd>{esc(str(value))}</dd></div>"
        for label, value in values.items()
        if value is not None and value != ""
    )
    if not rows:
        return ""
    mark = f' data-audience="{esc(audience, quote=True)}"' if audience else ""
    return f'<dl class="fields"{mark}>{rows}</dl>'


_SEARCH_ICON = (
    '<svg class="icon" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/>'
    '<path d="m20 20-3.5-3.5"/></svg>'
)


def search_box(label: str, extra_class: str = "") -> str:
    esc = html.escape
    return (
        f'<div class="explorer-search{extra_class}" hidden>{_SEARCH_ICON}'
        f'<input type="search" disabled aria-label="Search {esc(label, quote=True)}" '
        f'placeholder="Search {esc(label, quote=True)}\u2026"></div>'
    )


def query_params(query: str) -> dict[str, list[str]]:
    """Bound query parsing before selection or link construction."""
    if len(query) > 8192:
        return {}
    try:
        return parse_qs(query, keep_blank_values=True, max_num_fields=32)
    except ValueError:
        return {}


def selection_id(key: str) -> str:
    """Keep ordinary keys readable and long stored identities addressable."""
    return key if len(key) <= 256 else anchor(key)


def selection_href(key: str) -> str:
    return "?" + urlencode({"selected": selection_id(key)})


def selected_key(query: str) -> str:
    values = query_params(query).get("selected", [])
    if len(values) != 1 or len(values[0]) > 256:
        return ""
    return values[0]


def render(
    entries: Sequence[Entry],
    *,
    label: str,
    empty: str = "No records reported.",
    empty_hint: str = "",
    toolbar: str = "",
    query: str | None = None,
    searchable: bool = True,
) -> str:
    """*toolbar* is trusted, already-escaped HTML shown under the list header (filter chips, notes).

    *empty* is the empty-state title and *empty_hint* its one-line guidance, both plain text."""
    esc = html.escape
    links, details = [], []
    keys = [entry.key for entry in entries]
    selected = selected_key(query or "")
    selections = [selection_id(key) for key in keys]
    chosen = selections.index(selected) if selected in selections else 0
    params = query_params(query or "")
    for index, (entry, identifier) in enumerate(zip(entries, identifiers(keys), strict=True)):
        href = "#" + identifier
        current = hidden = ""
        if query is not None:
            href = "?" + urlencode({**params, "selected": [selection_id(entry.key)]}, doseq=True)
            current = ' aria-current="true"' if index == chosen else ""
            hidden = " hidden" if index != chosen else ""
        links.append(
            f'<a href="{esc(href, quote=True)}" data-record-link="{identifier}"{current}>'
            f'<strong title="{esc(entry.title, quote=True)}">{esc(entry.title[:120])}</strong>'
            f"<span>{esc(entry.status[:160])}</span>{entry.summary_html}</a>"
        )
        details.append(
            f'<article id="{identifier}"{hidden} data-record-detail tabindex="-1">'
            f'<div class="detail-bar"><span>{esc(entry.title[:120])}</span></div>'
            f'<div class="detail-body">{entry.detail}</div></article>'
        )
    return (
        '<section class="admin-explorer" data-explorer' + (" data-query-selection" if query is not None else "") + ">"
        '<aside class="explorer-list"><div class="explorer-head">'
        f'<span class="explorer-title">{esc(label[:1].upper() + label[1:])}</span>'
        f'<span class="explorer-count" data-explorer-count>{len(entries)}</span></div>'
        f"{toolbar}{search_box(label) if searchable else ''}"
        f'<nav aria-label="{esc(label, quote=True)}">{"".join(links)}</nav></aside>'
        '<div class="explorer-details">'
        + "".join(details)
        + empty_state(empty, empty_hint, attrs="data-explorer-empty" + (" hidden" if entries else ""))
        + "</div></section>"
    )


SCRIPT = """(function () {
if (!window.fleetHashBound) {
 window.fleetHashBound = true;
 window.addEventListener('hashchange', function() {
  document.querySelectorAll('[data-explorer]').forEach(function(root) { if (root.fleetApplyHash) { root.fleetApplyHash(location.hash.slice(1)); } });
 });
}
function start() {
 document.querySelectorAll('[data-explorer]').forEach(function(root) {
  var links = Array.from(root.querySelectorAll('[data-record-link]'));
  var records = Array.from(root.querySelectorAll('[data-record-detail]'));
  var searchText = records.map(function(record) {
   var plain = record.cloneNode(true);
   var evidence = Array.from(plain.querySelectorAll('.investigation')).map(function(panel) {
    return panel.dataset.searchText || '';
   }).join(' ');
   plain.querySelectorAll('.investigation, [data-search-exclude]').forEach(function(node) { node.remove(); });
   return (plain.textContent + ' ' + evidence).toLowerCase();
  });
  var input = root.querySelector('input');
  var empty = root.querySelector('[data-explorer-empty]');
  var emptyHtml = empty.innerHTML;
  var chosen = '';
  function apply(preferred) {
   var query = input.value.trim().toLowerCase();
   var visible = links.filter(function(link, index) {
    link.hidden = (link.textContent + ' ' + searchText[index]).toLowerCase().indexOf(query) === -1;
    return !link.hidden;
   });
   var selected = visible.find(function(link) { return link.dataset.recordLink === preferred; }) || visible[0];
   chosen = selected ? selected.dataset.recordLink : '';
   links.forEach(function(link) {
    if (link.dataset.recordLink === chosen) { link.setAttribute('aria-current', 'true'); }
    else { link.removeAttribute('aria-current'); }
   });
   records.forEach(function(record) { record.hidden = record.id !== chosen; });
   var count = root.querySelector('[data-explorer-count]');
   if (count) { count.textContent = String(visible.length); }
   empty.hidden = !!selected;
   if (!selected) {
    if (query) { empty.textContent = 'No matching records.'; } else { empty.innerHTML = emptyHtml; }
   }
  }
  links.forEach(function(link) {
   link.addEventListener('click', function(event) {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) { return; }
    if (root.hasAttribute('data-query-selection')) { return; }
    event.preventDefault();
    apply(link.dataset.recordLink);
    history.replaceState(null, '', '#' + chosen);
    if (window.matchMedia && window.matchMedia('(max-width:700px)').matches) {
     var target = records.find(function(record) { return record.id === chosen; });
     if (target) {
      var calm = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      target.scrollIntoView({ block: 'start', behavior: calm ? 'auto' : 'smooth' });
      target.focus({ preventScroll: true });
     }
    }
   });
   link.addEventListener('keydown', function(event) {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') { return; }
    var visible = links.filter(function(item) { return !item.hidden; });
    var index = visible.indexOf(link) + (event.key === 'ArrowDown' ? 1 : -1);
    var next = visible[(index + visible.length) % visible.length];
    if (next) { event.preventDefault(); next.focus(); next.click(); }
   });
  });
  input.addEventListener('input', function() { apply(chosen); });
  root.fleetApplyHash = apply;
  root.classList.add('is-enhanced');
  input.disabled = false;
  root.querySelector('.explorer-search').hidden = false;
  var initial = root.querySelector('[data-record-link][aria-current]');
  apply(location.hash.slice(1) || (initial ? initial.dataset.recordLink : ''));
 });
}
(typeof window!=='undefined'&&window.fleetOnInit||function(f){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',f);else f();})(start);
})();"""

CSS = """
.admin-explorer { display:grid; grid-template-columns:280px minmax(0,1fr); min-height:360px; border:1px solid var(--line); border-radius:8px; overflow:hidden; }
.admin-explorer [hidden] { display:none !important; }
.explorer-list { min-width:0; border-right:1px solid var(--line); padding:0 8px 12px; }
.explorer-head { display:flex; align-items:center; justify-content:space-between; gap:8px; height:40px; padding:0 8px; font-size:13px; font-weight:600; }
.explorer-count { color:var(--muted); font-size:12px; font-weight:400; font-variant-numeric:tabular-nums; }
.explorer-search { position:relative; margin:0 0 8px; }
.explorer-search .icon { position:absolute; left:10px; top:50%; transform:translateY(-50%); color:var(--muted); pointer-events:none; }
.explorer-search input { min-width:0; width:100%; height:34px; padding:0 10px 0 30px; border:1px solid var(--line); border-radius:8px; background:transparent; color:var(--ink); font:inherit; font-size:13px; }
.explorer-list nav { display:flex; flex-direction:column; gap:2px; margin:0; border:0; }
.explorer-list a { display:block; padding:8px 10px; border-radius:6px; text-decoration:none; color:var(--ink); overflow-wrap:anywhere; }
.explorer-list a strong { display:block; font-size:13px; font-weight:500; line-height:1.3; }
.explorer-list a span { display:block; color:var(--muted); font-size:12px; margin-top:1px; line-height:1.3; }
.explorer-list a:hover, .explorer-list a[aria-current] { background:var(--surface-raised); }
.explorer-details { min-width:0; max-width:100%; overflow-wrap:anywhere; }
.detail-bar { display:flex; align-items:center; height:40px; padding:0 20px; border-bottom:1px solid var(--line); color:var(--muted); font-family:var(--font-mono); font-size:13px; overflow:hidden; white-space:nowrap; }
.detail-bar span { overflow:hidden; text-overflow:ellipsis; }
.detail-body { min-width:0; padding:20px 24px 28px; }
.explorer-details > article + article { margin-top:0; border-top:1px solid var(--line); }
.explorer-details > .empty { padding:28px; }
.detail-hero { display:flex; align-items:center; flex-wrap:wrap; gap:10px; margin:0 0 16px; }
.detail-hero h2, .explorer-details h2 { font-size:22px; font-weight:600; margin:0; }
.explorer-details h3 { margin:24px 0 8px; font-size:13px; font-weight:600; }
.explorer-details p, .explorer-details dd { max-width:85ch; }
.explorer-details p { color:var(--muted); font-size:13px; }
.explorer-details p.work-text { color:var(--ink); }
dl.fields { margin:0 0 16px; border:1px solid var(--line); border-radius:8px; }
dl.fields > div { display:flex; justify-content:space-between; align-items:baseline; gap:24px; padding:10px 14px; border-top:1px solid var(--line-quiet); }
dl.fields > div:first-child { border-top:0; }
dl.fields dt { color:var(--muted); font-size:13px; flex:0 0 auto; }
dl.fields dd { margin:0; min-width:0; text-align:right; font-size:13px; white-space:pre-wrap; overflow-wrap:anywhere; }
.explorer-details input:not([type="checkbox"]), .explorer-details select, .explorer-details textarea { min-width:0; max-width:100%; box-sizing:border-box; }
.explorer-details .roster-grid { grid-template-columns:repeat(auto-fit,minmax(min(240px,100%),1fr)); }
.explorer-details a:not(.button) { color:var(--ink); }
.explorer-footer { margin-top:16px; }
.explorer-details .work-item.panel { padding:0; border:0; background:transparent; }
.explorer-actions { display:flex; flex-wrap:wrap; gap:8px; margin:0 0 16px; }
.explorer-actions a, .explorer-details .row-action { display:inline-flex; align-items:center; min-height:32px; padding:0 12px; border:1px solid var(--line); border-radius:8px; font-size:13px; text-decoration:none; }
.explorer-actions a:hover, .explorer-details .row-action:hover { background:var(--surface-raised); }
.explorer-details ul.plain { list-style:none; margin:0 0 16px; padding:0; border:1px solid var(--line); border-radius:8px; }
.explorer-details ul.plain > li { padding:10px 14px; border-top:1px solid var(--line-quiet); font-size:13px; }
.explorer-details ul.plain > li:first-child { border-top:0; }
.admin-explorer.is-enhanced .explorer-list, .admin-explorer.is-enhanced .explorer-details { max-height:calc(100vh - 230px); overflow-y:auto; }
.admin-explorer.is-enhanced .explorer-details article { margin:0; padding:0; border:0; }
.resource-bars { margin-top:6px; }
.resource-bars span { font-size:11px; }
.resource-bars progress { display:block; width:100%; height:4px; margin:2px 0 6px; border:0; accent-color:var(--working); }
.resource-bars progress.capacity-bar { accent-color:var(--idle); }
.resource-bars progress::-webkit-progress-bar { background:var(--bar-track); }
.resource-bars progress::-webkit-progress-value { background:var(--working); }
.resource-bars progress.capacity-bar::-webkit-progress-value { background:var(--idle); }
.overview-machines { min-width:0; border:1px solid var(--line); border-radius:8px; padding:0 16px 4px; }
.overview-machines > header { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:12px 0; }
.overview-machines h2 { margin:0; font-size:14px; }
.overview-machines-sub { margin:0 0 10px; color:var(--muted); font-size:11px; }
.overview-machine-row { display:grid; grid-template-columns:minmax(150px,1.2fr) minmax(220px,1.7fr) minmax(100px,.8fr) minmax(54px,.35fr); align-items:center; gap:16px; padding:10px 0; border-top:1px solid var(--line-quiet); min-width:0; }
.overview-machine-name, .overview-machine-capacity, .overview-machine-load, .overview-machine-age { min-width:0; }
.overview-machine-name > a { font-size:13px; font-weight:600; }
.overview-machine-name > span { margin-left:8px; color:var(--muted); font-size:11px; }
.overview-machine-name > small { display:block; margin-top:3px; }
.overview-machine-capacity > div { display:flex; justify-content:space-between; gap:8px; font-size:10px; }
.overview-machine-capacity > div span, .overview-machine-load > span, .overview-machine-age > span { color:var(--muted); }
.overview-machine-capacity > div b, .overview-machine-load > b, .overview-machine-age > b { font-weight:500; }
.overview-machine-capacity progress, .overview-machine-load progress { display:block; width:100%; height:5px; margin:3px 0 5px; accent-color:var(--working); }
.overview-machine-capacity progress.overview-capacity-bar { accent-color:var(--idle); }
.overview-machine-capacity progress::-webkit-progress-bar, .overview-machine-load progress::-webkit-progress-bar { background:var(--bar-track); }
.overview-machine-capacity progress.overview-capacity-bar::-webkit-progress-value { background:var(--idle); }
.overview-machine-capacity progress.overview-use-bar::-webkit-progress-value, .overview-machine-load progress::-webkit-progress-value { background:var(--working); }
.overview-machine-capacity > .overview-managed-hint { display:block; margin:-2px 0 4px; color:var(--muted); font-size:10px; }
.overview-machine-load, .overview-machine-age { font-size:11px; }
.overview-machine-load > span, .overview-machine-age > span { display:block; font-size:10px; }
.overview-machines-empty { margin:8px 0 2px; color:var(--muted); font-size:12px; }
.overview-machine-rows { min-width:0; }
.overview .admin-cards { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; }
.overview .admin-cards .admin-card h2, .overview-machines h2 { font-weight:600; }
.admin-card { border:1px solid var(--line); border-radius:8px; padding:0; min-width:0; }
.admin-card header { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:12px 16px; border-bottom:1px solid var(--line); }
.admin-card h2 { font-size:14px; margin:0; }
.admin-card ul { list-style:none; padding:0; margin:0; }
.admin-card li { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:10px 16px; border-bottom:1px solid var(--line-quiet); overflow-wrap:anywhere; }
.admin-card li:last-child { border-bottom:0; }
.admin-card li > div { min-width:0; }
.admin-card li > a:hover { color:var(--ink); }
.admin-card .row-title { font-size:13px; }
.admin-card .row-sub { display:block; color:var(--muted); font-size:12px; }
.admin-card > p { margin:0; padding:10px 16px; }
.admin-card li a { display:block; min-width:0; font-size:13px; }
.admin-card small, .admin-card p { color:var(--muted); font-size:12px; }
@media (max-width:900px) { .admin-cards, .overview .admin-cards { grid-template-columns:1fr; } }
@media (max-width:700px) {
 .overview-machine-row { grid-template-columns:minmax(0,1fr); gap:8px; }
 .overview-machine-age { display:flex; gap:6px; align-items:baseline; }
 .overview-machine-age > span { display:inline; }
 .admin-explorer { grid-template-columns:minmax(0,1fr); }
 .explorer-list { border-right:0; border-bottom:1px solid var(--line); }
 .detail-body { min-width:0; padding:16px; }
 .detail-bar { padding:0 16px; }
 .admin-explorer.is-enhanced .explorer-list { max-height:none; overflow:visible; }
 .explorer-details article { scroll-margin-top:12px; }
 .admin-explorer.is-enhanced .explorer-details { max-height:none; overflow:visible; }
 dl.fields > div { flex-direction:column; gap:2px; }
 dl.fields dd { text-align:left; }
}
"""


SHELL_CSS = """
.shell-main a, .shell-sidebar a { text-decoration:none; }
.sidebar-meta { flex-wrap:wrap; row-gap:4px; }
.sidebar-version { flex:none; }
.repository-grid { margin-bottom:16px; }
.repo-policy-row { padding:10px 0; border-top:1px solid var(--line-quiet); }
.repo-policy-row:first-of-type { border-top:0; }
.repo-policy-row h3 { margin:0 0 2px; font-size:13px; font-weight:600; }
.repo-policy-row p { margin:2px 0; font-size:13px; }
.attention-shortcut { display:flex; align-items:center; gap:8px; white-space:nowrap; }
.attention-shortcut .nav-count { margin-left:auto; flex:none; }
.shell-main a:hover { color:var(--ink); }
.shell-main p a:hover, .shell-main small a:hover { text-decoration:underline; text-underline-offset:3px; }
.shell-main a:focus-visible { outline:2px solid var(--signal); outline-offset:2px; border-radius:4px; }
.pill { display:inline-block; padding:1px 8px; border-radius:6px; border:1px solid var(--line); font-size:12px; font-weight:500; line-height:1.5; white-space:nowrap; color:var(--muted); background:var(--surface-raised); }
.pill--warn { color:var(--signal); border-color:var(--warn-line); background:var(--signal-quiet); }
.pill--ok { color:var(--ok); border-color:var(--ok); background:transparent; }
.admin-section-head { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }
.audience-toggle { display:inline-flex; padding:2px; border:1px solid var(--line); border-radius:8px; background:var(--surface-raised); }
.audience-toggle[hidden] { display:none; }
.audience-toggle button { min-height:26px; padding:0 12px; border:0; border-radius:6px; background:transparent; color:var(--muted); font:inherit; font-size:12px; cursor:pointer; }
.audience-toggle button[aria-pressed="true"] { background:var(--canvas); color:var(--ink); box-shadow:0 0 0 1px var(--line); }
html[data-audience-mode="admin"] [data-audience="agent"] { display:none !important; }
html[data-audience-mode="agent"] [data-audience="agent"] { position:relative; margin:12px 0; padding-left:12px; border-left:2px solid var(--line); }
html[data-audience-mode="agent"] [data-audience="agent"]::before { content:"Agent data"; display:block; margin-bottom:4px; color:var(--faint); font-family:var(--font-mono); font-size:10px; letter-spacing:.06em; text-transform:uppercase; }
html[data-audience-mode="agent"] dl.fields[data-audience="agent"] { padding-left:0; border-left:1px solid var(--line); }
html[data-audience-mode="agent"] dl.fields[data-audience="agent"]::before { padding:6px 14px 0; margin:0; }
.preview-pill { display:inline-block; margin-left:6px; padding:0 6px; border:1px solid var(--line); border-radius:6px; color:var(--muted); font-size:10px; }
"""

SHELL_SCRIPT = """(function(){
var KEY='fleet-audience',root=document.documentElement;
function stored(){try{return localStorage.getItem(KEY)==='agent'?'agent':'admin';}catch(error){return 'admin';}}
function paint(mode){root.setAttribute('data-audience-mode',mode);
document.querySelectorAll('[data-audience-set]').forEach(function(button){
button.setAttribute('aria-pressed',button.getAttribute('data-audience-set')===mode?'true':'false');});}
window.fleetSetAudience=function(mode,persist){mode=mode==='agent'?'agent':'admin';paint(mode);
if(persist!==false){try{localStorage.setItem(KEY,mode);}catch(error){}}};
paint(stored());
function start(){document.querySelectorAll('.audience-toggle').forEach(function(group){
group.hidden=false;group.querySelectorAll('[data-audience-set]').forEach(function(button){
button.addEventListener('click',function(){window.fleetSetAudience(button.getAttribute('data-audience-set'));});});});
paint(root.getAttribute('data-audience-mode')||'admin');}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start);else start();
})();"""


def audience_toggle() -> str:
    """Admin/Agent switch. Hidden until the script runs, so no-JS shows everything."""
    return (
        '<div class="audience-toggle" role="group" aria-label="Information audience" hidden>'
        '<button type="button" data-audience-set="admin" aria-pressed="true">Admin</button>'
        '<button type="button" data-audience-set="agent" aria-pressed="false">Agent</button></div>'
    )
