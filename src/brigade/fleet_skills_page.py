"""Settings > Skills: review which skills need a decision, and the plan that follows.

Presentation only. Nothing here runs a command on a machine. Every stored or
reported string is escaped, and hashes, raw status codes, and ISO times are
agent data behind the shared Admin/Agent switch.
"""

from __future__ import annotations

import json

import hashlib
import hmac
import html
import uuid
from datetime import datetime
from typing import Mapping

from . import fleet_admin_explorer as explorer
from . import fleet_command_deck as deck
from . import fleet_skills_review as review
from .fleet_skills_review import Plan, Skill, SkillsView

CSRF_PURPOSE = b"brigade-fleet-skills-review-csrf-v1"
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
MAX_FORM_BYTES = 64 * 1024
READ_ONLY_NOTICE = "Access: read-only. You can inspect skills; editing requires an authorized admin request."
_esc = html.escape

_STATUS_KIND = {"current": "ok", "stale": "warn", "missing": "warn", "unknown": "warn"}
_REASON_KIND = {"conflict": "warn", "regressed": "warn", "old install": "muted"}

CSS = """
.skills-bar { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:12px; margin:0 0 12px; }
.skills-bar p { margin:0; color:var(--muted); font-size:13px; }
.skills-page .access-note { margin:0 0 12px; color:var(--muted); font-size:12px; }
.skills-page .plan-title { margin:0 0 4px; font-size:22px; font-weight:600; }
.skills-page .plan-sub { margin:0 0 16px; color:var(--muted); font-size:13px; }
.skills-page .link-button, .skills-page button[type="submit"], .skills-page .copy-command { display:inline-flex; align-items:center; min-height:32px; padding:0 14px; border:1px solid var(--line); border-radius:8px; background:transparent; color:var(--ink); font:inherit; font-size:13px; cursor:pointer; text-decoration:none; }
.skills-page .link-button.primary, .skills-page button[type="submit"].primary { background:var(--ink); color:var(--canvas); border-color:var(--ink); font-weight:500; }
.skills-page .link-button:hover, .skills-page .copy-command:hover { background:var(--surface-raised); }
.skills-page .link-button.primary:hover { background:var(--ink); }
.skills-page button:disabled { opacity:.5; cursor:not-allowed; }
.explorer-list nav.skills-chips { display:flex; flex-direction:row; flex-wrap:wrap; gap:4px; margin:0 0 8px; padding:0 2px; }
.explorer-list nav.skills-chips a { display:inline-block; min-height:0; margin:0; padding:1px 9px; border:1px solid var(--line); border-radius:999px; color:var(--muted); font-size:12px; line-height:1.7; text-decoration:none; }
.explorer-list nav.skills-chips a:hover { background:var(--surface-raised); color:var(--ink); }
.explorer-list nav.skills-chips a[aria-current="true"] { background:var(--surface-raised); color:var(--ink); border-color:var(--muted); }
.skills-missing { margin:0 0 8px; padding:0 8px; color:var(--muted); font-size:12px; }
.explorer-list a .skill-meta { display:flex; align-items:center; gap:8px; margin-top:4px; }
.explorer-list a .skill-meta .pill { display:inline-block; margin:0; font-size:11px; padding:0 7px; }
.explorer-list a .skill-meta .pill--warn { color:var(--signal); }
.explorer-list a .skill-meta .pill--ok { color:var(--ok); }
.explorer-list a .skill-meta .skill-score { margin:0; font-family:var(--font-mono); font-size:11px; color:var(--muted); }
.skills-page .skill-verdict { margin:0 0 8px; color:var(--ink); font-size:15px; max-width:70ch; }
.skills-page .suggestion { margin:0 0 16px; font-size:13px; }
.skills-page .callout { margin:0 0 12px; padding:0; border:0; background:none; font-size:13px; color:var(--ink); }
.skills-page .callout[role="alert"] { color:var(--bad); }
.skills-page .table-wrap { margin:0 0 16px; overflow-x:auto; border:1px solid var(--line); border-radius:8px; }
.skills-page .table-wrap table { width:100%; min-width:440px; border-collapse:collapse; font-size:13px; }
.skills-page .table-wrap td { vertical-align:middle; }
.skills-page .table-wrap th { white-space:nowrap; padding:8px 14px; text-align:left; color:var(--muted); font-weight:500; font-size:12px; }
.skills-page .table-wrap td { padding:9px 14px; border-top:1px solid var(--line-quiet); }
.skills-page .table-wrap td code { font-size:12px; overflow-wrap:anywhere; }
.skills-page .decision { margin:0 0 16px; }
.skills-page .decision h3 { margin:0 0 8px; }
.segmented { display:inline-flex; flex-wrap:wrap; padding:2px; border:1px solid var(--line); border-radius:8px; background:var(--surface-raised); }
.segmented label { position:relative; display:block; }
.segmented input { position:absolute; opacity:0; inset:0; margin:0; cursor:pointer; }
.segmented span { display:block; min-height:28px; padding:0 14px; border-radius:6px; color:var(--muted); font-size:13px; line-height:28px; }
.segmented input:checked + span { background:var(--canvas); color:var(--ink); box-shadow:0 0 0 1px var(--line); }
.segmented input:disabled { cursor:not-allowed; }
.segmented input:disabled + span { opacity:.4; }
.segmented input:focus-visible + span { outline:2px solid var(--signal); outline-offset:1px; }
.skills-page .decision-hint { margin:8px 0 0; font-size:12px; color:var(--muted); }
.skills-page details.overrides { margin:12px 0 0; }
.skills-page details.overrides summary { cursor:pointer; color:var(--muted); font-size:13px; }
.skills-page details.overrides select { min-height:28px; padding:0 8px; border:1px solid var(--line); border-radius:6px; background:var(--canvas); color:var(--ink); font:inherit; font-size:13px; }
.skills-page .decision-actions { display:flex; gap:8px; margin-top:12px; }
.plan-group { margin:0 0 16px; }
.plan-group ul { list-style:none; margin:0; padding:0; border:1px solid var(--line); border-radius:8px; }
.plan-group li { padding:8px 14px; border-top:1px solid var(--line-quiet); font-size:13px; }
.plan-group li:first-child { border-top:0; }
.plan-group li .muted { color:var(--muted); }
.plan-group h3 { margin:0 0 6px; font-size:13px; font-weight:600; }
.plan-machine { margin:0 0 20px; border:1px solid var(--line); border-radius:8px; }
.plan-machine > header { padding:10px 14px; border-bottom:1px solid var(--line); }
.plan-machine > header h3 { margin:0; font-size:14px; font-weight:600; }
.plan-machine > header p { margin:2px 0 0; font-size:12px; color:var(--muted); }
.plan-command { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:10px 14px; border-top:1px solid var(--line-quiet); }
.plan-machine > header + .plan-command { border-top:0; }
.plan-command code { min-width:0; font-size:12px; overflow-wrap:anywhere; }
.plan-command .what { display:block; margin-bottom:2px; color:var(--muted); font-size:12px; }
.plan-actions { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:16px 0; }
.skills-publish { display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin:8px 0 0; }
.skills-publish code { padding:6px 10px; border:1px solid var(--line); border-radius:6px; font-size:12px; }
@media (max-width:700px) { .plan-command { flex-direction:column; align-items:stretch; } .skills-page .copy-command { justify-content:center; } }
"""

