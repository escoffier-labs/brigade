"""Open pull request list for the Projects page, from the stored CI projection only.

Everything here is read from ``fleet_ci_board.load_view``: PR list metadata the
nodes already fetched plus the exact-head check rollup. Nothing is fetched or
inferred. Additions, deletions and review decisions are not in that projection,
so they are never shown.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any, Mapping


_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_COLOR = re.compile(r"[0-9a-f]{6}")

GROUPS = (
    ("failing", "Failing checks"),
    ("ready", "Ready"),
    ("waiting", "Waiting on checks"),
    ("drafts", "Drafts"),
    ("deps", "Dependency updates"),
    ("unknown", "Checks unknown"),
)
GROUP_TITLES = dict(GROUPS)
CHECK_LABELS = {
    "pass": "Checks passing",
    "fail": "Checks failing",
    "pending": "Checks pending",
    "unknown": "Checks unknown",
}
_ROLLUP = {"passing": "pass", "not-passing": "fail", "pending": "pending"}
MAX_ROWS = 500

_esc = html.escape


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def ago(value: object, now: datetime) -> str:
    moment = _parse(value)
    if moment is None:
        return "unknown"
    seconds = max(0, int((now - moment).total_seconds()))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def check_state(pr: Mapping[str, Any]) -> str:
    """pass / fail / pending / unknown. Anything the rollup cannot prove stays unknown."""
    rollup = pr.get("rollup")
    state = rollup.get("state") if isinstance(rollup, Mapping) else None
    return _ROLLUP.get(state, "unknown") if isinstance(state, str) else "unknown"


def group_of(check: str, *, draft: bool, bot: bool) -> str:
    if check == "fail":
        return "failing"
    if draft:
        return "drafts"
    if bot:
        return "deps"
    return {"pass": "ready", "pending": "waiting"}.get(check, "unknown")


def collect(ci_view: Mapping[str, Any] | None, now: datetime) -> dict[str, Any]:
    """Normalise the CI projection into display rows. Invalid repo names or numbers are skipped."""
    rows: list[dict[str, Any]] = []
    incomplete: list[str] = []
    observed: list[datetime] = []
    complete_repos = 0
    repositories = (ci_view or {}).get("repositories") or []
    for repo in repositories:
        name = repo.get("repo") if isinstance(repo, Mapping) else None
        if not isinstance(name, str) or not _REPO.fullmatch(name):
            continue
        head = repo.get("head") if isinstance(repo.get("head"), Mapping) else {}
        if (stamp := _parse(head.get("observed_at"))) is not None:
            observed.append(stamp)
        if repo.get("pulls_complete") is True:
            complete_repos += 1
        else:
            incomplete.append(name)
        for pr in repo.get("pulls") or []:
            number = pr.get("number") if isinstance(pr, Mapping) else None
            if type(number) is not int or number <= 0:
                continue
            check = check_state(pr)
            draft = pr.get("draft") is True
            bot = pr.get("author_bot") is True
            labels = []
            for label in pr.get("labels") or []:
                color = str(label.get("color") or "").lower() if isinstance(label, Mapping) else ""
                if isinstance(label, Mapping) and isinstance(label.get("name"), str) and label["name"]:
                    labels.append({"name": label["name"], "color": color if _COLOR.fullmatch(color) else None})
            rows.append(
                {
                    "repo": name,
                    "number": number,
                    "title": pr.get("title") if isinstance(pr.get("title"), str) else None,
                    "author": pr.get("author") if isinstance(pr.get("author"), str) else None,
                    "bot": bot,
                    "draft": draft,
                    "labels": labels,
                    "reviewers": [r for r in pr.get("requested_reviewers") or [] if isinstance(r, str)],
                    "check": check,
                    "group": group_of(check, draft=draft, bot=bot),
                    "updated": pr.get("updated_at"),
                    "created": pr.get("created_at"),
                    "head_sha": pr.get("head_sha") if isinstance(pr.get("head_sha"), str) else None,
                    "observed_at": pr.get("observed_at"),
                    "pulls_complete": repo.get("pulls_complete") is True,
                    "reason": repo.get("reason") if isinstance(repo.get("reason"), str) else None,
                }
            )
    rows.sort(key=lambda r: _parse(r["updated"]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return {
        "rows": rows[:MAX_ROWS],
        "truncated": len(rows) > MAX_ROWS,
        "incomplete": incomplete,
        "repositories": len(repositories),
        "complete_repositories": complete_repos,
        "observed": max(observed) if observed else None,
    }


_PR_ICON = (
    '<svg class="pr-icon" viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" '
    'stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><circle cx="4.5" cy="3.5" r="1.6"/>'
    '<circle cx="4.5" cy="12.5" r="1.6"/><circle cx="11.5" cy="12.5" r="1.6"/><path d="M4.5 5.1v5.8"/>'
    '<path d="M11.5 10.9V6.5a2 2 0 0 0-2-2H8"/></svg>'
)
_CHECK_ICONS = {
    "pass": '<circle cx="8" cy="8" r="6"/><path d="m5.5 8.2 1.8 1.8 3.2-3.6"/>',
    "fail": '<circle cx="8" cy="8" r="6"/><path d="m6 6 4 4m0-4-4 4"/>',
    "pending": '<circle cx="8" cy="8" r="6"/><circle cx="8" cy="8" r="1.7" fill="currentColor" stroke="none"/>',
    "unknown": '<circle cx="8" cy="8" r="6" stroke-dasharray="2 2.4"/>',
}
_MAGNIFIER = (
    '<svg class="icon" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/>'
    '<path d="m20 20-3.5-3.5"/></svg>'
)


def _check_icon(check: str) -> str:
    label = CHECK_LABELS[check]
    return (
        f'<svg class="pr-check pr-check--{check}" viewBox="0 0 16 16" width="15" height="15" fill="none" '
        f'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" role="img" aria-label="{label}">'
        f"<title>{label}</title>{_CHECK_ICONS[check]}</svg>"
    )


def _label(label: Mapping[str, Any]) -> str:
    color = label["color"]
    cls = f" lc-{color}" if color else ""
    return f'<span class="pr-label{cls}">{_esc(label["name"])}</span>'


def _row(item: Mapping[str, Any], now: datetime) -> str:
    repo, number = item["repo"], item["number"]
    url = f"https://github.com/{repo}/pull/{number}"
    title = item["title"] or f"Pull request #{number}"
    author = item["author"]
    text = " ".join([title, f"#{number}", author or "", repo])
    labels = "".join(_label(label) for label in item["labels"])
    updated = _parse(item["updated"])
    created = _parse(item["created"])
    initial = _esc((author or "?")[:1].upper())
    who = (
        f'<span class="pr-author"><span class="pr-avatar{" pr-avatar--bot" if item["bot"] else ""}" aria-hidden="true">'
        f"{initial}</span>{_esc(author)}</span>"
        if author
        else ""
    )
    sub = "".join(
        part
        for part in (
            who,
            f'<span class="pr-repo">{_esc(repo)}</span>',
            f'<span class="pr-labels">{labels}</span>' if labels else "",
        )
    )
    agent_bits = [
        f"head <code>{_esc(item['head_sha'][:12])}</code>" if item["head_sha"] else "head unknown",
        "reviewers requested: " + _esc(", ".join(item["reviewers"]))
        if item["reviewers"]
        else "no requested reviewers recorded",
        "observed " + _esc(str(item["observed_at"] or "unknown")),
        "PR list complete"
        if item["pulls_complete"]
        else "PR list incomplete: " + _esc(item["reason"] or "unknown reason"),
    ]
    attrs = (
        f'data-group="{item["group"]}" data-repo="{_esc(repo, quote=True)}" data-number="{number}" '
        f'data-updated="{int(updated.timestamp()) if updated else 0}" '
        f'data-created="{int(created.timestamp()) if created else 0}" '
        f'data-author="{_esc((author or "").lower(), quote=True)}" '
        f'data-bot="{"1" if item["bot"] else "0"}" data-draft="{"1" if item["draft"] else "0"}" '
        f'data-labels="{_esc(chr(10).join(label["name"].lower() for label in item["labels"]), quote=True)}" '
        f'data-text="{_esc(text.lower(), quote=True)}"'
    )
    when = (
        f'<time datetime="{_esc(str(item["updated"]), quote=True)}">{_esc(ago(item["updated"], now))}</time>'
        if updated
        else "<span>unknown</span>"
    )
    draft_badge = '<span class="pr-draft">Draft</span>' if item["draft"] else ""
    icon = _PR_ICON.replace('class="pr-icon"', 'class="pr-icon pr-icon--draft"') if item["draft"] else _PR_ICON
    return (
        f'<li class="pr-row" {attrs}><a class="pr-link" href="{url}" target="_blank" rel="noopener noreferrer">'
        f'<span class="pr-lead">{icon}</span>'
        f'<span class="pr-body"><span class="pr-main"><span class="pr-num">#{number}</span>'
        f'<span class="pr-title">{_esc(title)}</span>{draft_badge}'
        f"{_check_icon(item['check'])}</span>"
        f'<span class="pr-sub">{sub}</span>'
        f'<span class="pr-agent" data-audience="agent">{" · ".join(agent_bits)}</span>'
        f"</span>"
        f'<span class="pr-time">{when}</span></a></li>'
    )


def label_css(rows: list[Mapping[str, Any]]) -> str:
    """One class per validated colour. Inline styles are blocked by the page CSP."""
    colors = sorted({label["color"] for row in rows for label in row["labels"] if label["color"]})
    return "".join(f".pr-label.lc-{c}{{--lc:#{c}}}" for c in colors if _COLOR.fullmatch(c))


def render(ci_view: Mapping[str, Any] | None, now: datetime) -> tuple[str, str]:
    """Return (section html, css). Server-rendered in full; the script only enhances."""
    data = collect(ci_view, now)
    rows = data["rows"]
    notes = []
    if data["repositories"]:
        notes.append(
            f"Updated {_esc(ago(data['observed'].isoformat(), now) if data['observed'] else 'unknown')} "
            f"from {data['complete_repositories']} "
            f"{'repository' if data['complete_repositories'] == 1 else 'repositories'}"
        )
    for name in data["incomplete"]:
        notes.append(f"PR list incomplete for {_esc(name)}")
    if data["truncated"]:
        notes.append(f"Showing the {MAX_ROWS} most recently updated pull requests")
    meta = "".join(f'<p class="pr-note">{note}</p>' for note in notes)
    if not rows:
        hint = (
            ""
            if data["repositories"]
            else " No repositories have CI snapshots yet. Run <code>brigade fleet ci reconcile</code> on a node to populate them."
        )
        empty = f'<p class="empty pr-empty-static">No open pull requests observed.{hint}</p>'
        body = empty
    else:
        sections = []
        for key, title in GROUPS:
            members = [r for r in rows if r["group"] == key]
            if not members:
                continue
            sections.append(
                f'<section class="pr-group" data-group-key="{key}"><h3><span>{_esc(title)}</span>'
                f'<span class="pr-count">{len(members)}</span></h3><ul class="pr-list">'
                + "".join(_row(r, now) for r in members)
                + "</ul></section>"
            )
        body = (
            '<div class="pr-groups" data-pr-groups>'
            + "".join(sections)
            + "</div>"
            + '<p class="empty" data-pr-none hidden>No matching pull requests.</p>'
        )
    controls = (
        '<div class="pr-controls" data-pr-controls hidden>'
        f'<div class="pr-search">{_MAGNIFIER}<input type="search" aria-label="Search pull requests" '
        'placeholder="Search pull requests, or label:bug" autocomplete="off" spellcheck="false"></div>'
        '<label class="pr-sort"><span>Sort</span><select aria-label="Sort pull requests">'
        '<option value="updated">Updated</option><option value="created">Created</option>'
        '<option value="repo">Repository</option></select></label>'
        '<div class="pr-groupby" role="group" aria-label="Group by"><span>Group</span>'
        '<button type="button" data-groupby="status" aria-pressed="true">Status</button>'
        '<button type="button" data-groupby="repo" aria-pressed="false">Repository</button></div></div>'
    )
    section = (
        '<section class="pr-section" data-pr-root aria-label="Open pull requests">'
        '<div class="pr-head"><h2>Open pull requests</h2>'
        + (f'<span class="pr-total">{len(rows)}</span>' if rows else "")
        + "</div>"
        + meta
        + (controls if rows else "")
        + body
        + "</section>"
    )
    return section, label_css(rows) + CSS


CSS = """
.pr-section { margin:0 0 24px; }
.pr-head { display:flex; align-items:center; gap:8px; margin:0 0 4px; }
.pr-head h2 { margin:0; font-size:14px; font-weight:600; }
.pr-total, .pr-count { color:var(--muted); font-size:12px; font-variant-numeric:tabular-nums; }
.pr-note { margin:2px 0; color:var(--muted); font-size:12px; }
.pr-controls { display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin:12px 0 8px; }
.pr-controls[hidden] { display:none; }
.pr-search { position:relative; flex:1 1 280px; min-width:0; }
.pr-search .icon { position:absolute; left:11px; top:50%; transform:translateY(-50%); color:var(--muted); pointer-events:none; }
.pr-search input { width:100%; height:36px; padding:0 12px 0 32px; border:1px solid var(--line); border-radius:8px; background:transparent; color:var(--ink); font:inherit; font-size:13px; }
.pr-sort { display:inline-flex; align-items:center; gap:6px; height:36px; padding:0 4px 0 12px; border:1px solid var(--line); border-radius:8px; color:var(--muted); font-size:13px; }
.pr-sort select { height:30px; border:0; background:transparent; color:var(--ink); font:inherit; font-size:13px; font-weight:600; cursor:pointer; }
.pr-sort select option { background:var(--surface); color:var(--ink); }
.pr-groupby { display:inline-flex; align-items:center; gap:2px; height:36px; padding:0 3px 0 12px; border:1px solid var(--line); border-radius:8px; color:var(--muted); font-size:13px; }
.pr-groupby span { margin-right:auto; padding-right:6px; }
.pr-groupby button { height:28px; padding:0 10px; border:0; border-radius:6px; background:transparent; color:var(--muted); font:inherit; font-size:12px; cursor:pointer; }
.pr-groupby button[aria-pressed="true"] { background:var(--surface-raised); color:var(--ink); box-shadow:0 0 0 1px var(--line); }
.pr-search input:focus-visible, .pr-sort select:focus-visible, .pr-groupby button:focus-visible { outline:2px solid var(--signal); outline-offset:2px; }
.pr-groups [hidden], .pr-section [hidden] { display:none !important; }
.pr-group { margin:0 0 8px; }
.pr-group h3 { display:flex; align-items:center; gap:8px; margin:14px 0 4px; padding:0 4px; font-size:12px; font-weight:500; color:var(--muted); }
.pr-group h3::after { content:""; flex:1; height:1px; background:var(--line-quiet); order:3; }
.pr-list { list-style:none; margin:0; padding:0; }
.pr-link { display:grid; grid-template-columns:20px minmax(0,1fr) auto; gap:8px; align-items:start; padding:7px 8px; border-radius:6px; color:var(--ink); }
.pr-link:hover { background:var(--surface-raised); }
.pr-lead { padding-top:2px; color:var(--ok); }
.pr-icon--draft { color:var(--faint); }
.pr-row[data-draft="0"] .pr-icon { color:var(--ok); }
.pr-body { min-width:0; display:block; }
.pr-main { display:flex; align-items:center; gap:8px; min-width:0; font-size:14px; line-height:20px; }
.pr-num { flex:none; color:var(--muted); font-size:12px; font-variant-numeric:tabular-nums; }
.pr-title { min-width:0; font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.pr-draft { flex:none; padding:0 6px; border:1px solid var(--line); border-radius:6px; color:var(--muted); font-size:11px; line-height:16px; }
.pr-check { flex:none; }
.pr-check--pass { color:var(--ok); }
.pr-check--fail { color:var(--bad); }
.pr-check--pending { color:var(--signal); }
.pr-check--unknown { color:var(--faint); }
.pr-sub { display:flex; flex-wrap:wrap; align-items:center; gap:4px 10px; margin-top:2px; color:var(--muted); font-size:12px; line-height:18px; }
.pr-author { display:inline-flex; align-items:center; gap:5px; }
.pr-avatar { display:inline-flex; align-items:center; justify-content:center; width:14px; height:14px; border-radius:50%; background:var(--bar-track); color:var(--muted); font-size:9px; font-weight:600; }
.pr-avatar--bot { border-radius:4px; background:var(--working); color:var(--on-accent); }
.pr-labels { display:inline-flex; flex-wrap:wrap; gap:4px; }
.pr-label { --lc:var(--muted); display:inline-block; padding:0 6px; border:1px solid var(--line); border-radius:4px; background:var(--surface-raised); color:var(--ink); font-size:11px; font-weight:500; line-height:16px; }
@supports (color:color-mix(in srgb, red 50%, blue)) {
 .pr-label { background:color-mix(in srgb, var(--lc) 20%, transparent); border-color:color-mix(in srgb, var(--lc) 45%, transparent); color:color-mix(in srgb, var(--lc) 30%, var(--ink)); }
}
@media (prefers-color-scheme: light) {
 .pr-label { color:var(--ink); }
 @supports (color:color-mix(in srgb, red 50%, blue)) {
  .pr-label { background:color-mix(in srgb, var(--lc) 28%, var(--surface)); border-color:color-mix(in srgb, var(--lc) 70%, var(--ink)); }
 }
}
.pr-time { padding-top:2px; color:var(--muted); font-size:12px; font-variant-numeric:tabular-nums; white-space:nowrap; text-align:right; }
html[data-audience-mode="agent"] .pr-row [data-audience="agent"] { display:block; margin:3px 0 0; padding:0; border:0; color:var(--faint); font-family:var(--font-mono); font-size:11px; line-height:16px; }
html[data-audience-mode="agent"] .pr-row [data-audience="agent"]::before { display:inline; content:"Agent data "; margin:0 6px 0 0; letter-spacing:.06em; text-transform:uppercase; }
html[data-audience-mode="agent"] .pr-row [data-audience="agent"] code { font-size:11px; color:var(--muted); }
.pr-empty-static { padding:12px 4px; }
@media (max-width:700px) {
 .pr-link { grid-template-columns:20px minmax(0,1fr); }
 .pr-time { grid-column:2; text-align:left; padding-top:0; }
 .pr-title { white-space:normal; }
 .pr-controls > * { flex:1 1 100%; }
 .pr-sort, .pr-groupby { justify-content:space-between; }
}
"""

SCRIPT = """(function(){function start(){var root=document.querySelector('[data-pr-root]');if(!root)return;
var holder=root.querySelector('[data-pr-groups]'),controls=root.querySelector('[data-pr-controls]');if(!holder||!controls)return;
var input=controls.querySelector('input'),select=controls.querySelector('select'),buttons=Array.from(controls.querySelectorAll('[data-groupby]')),none=root.querySelector('[data-pr-none]'),total=root.querySelector('.pr-total');
var rows=Array.from(holder.querySelectorAll('.pr-row'));
var titles={failing:'Failing checks',ready:'Ready',waiting:'Waiting on checks',drafts:'Drafts',deps:'Dependency updates',unknown:'Checks unknown'};
var order=['failing','ready','waiting','drafts','deps','unknown'];var mode='status';
controls.hidden=false;
function tokens(q){var out=[],re=/([a-z]+):"([^"]*)"|([a-z]+):(\\S*)|"([^"]*)"|(\\S+)/gi,m;
while((m=re.exec(q))){if(m[1])out.push([m[1].toLowerCase(),m[2]]);else if(m[3])out.push([m[3].toLowerCase(),m[4]]);else out.push(['text',m[5]!==undefined?m[5]:m[6]]);}return out;}
function matches(row,list){return list.every(function(t){var k=t[0],v=t[1].toLowerCase();
if(k==='label')return row.dataset.labels.split('\\n').some(function(l){return l.indexOf(v)!==-1;});
if(k==='repo')return row.dataset.repo.toLowerCase().indexOf(v)!==-1;
if(k==='author')return row.dataset.author.indexOf(v)!==-1;
if(k==='is'){if(v==='draft')return row.dataset.draft==='1';if(v==='bot')return row.dataset.bot==='1';return false;}
var text=k==='text'?v:(k+':'+v);return row.dataset.text.indexOf(text)!==-1;});}
function sorter(a,b){var key=select.value;
if(key==='repo'){var r=a.dataset.repo.localeCompare(b.dataset.repo);return r||(Number(b.dataset.number)-Number(a.dataset.number));}
var f=key==='created'?'created':'updated';return Number(b.dataset[f])-Number(a.dataset[f]);}
function section(key,title,items){var s=document.createElement('section');s.className='pr-group';s.dataset.groupKey=key;
var h=document.createElement('h3'),n=document.createElement('span'),c=document.createElement('span');n.textContent=title;c.className='pr-count';c.textContent=String(items.length);h.appendChild(n);h.appendChild(c);
var ul=document.createElement('ul');ul.className='pr-list';items.forEach(function(r){ul.appendChild(r);});s.appendChild(h);s.appendChild(ul);return s;}
function paint(){var list=tokens(input.value.trim()),shown=rows.filter(function(r){return matches(r,list);}).sort(sorter);
rows.forEach(function(r){if(r.parentNode)r.parentNode.removeChild(r);});holder.textContent='';
if(mode==='repo'){var repos=[];shown.forEach(function(r){if(repos.indexOf(r.dataset.repo)===-1)repos.push(r.dataset.repo);});
repos.sort();
repos.forEach(function(name){holder.appendChild(section(name,name,shown.filter(function(r){return r.dataset.repo===name;})));});}
else{order.forEach(function(key){var items=shown.filter(function(r){return r.dataset.group===key;});if(items.length)holder.appendChild(section(key,titles[key],items));});}
none.hidden=shown.length>0;if(total)total.textContent=String(shown.length);}
input.addEventListener('input',paint);select.addEventListener('change',paint);
buttons.forEach(function(b){b.addEventListener('click',function(){mode=b.dataset.groupby;buttons.forEach(function(o){o.setAttribute('aria-pressed',o===b?'true':'false');});paint();});});
paint();}
(typeof window!=='undefined'&&window.fleetOnInit||function(f){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',f);else f();})(start);})();"""
