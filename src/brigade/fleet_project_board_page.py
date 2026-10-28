"""Projects page: a stored GitHub Projects board with epics, board, and list views.

Server-rendered in full (every card is in the HTML, the epic filter and view
switch are plain links); the script only adds search and the Done collapse.
Everything comes from one stored, validated snapshot. Nothing is fetched here
and nothing is written to the board.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import urlencode

from . import fleet_admin_explorer as explorer
from . import fleet_project_board as store
from . import fleet_projects_prs as prs

VIEWS = ("board", "epics", "list")
ROUTE = "/deck/projects"
_ACTIVE = frozenset({"in progress", "review", "merge queue"})
_EPIC_PREFIX = re.compile(r"^\s*epic\s*:\s*", re.IGNORECASE)
_esc = html.escape


@dataclass(frozen=True)
class Options:
    view: str = "board"
    epic: int | None = None
    # Issue numbers repeat across repositories, so an epic is (repo, number).
    epic_repo: str | None = None


def href(view: str = "board", epic: int | None = None, repo: str | None = None) -> str:
    params: dict[str, str] = {}
    if view != "board":
        params["view"] = view
    if epic is not None and repo is not None:
        params["epic"] = str(epic)
        params["repo"] = repo
    return ROUTE + ("?" + urlencode(params) if params else "")


def _parent_repo(item: Mapping[str, Any]) -> str | None:
    if item["parent"] is None:
        return None
    return item.get("parent_repo") or item["repo"]


def epic_title(title: str) -> str:
    return _EPIC_PREFIX.sub("", title) or title


def epics_of(items: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Items with sub-issues, in-progress statuses first, then by completion.

    An epic whose sub-issues are all done sorts with Done epics, whatever its status.
    """

    def rank(item: Mapping[str, Any]) -> tuple[int, float, int]:
        status = (item["status"] or "").lower()
        finished = status == "done" or item["sub_done"] >= item["sub_total"]
        group = 2 if finished else 0 if status in _ACTIVE else 1
        return (group, -item["sub_done"] / item["sub_total"], item["number"])

    return sorted((i for i in items if i["sub_total"] > 0), key=rank)


def columns_of(board: Mapping[str, Any], items: list[Mapping[str, Any]]) -> list[tuple[str, list[Mapping[str, Any]]]]:
    """Board status order, Done last, with a leading 'No status' group only when needed."""
    options = list(board["fields"]["status_options"])
    names = [o for o in options if o.lower() != "done"] + [o for o in options if o.lower() == "done"]
    groups = [(name, [i for i in items if i["status"] == name]) for name in names]
    loose = [i for i in items if i["status"] is None]
    return ([("No status", loose)] if loose else []) + groups