SCRIPT = """(function(){
function start(){document.querySelectorAll('[data-command]').forEach(function(button){
if(button.dataset.bound)return;button.dataset.bound='true';
button.addEventListener('click',async function(){
var text=button.dataset.command,label=button.textContent,copied=false;
try{await navigator.clipboard.writeText(text);copied=true;}catch(error){
var scratch=document.createElement('textarea');scratch.value=text;scratch.setAttribute('readonly','');
scratch.style.position='fixed';scratch.style.opacity='0';document.body.appendChild(scratch);scratch.select();
try{copied=document.execCommand('copy')===true;}catch(ignored){}scratch.remove();}
button.textContent=copied?'Copied':'Select the command to copy it';
setTimeout(function(){button.textContent=label;},2000);});});}
(typeof window!=='undefined'&&window.fleetOnInit||function(f){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',f);else f();})(start);
})();"""


def csrf_value(token: str) -> str:
    return hmac.new(token.encode("utf-8"), CSRF_PURPOSE, hashlib.sha256).hexdigest()


def _csrf_field(csrf: str, editable: bool) -> str:
    """Read-only pages carry no token at all."""
    return f'<input type="hidden" name="csrf" value="{_esc(csrf, quote=True)}">' if editable else ""


def anchor(skill_id: str) -> str:
    return explorer.anchor("skill:" + skill_id)


def _copy_button(command: str, label: str = "Copy") -> str:
    return (
        f'<button type="button" class="copy-command" data-command="{_esc(command, quote=True)}" '
        f'aria-label="Copy command">{_esc(label)}</button>'
    )


def _reason_pill(skill: Skill) -> str:
    if not skill.reasons:
        return explorer.pill("no action needed", "ok")
    reason = skill.reasons[0]
    return explorer.pill(reason, _REASON_KIND.get(reason, "warn" if "stale" in reason else "muted"))


def _score(skill: Skill) -> str:
    return "" if skill.wilson is None else f"{skill.wilson:.2f}"


def _summary(skill: Skill) -> str:
    score = _score(skill)
    return (
        f'<span class="skill-meta">{_reason_pill(skill)}'
        + (f'<span class="skill-score" title="Wilson score">{_esc(score)}</span>' if score else "")
        + "</span>"
    )


def _differs(skill: Skill) -> bool:
    return len({(c.status, c.rollback_available, c.installed_sha256) for c in skill.copies}) > 1


def _conflict_text(skill: Skill) -> str:
    lines = []
    for item in skill.conflicts:
        where = ", ".join(item["locations"])
        if item["kind"] == "duplicate-source":
            lines.append(f"{item['machine']}: installed from different sources for {where}.")
        else:
            lines.append(f"{item['machine']}: the copies for {where} do not match each other.")
    return "".join(f'<p class="callout">{_esc(line)}</p>' for line in lines)


def _machines_table(skill: Skill) -> str:
    rows = "".join(
        f"<tr><td>{_esc(copy.machine)}</td><td>{_esc(copy.harness)}</td>"
        f"<td>{explorer.pill(copy.status, _STATUS_KIND.get(copy.status, 'muted'))}</td>"
        f"<td>{'Yes' if copy.rollback_available else 'No'}</td></tr>"
        for copy in skill.copies
    )
    return (
        '<div class="table-wrap"><table><thead><tr><th>Machine</th><th>Harness</th><th>Copy</th>'
        f"<th>Roll back available</th></tr></thead><tbody>{rows}</tbody></table></div>"
    )


def _agent_block(skill: Skill, view: SkillsView) -> str:
    machines = {machine.label: machine for machine in view.machines}
    rows = "".join(
        f"<tr><td>{_esc(copy.machine)}</td><td>{_esc(copy.harness)}</td><td><code>{_esc(copy.status)}</code></td>"
        f"<td><code>{_esc(copy.installed_sha256 or 'none')}</code></td>"
        f"<td><code>{_esc(copy.registry_sha256 or 'none')}</code></td></tr>"
        for copy in skill.copies
    )
    reports = "".join(
        f"<tr><td>{_esc(machine.label)}</td><td>{_esc(machine.brigade_version)}</td>"
        f"<td>{_esc(machine.observed_at)}</td><td>{_esc(machine.registry_revision or 'unknown')}</td></tr>"
        for copy_machine in sorted({copy.machine for copy in skill.copies})
        for machine in (machines.get(copy_machine),)
        if machine is not None
    )
    conflicts = "".join(
        f"<li>{_esc(item['machine'])}: <code>{_esc(item['kind'])}</code> across {_esc(', '.join(item['locations']))}</li>"
        for item in skill.conflicts
    )
    counts = explorer.facts(
        {
            "Helped": skill.helped,
            "Hurt": skill.hurt,
            "Neutral": skill.neutral,
            "Wilson lower bound": "none" if skill.wilson is None else f"{skill.wilson:.4f}",
            "Ratchet status": skill.status or "none",
            "Ratchet suggestion": skill.suggestion or "none",
            "Suggestion reason": skill.suggestion_reason or "none",
            "Last signal": skill.last_signal_at or "none",
            "Usage tracked": "yes" if skill.usage_tracked else "no",
        }
    )
    return explorer.agent(
        counts + f'<div class="table-wrap"><table><thead><tr><th>Machine</th><th>Harness</th><th>Status code</th>'
        f"<th>Installed sha256</th><th>Registry sha256</th></tr></thead><tbody>{rows}</tbody></table></div>"
        + (
            '<div class="table-wrap"><table><thead><tr><th>Machine</th><th>Brigade</th><th>Reported</th>'
            f"<th>Registry revision</th></tr></thead><tbody>{reports}</tbody></table></div>"
            if reports
            else ""
        )
        + (f'<h3>Conflict locations</h3><ul class="plain">{conflicts}</ul>' if conflicts else "")
    )