def _newest(items: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(items, key=lambda i: i["updated_at"], reverse=True)


def _status_pill(status: str | None) -> str:
    name = status or "No status"
    lowered = name.lower()
    return explorer.pill(name, "ok" if lowered == "done" else "warn" if lowered == "blocked" else "muted")


def _search_text(item: Mapping[str, Any]) -> str:
    return " ".join([item["title"], f"#{item['number']}", item["repo"], *item["assignees"]]).lower()


def _attrs(item: Mapping[str, Any]) -> str:
    return (
        f'data-bd-item data-number="{item["number"]}" '
        f'data-labels="{_esc(chr(10).join(label["name"].lower() for label in item["labels"]), quote=True)}" '
        f'data-text="{_esc(_search_text(item), quote=True)}"'
    )


def _chips(item: Mapping[str, Any]) -> str:
    return "".join(prs._label(label) for label in item["labels"])


def _pr_link(item: Mapping[str, Any], checks: Mapping[tuple[str, int], str]) -> str:
    linked = item["linked_prs"]
    if not linked:
        return ""
    first = linked[0]
    check = checks.get((item["repo"], first))
    count = f'<span class="bd-pr-count">{len(linked)}</span>'
    label = f"{len(linked)} linked pull request{'' if len(linked) == 1 else 's'}"
    return (
        f'<a class="bd-pr" href="{_esc(store.pull_url(item["repo"], first), quote=True)}" target="_blank" '
        f'rel="noopener noreferrer" title="{label}" aria-label="{label}">{prs._PR_ICON}{count}'
        f"{prs._check_icon(check) if check else ''}</a>"
    )


def _card(item: Mapping[str, Any], checks: Mapping[tuple[str, int], str]) -> str:
    epic = (
        f'<a class="bd-epic-chip" href="{_esc(href(epic=item["parent"], repo=_parent_repo(item)), quote=True)}">'
        f"epic #{item['parent']}</a>"
        if item["parent"] is not None
        else ""
    )
    pr = _pr_link(item, checks)
    meta = _chips(item) + epic + pr
    return (
        f'<article class="bd-card" {_attrs(item)}>'
        f'<a class="bd-card-title" href="{_esc(item["url"], quote=True)}" target="_blank" rel="noopener noreferrer">'
        f'<span class="pr-num">#{item["number"]}</span> <span class="bd-card-text">{_esc(item["title"])}</span></a>'
        + (f'<div class="bd-card-meta">{meta}</div>' if meta else "")
        + "</article>"
    )


def _epic_card(item: Mapping[str, Any]) -> str:
    total, done = item["sub_total"], item["sub_done"]
    title = epic_title(item["title"])
    return (
        f'<a class="bd-epic-card" {_attrs(item)} href="{_esc(href(epic=item["number"], repo=item["repo"]), quote=True)}">'
        f'<span class="bd-epic-top"><span class="pr-num">#{item["number"]}</span>'
        f'<span class="bd-epic-title" title="{_esc(title, quote=True)}">{_esc(title)}</span>'
        f"{_status_pill(item['status'])}</span>"
        f'<progress class="bd-bar" value="{done}" max="{total}" aria-label="{done} of {total} sub-issues done"></progress>'
        f'<span class="bd-epic-count">{done} of {total} done</span></a>'
    )


def _column(name: str, items: list[Mapping[str, Any]], checks: Mapping[tuple[str, int], str]) -> str:
    lowered = name.lower()
    done = lowered == "done"
    head_pill = explorer.pill(name, "warn") if lowered == "blocked" else ""
    cards = "".join(_card(i, checks) for i in _newest(items))
    return (
        f'<section class="bd-col{" bd-col--blocked" if lowered == "blocked" else ""}" data-bd-group'
        f"{' data-collapsible' if done else ''}>"
        f'<h3 class="bd-col-head">{head_pill or _esc(name)}'
        f'<span class="bd-count" data-bd-count>{len(items)}</span></h3>'
        f'<div class="bd-cards">{cards or ""}</div></section>'
    )


def _row(item: Mapping[str, Any], now: datetime) -> str:
    epic = f'<span class="bd-epic-chip">epic #{item["parent"]}</span>' if item["parent"] is not None else ""
    closed = item["state"] == "CLOSED"
    when = prs.ago(item["updated_at"], now)
    meta = _chips(item) + epic
    return (
        f'<li class="pr-row" {_attrs(item)}><a class="pr-link" href="{_esc(item["url"], quote=True)}" target="_blank" '
        f'rel="noopener noreferrer"><span class="pr-lead{" bd-closed" if closed else ""}">{_ISSUE_ICON}</span>'
        f'<span class="pr-body"><span class="pr-main"><span class="pr-num">#{item["number"]}</span>'
        f'<span class="pr-title">{_esc(item["title"])}</span></span>'
        + (f'<span class="pr-sub">{meta}</span>' if meta else "")
        + f'</span><span class="pr-time"><time datetime="{_esc(item["updated_at"], quote=True)}">{_esc(when)}</time>'
        "</span></a></li>"
    )


_ISSUE_ICON = (
    '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" '
    'aria-hidden="true"><circle cx="8" cy="8" r="6"/><circle cx="8" cy="8" r="1.2" fill="currentColor" stroke="none"/></svg>'
)


def _agent_line(board: Mapping[str, Any]) -> str:
    limit = board.get("rate_limit")
    bits = [
        f"board {_esc(board['owner'])}/{board['number']}",
        f"project id <code>{_esc(board['project_id'])}</code>" if board.get("project_id") else "project id not stored",
        f"observed {_esc(board['observed_at'])}",
        f"rate limit cost {limit['cost']}, remaining {limit['remaining']}" if limit else "rate limit not recorded",
        "snapshot complete" if board["complete"] else "snapshot incomplete: paging stopped",
        f"{board.get('skipped', 0)} non-issue items skipped",
        f"sync <code>{_esc(store.SYNC_COMMAND)}</code>",
    ]
    return explorer.agent(" · ".join(bits), tag="p").replace(
        'data-audience="agent"', 'class="bd-agent" data-audience="agent"', 1
    )


_MAGNIFIER = prs._MAGNIFIER


def render(
    board: Mapping[str, Any],
    options: Options,
    now: datetime,
    *,
    ci_view: Mapping[str, Any] | None = None,
) -> tuple[str, str, str]:
    """Return (section html, css, script)."""
    all_items = list(board["items"])
    epic, epic_repo = options.epic, options.epic_repo
    selected = (epic_repo, epic)
    items = [
        i
        for i in all_items
        if epic is None or (i["repo"], i["number"]) == selected or (_parent_repo(i), i["parent"]) == selected
    ]
    checks: dict[tuple[str, int], str] = {}
    if any(i["linked_prs"] for i in items) and ci_view:
        checks = {(r["repo"], r["number"]): r["check"] for r in prs.collect(ci_view, now)["rows"]}
    view = options.view if options.view in VIEWS else "board"
    segments = "".join(
        f'<a href="{_esc(href(name, epic if name != "epics" else None, epic_repo), quote=True)}"'
        + (' aria-current="page"' if name == view else "")
        + f">{label}</a>"
        for name, label in (("board", "Board"), ("epics", "Epics"), ("list", "List"))
    )
    head = (
        '<div class="bd-head">'
        f'<h2 class="bd-title"><a href="{_esc(board["url"], quote=True)}" target="_blank" rel="noopener noreferrer">'
        f"{_esc(board['title'])}</a></h2>"
        f'<span class="bd-updated">Updated {_esc(prs.ago(board["observed_at"], now))}</span>'
        f'<div class="bd-search" data-bd-search hidden>{_MAGNIFIER}<input type="search" '
        'aria-label="Search issues" placeholder="Search issues, or label:fleet" autocomplete="off" '
        'spellcheck="false"></div>'
        f'<div class="bd-seg" role="group" aria-label="Project view">{segments}</div></div>'
    )
    notes = ""
    if not board["complete"]:
        notes = '<p class="bd-note">Board incomplete: paging stopped before every item was read.</p>'
    epics = epics_of(all_items)
    strip = '<div class="bd-epics" data-bd-group>' + "".join(_epic_card(e) for e in epics) + "</div>" if epics else ""
    chip = ""
    if epic is not None:
        known = any((e["repo"], e["number"]) == selected for e in epics)
        chip = (
            f'<p class="bd-filter">{"Showing epic" if known else "Epic not found"} #{epic} · '
            f'<a href="{href(view)}">Clear</a></p>'
        )
    none = '<p class="empty" data-bd-none hidden>No matching issues.</p>'
    if view == "epics":
        body = (
            strip.replace('class="bd-epics"', 'class="bd-epics bd-epics--page"', 1)
            if epics
            else '<p class="empty">No epics on this board.</p>'
        )
        body += none
    elif view == "list":
        sections = []
        for name, members in columns_of(board, items):
            if members:
                sections.append(
                    f'<section class="pr-group" data-bd-group data-bd-hide-empty><h3><span>{_esc(name)}</span>'
                    f'<span class="pr-count" data-bd-count>{len(members)}</span></h3><ul class="pr-list">'
                    + "".join(_row(i, now) for i in _newest(members))
                    + "</ul></section>"
                )
        body = "".join(sections) + none if sections else '<p class="empty">No issues on this board.</p>'
    else:
        if all_items:
            columns = "".join(_column(name, members, checks) for name, members in columns_of(board, items))
            board_html = f'<div class="bd-columns">{columns}</div>' + none
        else:
            board_html = '<p class="empty">No issues on this board.</p>'
        body = strip + chip + board_html
    if view != "board":
        body = chip + body
    section = (
        f'<section class="bd" data-bd-root aria-label="Project board">{head}{notes}{body}{_agent_line(board)}</section>'
    )
    css = prs.label_css(all_items) + prs.CSS + CSS
    return section, css, SCRIPT


CSS = """
.bd { margin:0 0 24px; }
.bd-head { display:flex; flex-wrap:wrap; align-items:center; gap:8px 14px; margin:0 0 10px; }
.bd-title { margin:0; font-size:15px; font-weight:600; }
.bd-title a { color:var(--ink); }
.bd-title a:hover { text-decoration:underline; text-underline-offset:3px; }
.bd-updated { color:var(--muted); font-size:12px; }
.bd-search { position:relative; flex:1 1 220px; min-width:0; max-width:340px; margin-left:auto; }
.bd-search[hidden] { display:none; }
.bd-search .icon { position:absolute; left:11px; top:50%; transform:translateY(-50%); color:var(--muted); pointer-events:none; }
.bd-search input { width:100%; height:32px; padding:0 12px 0 32px; border:1px solid var(--line); border-radius:8px; background:transparent; color:var(--ink); font:inherit; font-size:13px; }
.bd-search input:focus-visible, .bd-seg a:focus-visible, .bd-col-head:focus-visible { outline:2px solid var(--signal); outline-offset:2px; }
.bd-seg { display:inline-flex; gap:2px; height:32px; padding:3px; border:1px solid var(--line); border-radius:8px; }
.bd-seg a { display:inline-flex; align-items:center; padding:0 12px; border-radius:6px; color:var(--muted); font-size:12px; }
.bd-seg a[aria-current="page"] { background:var(--surface-raised); color:var(--ink); box-shadow:0 0 0 1px var(--line); }
.bd-note, .bd-filter { margin:4px 0 8px; color:var(--muted); font-size:12px; }
.bd-filter a { color:var(--ink); }
.bd-epics { display:flex; gap:8px; margin:0 0 12px; padding-bottom:4px; overflow-x:auto; }
.bd-epics--page { flex-wrap:wrap; overflow:visible; }
.bd-epic-card { display:flex; flex-direction:column; gap:5px; flex:0 0 252px; min-width:0; padding:8px 10px; border:1px solid var(--line); border-radius:8px; color:var(--ink); }
.bd-epics--page .bd-epic-card { flex:1 1 260px; max-width:360px; }
.bd-epic-card:hover { background:var(--surface-raised); }
.bd-epic-top { display:flex; align-items:center; gap:6px; min-width:0; font-size:13px; }
.bd-epic-title { flex:1; min-width:0; font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.bd-epic-top .pill { flex:none; font-size:11px; padding:0 6px; }
.bd-bar { display:block; width:100%; height:4px; border:0; border-radius:2px; background:var(--bar-track); accent-color:var(--ok); -webkit-appearance:none; appearance:none; }
.bd-bar::-webkit-progress-bar { background:var(--bar-track); border-radius:2px; }
.bd-bar::-webkit-progress-value { background:var(--ok); border-radius:2px; }
.bd-bar::-moz-progress-bar { background:var(--ok); border-radius:2px; }
.bd-epic-count { color:var(--muted); font-size:11px; font-variant-numeric:tabular-nums; }
.bd-columns { display:flex; gap:10px; align-items:flex-start; overflow-x:auto; padding-bottom:6px; }
.bd-col { flex:0 0 260px; min-width:0; border:1px solid var(--line); border-radius:8px; padding:0 8px 8px; }
.bd-col-head { display:flex; align-items:center; gap:8px; margin:0; padding:9px 2px; font-size:12px; font-weight:600; }
.bd-col-head .pill { font-size:12px; }
.bd-count { color:var(--muted); font-size:12px; font-weight:400; font-variant-numeric:tabular-nums; }
.bd-col[data-collapsible].is-collapsible .bd-col-head { cursor:pointer; }
.bd-col[data-collapsible].is-collapsible .bd-col-head::after { content:"Show"; margin-left:auto; color:var(--muted); font-size:11px; font-weight:400; }
.bd-col[data-collapsible].is-collapsible:not(.is-collapsed) .bd-col-head::after { content:"Hide"; }
.bd-col.is-collapsed { padding-bottom:0; }
.bd-col.is-collapsed .bd-cards { display:none; }
.bd-cards { display:flex; flex-direction:column; gap:6px; }
@media (min-width:601px) { .bd-cards { max-height:calc(100vh - 280px); overflow-y:auto; } }
.bd-card { padding:8px 9px; border:1px solid var(--line); border-radius:6px; background:var(--surface-raised); }
.bd-card-title { display:block; color:var(--ink); font-size:13px; line-height:18px; font-weight:500; }
.bd-card-text { display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
.bd-card-title .pr-num { float:left; margin-right:5px; }
.bd-card-title:hover .bd-card-text { text-decoration:underline; text-underline-offset:2px; }
.bd-card-meta { display:flex; flex-wrap:wrap; align-items:center; gap:4px 6px; margin-top:6px; }
.bd-epic-chip { display:inline-block; padding:0 6px; border:1px solid var(--line); border-radius:4px; color:var(--muted); font-size:11px; line-height:16px; }
a.bd-epic-chip:hover { color:var(--ink); }
.bd-pr { display:inline-flex; align-items:center; gap:3px; margin-left:auto; color:var(--muted); font-size:11px; }
.bd-pr:hover { color:var(--ink); }
.bd-pr .pr-icon { width:13px; height:13px; }
.bd-pr .pr-check { width:13px; height:13px; }
.bd-closed { color:var(--faint); }
.bd [hidden] { display:none !important; }
html[data-audience-mode="agent"] .bd-agent[data-audience="agent"] { display:block; margin:12px 0 0; padding:0; border:0; color:var(--faint); font-family:var(--font-mono); font-size:11px; line-height:16px; }
html[data-audience-mode="agent"] .bd-agent[data-audience="agent"]::before { display:inline; content:"Agent data "; margin:0 6px 0 0; letter-spacing:.06em; text-transform:uppercase; }
html[data-audience-mode="agent"] .bd-agent code { font-size:11px; color:var(--muted); }
.bd .pr-sub { margin-top:3px; }
@media (max-width:600px) {
 .bd-columns { flex-direction:column; overflow-x:visible; }
 .bd-col { flex:none; width:auto; }
 .bd-search { max-width:none; margin-left:0; flex-basis:100%; }
 .bd-epic-card { flex-basis:200px; }
}
"""

SCRIPT = """(function(){function start(){var root=document.querySelector('[data-bd-root]');if(!root)return;
var items=Array.from(root.querySelectorAll('[data-bd-item]')),box=root.querySelector('[data-bd-search]'),input=box&&box.querySelector('input'),none=root.querySelector('[data-bd-none]');
if(box)box.hidden=false;
root.querySelectorAll('.bd-col[data-collapsible]').forEach(function(col){var head=col.querySelector('.bd-col-head');col.classList.add('is-collapsible','is-collapsed');
head.setAttribute('role','button');head.setAttribute('tabindex','0');head.setAttribute('aria-expanded','false');
function flip(){var closed=col.classList.toggle('is-collapsed');head.setAttribute('aria-expanded',closed?'false':'true');}
head.addEventListener('click',flip);head.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();flip();}});});
function tokens(q){var out=[],re=/([a-z]+):"([^"]*)"|([a-z]+):(\\S*)|"([^"]*)"|(\\S+)/gi,m;
while((m=re.exec(q))){if(m[1])out.push([m[1].toLowerCase(),m[2]]);else if(m[3])out.push([m[3].toLowerCase(),m[4]]);else out.push(['text',m[5]!==undefined?m[5]:m[6]]);}return out;}
function matches(el,list){return list.every(function(t){var k=t[0],v=t[1].toLowerCase();
if(k==='label')return el.dataset.labels.split('\\n').some(function(l){return l.indexOf(v)!==-1;});
var text=k==='text'?v:(k+':'+v);return el.dataset.text.indexOf(text)!==-1;});}
function paint(){var list=tokens(input.value.trim()),shown=0;
items.forEach(function(el){var ok=matches(el,list);el.hidden=!ok;if(ok)shown++;});
root.querySelectorAll('[data-bd-group]').forEach(function(g){var n=g.querySelectorAll('[data-bd-item]:not([hidden])').length,c=g.querySelector('[data-bd-count]');
if(c)c.textContent=String(n);if(g.hasAttribute('data-bd-hide-empty'))g.hidden=n===0;});
if(none)none.hidden=shown>0||!list.length;}
if(input)input.addEventListener('input',paint);}
(typeof window!=='undefined'&&window.fleetOnInit||function(f){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',f);else f();})(start);})();"""