def _decision_form(
    skill: Skill, entry: Mapping[str, object] | None, *, csrf: str, editable: bool, filter_key: str
) -> str:
    chosen = str(entry.get("default", "keep")) if entry else "keep"
    overrides = entry.get("machines") if entry and isinstance(entry.get("machines"), dict) else {}
    off = "" if editable else " disabled"
    options = []
    for name in review.DECISIONS:
        usable = review.allowed(skill, name)
        attrs = (" checked" if name == chosen else "") + ("" if usable and editable else " disabled")
        options.append(
            f'<label><input type="radio" name="decision" value="{name}"{attrs}><span>{_esc(review.DECISION_LABELS[name])}</span></label>'
        )
    hints = []
    if not skill.can_roll_back:
        hints.append("Roll back is off because no machine has a rollback snapshot.")
    if not skill.can_update:
        hints.append("Update is off because every copy is current.")
    per_machine = ""
    if _differs(skill):
        rows = []
        for copy in skill.copies:
            current = str(overrides.get(copy.key, "")) if isinstance(overrides, dict) else ""
            choices = ['<option value="">Same as above</option>'] + [
                f'<option value="{name}"{" selected" if name == current else ""}'
                f"{'' if review.allowed(skill, name, copy) else ' disabled'}>{_esc(review.DECISION_LABELS[name])}</option>"
                for name in review.DECISIONS
            ]
            rows.append(
                f"<tr><td>{_esc(copy.machine)}</td><td>{_esc(copy.harness)}</td><td>"
                f'<select name="ov:{_esc(copy.key, quote=True)}" aria-label="Decision for {_esc(copy.machine, quote=True)} {_esc(copy.harness, quote=True)}"{off}>'
                + "".join(choices)
                + "</select></td></tr>"
            )
        per_machine = (
            '<details class="overrides"><summary>Different choice for one machine</summary>'
            '<div class="table-wrap"><table><thead><tr><th>Machine</th><th>Harness</th><th>Decision</th></tr></thead>'
            f"<tbody>{''.join(rows)}</tbody></table></div></details>"
        )
    hint_html = "".join(f'<p class="decision-hint">{_esc(h)}</p>' for h in hints)
    bindings = "".join(
        f"<p>{_esc(c.machine)} ({_esc(c.harness)})</p><pre>{_esc(json.dumps(c.binding, sort_keys=True, indent=2) if c.binding else 'Incomplete package provenance; mutation approval unavailable')}</pre>"
        for c in skill.copies
    )
    hint_html += f"<details><summary>Exact package bindings</summary>{bindings}<p>Null evaluation means evidence pending or absent.</p></details>"

    save = (
        '<div class="decision-actions"><button type="submit" class="primary">Save decision</button></div>'
        if editable
        else ""
    )
    return (
        '<form class="decision" method="post" action="/deck/skills">'
        "<h3>Decision</h3>"
        f"{_csrf_field(csrf, editable)}"
        f'<input type="hidden" name="display_token" value="{review.review_token(skill)}">'
        '<input type="hidden" name="action" value="save">'
        f'<input type="hidden" name="skill" value="{_esc(skill.skill_id, quote=True)}">'
        f'<input type="hidden" name="filter" value="{_esc(filter_key, quote=True)}">'
        f'<div class="segmented" role="radiogroup" aria-label="Decision for {_esc(skill.skill_id, quote=True)}">{"".join(options)}</div>'
        f"{hint_html}{per_machine}{save}</form>"
    )


def _detail(
    skill: Skill, view: SkillsView, entry: Mapping[str, object] | None, *, csrf: str, editable: bool, filter_key: str
) -> str:
    kind = _REASON_KIND.get(skill.reason, "warn" if "stale" in skill.reason else "muted")
    if not skill.reasons:
        kind = "ok"
    suggestion = review.suggestion_text(skill)
    return (
        explorer.hero(skill.skill_id, skill.reason, kind)
        + f'<p class="skill-verdict">{_esc(review.verdict(skill))}</p>'
        + (f'<p class="suggestion">{_esc(suggestion)}</p>' if suggestion else "")
        + _conflict_text(skill)
        + _machines_table(skill)
        + _decision_form(skill, entry, csrf=csrf, editable=editable, filter_key=filter_key)
        + _agent_block(skill, view)
    )


def _chips(view: SkillsView, active: str) -> str:
    links = []
    for key, label in review.FILTERS:
        count = sum(1 for skill in view.skills if review.matches(skill, key))
        current = ' aria-current="true"' if key == active else ""
        links.append(f'<a href="/deck/skills?filter={key}"{current}>{_esc(label)} {count}</a>')
    return f'<nav class="skills-chips" aria-label="Skill filters">{"".join(links)}</nav>'


def _publish_hint() -> str:
    return (
        f'<div class="skills-publish"><span>No skills reports yet. Publish one from a machine with</span>'
        f"<code>{_esc(review.PUBLISH_COMMAND)}</code>{_copy_button(review.PUBLISH_COMMAND)}</div>"
    )


def _banner(banner: str | None, error: str | None) -> str:
    out = ""
    if banner:
        out += f'<p class="callout" role="status">{_esc(banner)}</p>'
    if error:
        out += f'<p class="callout" role="alert">{_esc(error)}</p>'
    return out


def _page(content: str, *, nonce: str, now: datetime, title: str) -> str:
    return deck._document(
        f'<main class="deck-shell admin-page skills-page">{content}</main>',
        nonce=nonce,
        now=now,
        title=title,
        active="skills",
        view=None,
        refresh=False,
        extra_css=explorer.CSS + CSS,
        script=explorer.SCRIPT + SCRIPT,
    )


def render(
    view: SkillsView,
    draft: Mapping[str, Mapping[str, object]],
    *,
    nonce: str,
    now: datetime,
    csrf: str,
    editable: bool,
    filter_key: str = "needs",
    banner: str | None = None,
    error: str | None = None,
) -> str:
    if filter_key not in dict(review.FILTERS):
        filter_key = "needs"
    plan = review.build_plan(view, draft)
    top = _banner(banner, error)
    top += "".join(f'<p class="access-note">{_esc(b)}</p>' for b in plan.blockers)
    if not editable:
        top += f'<p class="access-note">{_esc(READ_ONLY_NOTICE)}</p>'
    if view.reports == 0:
        body = top + '<section class="panel"><h2>Skills</h2>' + _publish_hint() + "</section>"
        if view.machines:
            body += explorer.agent(_missing(view))
        return _page(body, nonce=nonce, now=now, title="Skills")
    shown = [skill for skill in view.skills if review.matches(skill, filter_key)]
    entries = [
        explorer.Entry(
            "skill:" + skill.skill_id,
            skill.skill_id,
            f"{len(skill.copies)} cop{'y' if len(skill.copies) == 1 else 'ies'} on "
            f"{len({c.machine for c in skill.copies})} machine{'' if len({c.machine for c in skill.copies}) == 1 else 's'}",
            _detail(skill, view, draft.get(skill.skill_id), csrf=csrf, editable=editable, filter_key=filter_key),
            _summary(skill),
        )
        for skill in shown
    ]
    missing = [machine.label for machine in view.machines if not machine.reported]
    toolbar = _chips(view, filter_key) + (
        f'<p class="skills-missing">No skills report: {_esc(", ".join(missing))}</p>' if missing else ""
    )
    bar = (
        '<div class="skills-bar">'
        f"<p>{_esc(f'{plan.count} change' + ('' if plan.count == 1 else 's') + ' in your draft')}</p>"
        f'<a class="link-button primary" href="/deck/skills/plan">'
        f"{_esc(f'Review plan ({plan.count} change' + ('' if plan.count == 1 else 's') + ')')}</a></div>"
    )
    explorer_html = explorer.render(
        entries,
        label="skills",
        empty="Nothing to review in this view." if view.skills else "No skills are installed on reporting machines.",
        toolbar=toolbar,
    )
    log = ""
    if view.events:
        log = explorer.agent(
            '<h3>Recent audit events</h3><ul class="plain">'
            + "".join(f"<li>{_esc(at)} <code>{_esc(kind)}</code></li>" for at, kind, _summary_text in view.events)
            + "</ul>"
        )
    return _page(top + bar + explorer_html + log, nonce=nonce, now=now, title="Skills")


def _missing(view: SkillsView) -> str:
    names = [machine.label for machine in view.machines if not machine.reported]
    return f"<p>No skills report: {_esc(', '.join(names))}</p>" if names else ""


def render_plan(
    view: SkillsView,
    draft: Mapping[str, Mapping[str, object]],
    *,
    nonce: str,
    now: datetime,
    csrf: str,
    editable: bool,
    banner: str | None = None,
    error: str | None = None,
) -> str:
    plan: Plan = review.build_plan(view, draft)
    top = _banner(banner, error)
    top += "".join(f'<p class="access-note">{_esc(b)}</p>' for b in plan.blockers)
    if not editable:
        top += f'<p class="access-note">{_esc(READ_ONLY_NOTICE)}</p>'
    back = '<p><a href="/deck/skills">Back to skills</a></p>'
    if plan.count == 0:
        return _page(
            top
            + back
            + '<section class="panel"><h2>Review plan</h2><p>No changes in your draft. Pick Update, Remove, or Roll back on a skill first.</p></section>',
            nonce=nonce,
            now=now,
            title="Review skills plan",
        )
    groups = []
    for name, items in plan.by_action().items():
        if not items:
            continue
        rows = "".join(
            f'<li>{_esc(item.skill_id)} <span class="muted">on {_esc(item.machine)} ({_esc(item.harness)})</span></li>'
            for item in items
        )
        groups.append(
            f'<div class="plan-group"><h3>{_esc(review.DECISION_LABELS[name])} {len(items)}</h3><ul>{rows}</ul></div>'
        )
    machines = []
    for machine, items in plan.by_machine().items():
        commands = "".join(
            '<div class="plan-command"><div>'
            f'<span class="what">{_esc(review.DECISION_LABELS[item.action])} {_esc(item.skill_id)} ({_esc(item.harness)})</span>'
            f"<code>{_esc(item.command)}</code></div>{_copy_button(item.command)}</div>"
            for item in items
        )
        machines.append(
            f'<section class="plan-machine"><header><h3>{_esc(machine)}</h3>'
            "<p>Run these in the workspace directory on this machine.</p></header>"
            f"{commands}</section>"
        )
    prompt = review.agent_prompt(view, plan)
    identifier = "plan-prompt-" + uuid.uuid4().hex
    prompt_html = (
        f'<div class="investigation"><button type="button" class="copy-prompt" data-copy-target="{identifier}" '
        f'data-copy-status="{identifier}-status">Copy prompt for an agent</button>'
        f'<span id="{identifier}-status" role="status" aria-live="polite" data-audience="agent">'
        "Select and copy manually if clipboard access is unavailable.</span>"
        '<details data-audience="agent"><summary>Agent prompt</summary>'
        f'<label for="{identifier}">Full plan prompt</label>'
        f'<textarea id="{identifier}" readonly rows="12">{_esc(prompt)}</textarea></details></div>'
    )

    def form(action: str, label: str, primary: bool = False) -> str:
        off = "" if editable else " disabled"
        css = ' class="primary"' if primary else ""
        return (
            '<form method="post" action="/deck/skills">'
            f"{_csrf_field(csrf, editable)}"
            f'<input type="hidden" name="action" value="{action}">'
            f'<button type="submit"{css}{off}>{_esc(label)}</button></form>'
        )

    actions = (
        '<div class="plan-actions">'
        + prompt_html
        + form("applied", "Mark applied", True)
        + form("discard", "Discard draft")
        + "</div>"
        '<p class="access-note">Mark applied records this reviewed plan for audit after you run it. '
        "The Hub does not run anything.</p>"
    )
    head = (
        '<h2 class="plan-title">Review plan</h2>'
        f'<p class="plan-sub">'
        f"{plan.count} change{'' if plan.count == 1 else 's'}. Nothing has run.</p>"
    )
    return _page(
        top + back + head + "".join(groups) + "".join(machines) + actions,
        nonce=nonce,
        now=now,
        title="Review skills plan",
    )


def redirect_target(filter_key: str, skill_id: str = "") -> str:
    key = filter_key if filter_key in dict(review.FILTERS) else "needs"
    return f"/deck/skills?filter={key}" + (f"#{anchor(skill_id)}" if skill_id else "")
